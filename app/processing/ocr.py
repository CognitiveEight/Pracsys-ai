"""
OCR for scanned evidence: Google Cloud Vision, tuned for full pages.

Three things here differ from the original single ``text_detection`` call, and
each has a fallback to the original behaviour, so no PDF that read before can
stop reading:

1. **Mode.** ``document_text_detection`` first: Vision's model for dense pages
   and handwriting (``text_detection`` is tuned for sparse text in photos:
   signs, labels). Same price per unit. If it errors or finds nothing, the
   original ``text_detection`` call runs.
2. **Resolution.** A phone-scanned PDF is one photo per page, typically at
   180-220 DPI. Re-rendering it at 150 DPI threw away a third to a half of the
   pixels before Vision saw them. When a page is *provably* nothing but one
   photo covering it (see :func:`native_page_image`), that photo is sent as
   is. Any other page is rendered, as before.
3. **Confidence.** Vision scores every word. A page whose mean word confidence
   is low (messy handwriting, a blurred photo) is reported, so the caller can
   say so instead of silently storing misread text.

Transient Vision failures (unavailable, deadline, quota) are retried once.
Every billed call is recorded with ``record_ocr_units`` here, the single
Vision call site.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, List, Optional, Tuple

from loguru import logger

#: Vision accepts up to ~10 MB of inline image per request; stay under it.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
#: Pixel cap per image (Vision rejects very large images as "Bad image data").
MAX_IMAGE_PIXELS = 20_000_000
#: Seconds per Vision request.
REQUEST_TIMEOUT_SECONDS = 60.0
#: Mean word confidence below which a page is flagged as possibly misread.
LOW_CONFIDENCE = 0.80
#: A placed photo must cover at least this share of the page to replace it.
#: Everything outside it is provably blank (only white fills are allowed),
#: so this only rules out thumbnails and logos.
MIN_PAGE_COVERAGE = 0.50
#: The photo's aspect ratio may differ from the page's by at most this much.
ASPECT_TOLERANCE = 0.03

#: Content-stream operators allowed on a page treated as "one photo": state,
#: colour and path-filling (iOS paints a white background before the photo).
#: Anything else (text, strokes, shading, inline images) means the page has
#: content the photo does not hold, so it is rendered instead.
_STATE_OPS = {b"q", b"Q", b"cm", b"gs", b"ri", b"i", b"w", b"J", b"j", b"M", b"d"}
_COLOUR_OPS = {b"cs", b"CS", b"sc", b"SC", b"scn", b"SCN", b"g", b"G", b"rg", b"RG", b"k", b"K"}
_PATH_OPS = {b"m", b"l", b"c", b"v", b"y", b"h", b"re", b"n", b"W", b"W*"}
_PAINT_OPS = {b"f", b"F", b"f*"}
_FILL_COLOUR_OPS = {b"sc", b"scn", b"g", b"rg", b"k"}

_TRANSIENT_ERRORS: Tuple[type, ...] = ()
try:  # google-api-core ships with google-cloud-vision
    from google.api_core import exceptions as _gexc

    _TRANSIENT_ERRORS = (
        _gexc.ServiceUnavailable,
        _gexc.DeadlineExceeded,
        _gexc.InternalServerError,
        _gexc.ResourceExhausted,
    )
except ImportError:  # pragma: no cover - vision unavailable entirely
    pass


@dataclass
class OcrResult:
    """One Vision read. ``text`` is empty when nothing was read."""

    text: str = ""
    confidence: Optional[float] = None  # mean word confidence, document mode only
    mode: str = ""  # "document" | "text"
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())

    @property
    def low_confidence(self) -> bool:
        return self.confidence is not None and self.confidence < LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# Vision client
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_client: Any = None
_client_key: Optional[str] = None


class OcrUnavailable(RuntimeError):
    """Vision is not installed or not configured; the message says which."""


def _api_key() -> str:
    import os

    from app.core.config import settings

    return (
        (getattr(settings, "google_cloud_vision_api_key", None) or "")
        or os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
        or ""
    ).strip()


def _vision_client():
    """One shared client per API key. Creating a client opens a gRPC channel;
    doing that per page was slow and the client is thread-safe."""
    global _client, _client_key
    try:
        from google.auth.api_key import Credentials as ApiKeyCredentials
        from google.cloud import vision
    except ImportError as err:
        raise OcrUnavailable("[Image content - GCP Vision API not available]") from err
    key = _api_key()
    if not key:
        raise OcrUnavailable(
            "[Error: Vision API key not configured (set GOOGLE_CLOUD_VISION_API_KEY)]"
        )
    with _client_lock:
        if _client is None or _client_key != key:
            _client = vision.ImageAnnotatorClient(credentials=ApiKeyCredentials(key))
            _client_key = key
        return _client


def _mean_word_confidence(response: Any) -> Optional[float]:
    try:
        scores = [
            word.confidence
            for page in response.full_text_annotation.pages
            for block in page.blocks
            for paragraph in block.paragraphs
            for word in paragraph.words
        ]
    except Exception:
        return None
    scores = [s for s in scores if isinstance(s, (int, float))]
    return sum(scores) / len(scores) if scores else None


# ---------------------------------------------------------------------------
# Reading order
# ---------------------------------------------------------------------------
#
# Vision returns text block by block. On a typed page with wide, justified
# word spacing it cuts every line into fragments and orders the fragments by
# block, so "renewed from 02-05-2006 to 02-05-2012" can come out with the
# dates swapped: harmless for search, wrong for a chronology. For a
# single-column page the reading order is simply lines top to bottom, words
# left to right, and the word boxes Vision returns are enough to rebuild it.
# Anything else (a column gutter, text not running left to right, too few
# words) keeps Vision's own order, which is what was used before.


@dataclass
class _Word:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str
    # Vision's verdict on what follows the word: True a space, False none
    # (punctuation, a joined suffix), None not reported.
    space_after: Optional[bool] = None

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1.0)


def _words(response: Any) -> Tuple[List[_Word], bool]:
    """Every word with its box, and whether they all read left to right."""
    words: List[_Word] = []
    rightward = 0
    for page in response.full_text_annotation.pages:
        for block in page.blocks:
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    v = list(word.bounding_box.vertices)
                    if len(v) != 4:
                        continue
                    text = "".join(s.text for s in word.symbols)
                    if not text:
                        continue
                    xs, ys = [p.x for p in v], [p.y for p in v]
                    # vertices[0] -> vertices[1] is the reading direction.
                    dx, dy = v[1].x - v[0].x, v[1].y - v[0].y
                    rightward += 1 if dx > 0 and abs(dy) <= dx else 0
                    words.append(
                        _Word(min(xs), max(xs), min(ys), max(ys), text, _space_after(word))
                    )
    return words, bool(words) and rightward >= 0.95 * len(words)


#: Vision DetectedBreak types followed by whitespace: SPACE, SURE_SPACE,
#: EOL_SURE_SPACE, LINE_BREAK. UNKNOWN (0) and HYPHEN (4) are not.
_SPACE_BREAKS = {1, 2, 3, 5}


def _space_after(word: Any) -> Optional[bool]:
    try:
        brk = word.symbols[-1].property.detected_break
    except (AttributeError, IndexError):
        return None
    kind = int(getattr(brk, "type_", getattr(brk, "type", 0)) or 0)
    if kind in _SPACE_BREAKS:
        return True
    # No break recorded. Vision leaves the field unset both for "joined" and
    # for "not reported", so only an explicit property counts as joined.
    return False if getattr(word.symbols[-1], "property", None) else None


def _has_column_gutter(words: List[_Word]) -> bool:
    """A vertical strip in the middle half of the text that no word crosses,
    with substantial text on both sides: two columns, or a table."""
    left = min(w.x0 for w in words)
    right = max(w.x1 for w in words)
    width = right - left
    if width <= 0:
        return False
    bins = 100
    covered = [0] * bins
    for w in words:
        a = int((w.x0 - left) / width * (bins - 1))
        b = int((w.x1 - left) / width * (bins - 1))
        for i in range(a, b + 1):
            covered[i] += 1
    for i in range(25, 75):
        if covered[i] == 0 and covered[i + 1] == 0:  # >= 2% of the width empty
            cut = left + width * (i + 1) / bins
            lhs = sum(1 for w in words if w.x1 <= cut)
            if min(lhs, len(words) - lhs) >= 0.2 * len(words):
                return True
    return False


def layout_text(response: Any) -> Optional[str]:
    """The page's text in reading order rebuilt from word boxes, or None when
    the page is not a plain single column (the caller keeps Vision's order)."""
    try:
        words, rightward = _words(response)
    except Exception:
        return None
    if len(words) < 8 or not rightward or _has_column_gutter(words):
        return None
    # Chain words into lines left to right; following the chain tolerates a
    # slightly skewed photo, where one line's y drifts across the page.
    lines: List[List[_Word]] = []
    for w in sorted(words, key=lambda w: (w.x0, w.y0)):
        best, best_gap = None, float("inf")
        for line in lines:
            last = line[-1]
            if last.x1 > w.x0 + 0.5 * w.h:
                continue  # not to the right of the line's end
            overlap = min(last.y1, w.y1) - max(last.y0, w.y0)
            ratio = overlap / min(last.h, w.h)
            # 0.3: adjacent lines never overlap (they sit ~1.6 heights apart),
            # while a word at the far end of a tilted line may overlap the
            # previous word by well under half. Among the lines that qualify,
            # the NEAREST preceding word wins: a speck of dirt sits inside the
            # height band of everything beside it and must not capture them.
            gap = w.x0 - last.x1
            if ratio >= 0.3 and gap < best_gap:
                best, best_gap = line, gap
        if best is None:
            lines.append([w])
        else:
            best.append(w)
    lines.sort(key=lambda line: (line[0].y0 + line[0].y1) / 2)

    rendered: List[str] = []
    heights = sorted(w.h for w in words)
    median_h = heights[len(heights) // 2]
    prev_bottom: Optional[float] = None
    for line in lines:
        parts = [line[0].text]
        for a, b in zip(line, line[1:]):
            # Vision's own spacing decides ("no." vs "no ."); box gaps only
            # when it did not report one.
            if a.space_after is None:
                joined = b.x0 - a.x1 < 0.3 * median_h
            else:
                joined = not a.space_after
            parts.append(("" if joined else " ") + b.text)
        top = min(w.y0 for w in line)
        if prev_bottom is not None and top - prev_bottom > 1.2 * median_h:
            rendered.append("")  # paragraph break
        rendered.append("".join(parts))
        prev_bottom = max(w.y1 for w in line)
    return "\n".join(rendered).strip() or None


def _call(method: str, image_bytes: bytes) -> Any:
    """One Vision request, retried once on a transient failure. Records the
    billed unit for every response received (an error response is billed)."""
    from google.cloud import vision

    from app.ai.usage import record_ocr_units

    client = _vision_client()
    image = vision.Image(content=image_bytes)
    for attempt in (1, 2):
        try:
            response = getattr(client, method)(image=image, timeout=REQUEST_TIMEOUT_SECONDS)
        except _TRANSIENT_ERRORS as err:
            if attempt == 2:
                raise
            logger.warning("Vision {} transient failure, retrying once: {}", method, err)
            time.sleep(1.0)
            continue
        record_ocr_units(1)
        return response
    raise RuntimeError("unreachable")  # pragma: no cover


def ocr_image(image_bytes: bytes) -> OcrResult:
    """Read one image. Never raises: failures come back in ``error``.

    ``document_text_detection`` first; if it errors or reads nothing, the
    original ``text_detection`` call, so an image that read before still reads.
    """
    errors: List[str] = []
    try:
        response = _call("document_text_detection", image_bytes)
        if response.error.message:
            errors.append(f"GCP Vision API error: {response.error.message}")
        else:
            text = (response.full_text_annotation.text or "").strip()
            if not text and response.text_annotations:
                text = (response.text_annotations[0].description or "").strip()
            if text:
                # Single-column pages: lines in reading order from the word
                # boxes. Anything else keeps Vision's order.
                text = layout_text(response) or text
                return OcrResult(text, _mean_word_confidence(response), "document")
    except OcrUnavailable as err:
        return OcrResult(error=str(err))
    except Exception as err:
        errors.append(str(err))

    try:
        response = _call("text_detection", image_bytes)
        if response.error.message:
            raise RuntimeError(f"GCP Vision API error: {response.error.message}")
        if response.text_annotations:
            text = (response.text_annotations[0].description or "").strip()
            if text:
                return OcrResult(text, None, "text")
        return OcrResult(error=None if not errors else "; ".join(errors))
    except OcrUnavailable as err:
        return OcrResult(error=str(err))
    except Exception as err:
        errors.append(str(err))
        return OcrResult(error="; ".join(errors))


# ---------------------------------------------------------------------------
# Page images
# ---------------------------------------------------------------------------


def prepare_image(image: Any) -> bytes:
    """Encode a PIL image for Vision: RGB/greyscale, under the pixel and
    request-size caps. PNG first (lossless); JPEG only if PNG is too large."""
    from PIL import Image

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    pixels = image.width * image.height
    if pixels > MAX_IMAGE_PIXELS:
        ratio = (MAX_IMAGE_PIXELS / pixels) ** 0.5
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))), resample)
    for _ in range(6):
        buf = BytesIO()
        image.save(buf, format="PNG", optimize=False)
        if buf.tell() <= MAX_REQUEST_BYTES:
            return buf.getvalue()
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=92)
        if buf.tell() <= MAX_REQUEST_BYTES:
            return buf.getvalue()
        image = image.resize((max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8))), resample)
    return buf.getvalue()


def _matmul(m: List[float], n: List[float]) -> List[float]:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return [
        a * a2 + b * c2, a * b2 + b * d2,
        c * a2 + d * c2, c * b2 + d * d2,
        e * a2 + f * c2 + e2, e * b2 + f * d2 + f2,
    ]


def _is_white(op: bytes, operands: Any) -> bool:
    """Whether a fill-colour operator sets white (gray 1, RGB 1 1 1, CMYK 0 0 0 0)."""
    try:
        values = [float(v) for v in operands]
    except (TypeError, ValueError):
        return False  # a named pattern or colour space: not provably white
    if op == b"k" or len(values) == 4:
        return all(v == 0 for v in values)
    return bool(values) and all(v == 1 for v in values)


def _single_photo_placement(page: Any, reader: Any) -> Optional[Tuple[str, List[float]]]:
    """``(xobject_name, ctm)`` when the page draws exactly one image and
    nothing else that could carry content, else None."""
    from PyPDF2.generic import ContentStream

    contents = page.get_contents()
    if contents is None:
        return None
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack: List[Tuple[List[float], bool]] = []
    fill_is_white = False  # the initial fill colour is black
    placed: Optional[Tuple[str, List[float]]] = None
    for operands, op in ContentStream(contents, reader).operations:
        if op in _FILL_COLOUR_OPS:
            fill_is_white = _is_white(op, operands)
            continue
        if op == b"q":
            stack.append((list(ctm), fill_is_white))
        elif op == b"Q":
            if stack:
                ctm, fill_is_white = stack.pop()
        elif op == b"cm":
            try:
                ctm = _matmul([float(x) for x in operands], ctm)
            except (TypeError, ValueError):
                return None
        elif op == b"Do":
            if placed is not None:
                return None  # a second XObject: more than one photo, or a form
            placed = (str(operands[0]), list(ctm))
        elif op in _STATE_OPS or op in _COLOUR_OPS:
            continue
        elif op in _PATH_OPS:
            if placed is not None:
                return None  # drawn on top of the photo: content it lacks
        elif op in _PAINT_OPS:
            # A white fill under the photo is the paper (iOS paints one);
            # any other fill could be content the photo does not hold.
            if placed is not None or not fill_is_white:
                return None
        else:
            return None  # text, strokes, shading, inline image, marked content...
    return placed


def native_page_jpeg(page: Any, reader: Any) -> Optional[bytes]:
    """The page's own JPEG photo, when the page is provably just that photo;
    else None and the caller renders the page.

    Required: no /Rotate, crop box equal to media box, exactly one plain 8-bit
    JPEG image XObject drawn axis-aligned and unmirrored over at least
    ``MIN_PAGE_COVERAGE`` of the page, with the placement's aspect ratio,
    and nothing drawn after it. Only the JPEG header is read here; the caller
    decodes it and falls back to rendering if that fails.
    """
    from PIL import Image

    try:
        if int(page.get("/Rotate", 0) or 0) % 360:
            return None
        media, crop = page.mediabox, page.cropbox
        if [float(v) for v in media] != [float(v) for v in crop]:
            return None
        placement = _single_photo_placement(page, reader)
        if placement is None:
            return None
        name, (a, b, c, d, e, f) = placement
        if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
            return None  # rotated, skewed or mirrored placement
        pw, ph = float(media.width), float(media.height)
        x0, y0 = e - float(media.left), f - float(media.bottom)
        if x0 < -2 or y0 < -2 or x0 + a > pw + 2 or y0 + d > ph + 2:
            return None
        if (a * d) / (pw * ph) < MIN_PAGE_COVERAGE:
            return None
        xobj = page["/Resources"]["/XObject"][name].get_object()
        if xobj.get("/Subtype") != "/Image" or "/SMask" in xobj or "/Mask" in xobj:
            return None
        # Plain 8-bit JPEG only: the phone-scan case, and the one encoding PIL
        # decodes exactly as the PDF shows it. CCITT/JBIG2 polarity, /Decode
        # inversion, image masks and indexed palettes are PDF-side semantics a
        # raw extraction would get wrong; those pages are rendered.
        filters = xobj.get("/Filter")
        filters = [str(x) for x in (filters if isinstance(filters, list) else [filters])]
        if filters != ["/DCTDecode"] or "/Decode" in xobj or xobj.get("/ImageMask"):
            return None
        if int(xobj.get("/BitsPerComponent", 8)) != 8:
            return None
        data = xobj.get_data()  # DCTDecode is passed through: the JPEG file itself
        header = Image.open(BytesIO(data))  # reads the header only
        if header.format != "JPEG":
            return None
        if abs((header.width / header.height) - (a / d)) / (a / d) > ASPECT_TOLERANCE:
            return None
        return data
    except Exception as err:  # unusual structure: render instead
        logger.debug("Native page image not usable, rendering instead: {}", type(err).__name__)
        return None


def jpeg_for_vision(data: bytes) -> Optional[bytes]:
    """A native page JPEG ready to send: as is when it decodes and fits the
    caps (no re-encoding, so no quality loss), re-encoded when it does not, and
    None when it does not decode (the caller then renders the page)."""
    from PIL import Image

    try:
        image = Image.open(BytesIO(data))
        image.load()
    except Exception:
        return None
    if (
        len(data) <= MAX_REQUEST_BYTES
        and image.mode in ("RGB", "L")
        and image.width * image.height <= MAX_IMAGE_PIXELS
    ):
        return data
    return prepare_image(image)
