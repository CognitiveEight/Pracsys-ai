"""OCR of scanned PDFs: better reads, and nothing that read before can stop.

Vision is never called: a fake client stands in, answering with the size of
the image it received, so a test can see exactly what would have been sent.

What is pinned here:
* a phone scan (one JPEG per page) is sent as its own photo, at full
  resolution, instead of a 150-DPI re-render;
* every page that is NOT provably just that photo (text or a stamp drawn on
  top, two images, rotation, a mirrored or rotated placement, a coloured
  shape, a crop box, a non-JPEG image) is rendered, as before;
* a photo that will not decode falls back to rendering;
* document mode first, the original text mode when it fails;
* transient failures retried once; every billed call counted, from worker
  threads too;
* unreadable and low-confidence pages are marked, never silently dropped.
"""

import io
import types

import pytest

fitz = pytest.importorskip("fitz")
from PIL import Image  # noqa: E402

from app.processing import file_handlers as fh  # noqa: E402
from app.processing import ocr  # noqa: E402

A4 = (595, 842)


def _jpeg(w=1500, h=2100, mode="RGB", color=(250, 250, 250)) -> bytes:
    buf = io.BytesIO()
    fill = color if mode == "RGB" else (0 if mode == "CMYK" else 250)
    if mode == "CMYK":
        fill = (0, 0, 0, 10)
    Image.new(mode, (w, h), fill).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _png(w=1500, h=2100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


def _pdf(build) -> bytes:
    doc = fitz.open()
    build(doc)
    out = doc.tobytes()
    doc.close()
    return out


def _scan_page(doc, image=None, rect=None, white_background=False, size=A4):
    page = doc.new_page(width=size[0], height=size[1])
    if white_background:
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    page.insert_image(rect or page.rect, stream=image or _jpeg(), keep_proportion=False)
    return page


def _native(pdf: bytes):
    from PyPDF2 import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return [ocr.native_page_jpeg(p, reader) for p in reader.pages]


# ---------------------------------------------------------------------------
# Which pages may be sent as their own photo
# ---------------------------------------------------------------------------


def test_phone_scans_are_sent_as_their_own_photo():
    # Full-bleed, and the iOS layout: a white page with a narrower photo centred
    # on it (photo aspect 1300:2100 = placement 521:842).
    pdf = _pdf(lambda d: [
        _scan_page(d, image=_jpeg(1500, 2122)),
        _scan_page(d, image=_jpeg(1300, 2100), white_background=True,
                   rect=fitz.Rect(37, 0, 558, 842)),
    ])
    got = _native(pdf)
    assert all(got)
    assert Image.open(io.BytesIO(got[0])).size == (1500, 2122)


def test_stretched_photo_is_rendered_not_sent_distorted():
    # Placement aspect differs from the photo's: the page shows it stretched.
    assert _native(_pdf(lambda d: _scan_page(d, image=_jpeg(1500, 1500)))) == [None]


@pytest.mark.parametrize(
    "case",
    [
        "text_on_top", "two_images", "page_rotate", "rotated_placement",
        "small_logo", "coloured_fill", "crop_box", "png_scan",
    ],
)
def test_pages_with_anything_else_are_rendered(case):
    def build(doc):
        if case == "text_on_top":
            page = _scan_page(doc)
            page.insert_text((50, 50), "RECEIVED 12/03/2025")  # a stamp the photo lacks
        elif case == "two_images":
            page = _scan_page(doc)
            page.insert_image(fitz.Rect(400, 700, 590, 840), stream=_jpeg(300, 200))
        elif case == "page_rotate":
            page = _scan_page(doc)
            page.set_rotation(90)
        elif case == "rotated_placement":
            page = doc.new_page(width=A4[0], height=A4[1])
            page.insert_image(page.rect, stream=_jpeg(2100, 1500), rotate=90)
        elif case == "small_logo":
            page = doc.new_page(width=A4[0], height=A4[1])
            page.insert_image(fitz.Rect(20, 20, 220, 300), stream=_jpeg(400, 560))
        elif case == "coloured_fill":
            page = doc.new_page(width=A4[0], height=A4[1])
            page.draw_rect(fitz.Rect(0, 0, 595, 60), color=None, fill=(0.8, 0, 0))
            page.insert_image(fitz.Rect(0, 60, 595, 842), stream=_jpeg(1500, 1970))
        elif case == "crop_box":
            page = _scan_page(doc)
            page.set_cropbox(fitz.Rect(0, 0, 500, 700))
        elif case == "png_scan":
            _scan_page(doc, image=_png())

    assert _native(_pdf(build)) == [None]


# ---------------------------------------------------------------------------
# The page pipeline, with a fake Vision client
# ---------------------------------------------------------------------------


class _FakeVision:
    """Answers with the received image's size; scripted failures per call."""

    def __init__(self, script=None, confidence=None):
        self.calls = []
        self.script = list(script or [])
        self.confidence = confidence

    def _respond(self, method, image):
        self.calls.append(method)
        action = self.script.pop(0) if self.script else "ok"
        if action == "transient":
            from google.api_core import exceptions

            raise exceptions.ServiceUnavailable("502:Bad Gateway")
        self.served = getattr(self, "served", 0) + 1
        if action == "error":
            return types.SimpleNamespace(
                error=types.SimpleNamespace(message="Bad image data"),
                text_annotations=[], full_text_annotation=types.SimpleNamespace(text="", pages=[]),
            )
        if action == "empty":
            return types.SimpleNamespace(
                error=types.SimpleNamespace(message=""), text_annotations=[],
                full_text_annotation=types.SimpleNamespace(text="", pages=[]),
            )
        im = Image.open(io.BytesIO(image.content))
        # Distinct per call: identical text on every page is (rightly) stripped
        # as a running header by the boilerplate filter.
        text = f"Page text {im.width}x{im.height} of the complaint, read number {self.served}"
        words = [types.SimpleNamespace(confidence=self.confidence)] if self.confidence else []
        pages = [types.SimpleNamespace(blocks=[types.SimpleNamespace(
            paragraphs=[types.SimpleNamespace(words=words)])])]
        return types.SimpleNamespace(
            error=types.SimpleNamespace(message=""),
            text_annotations=[types.SimpleNamespace(description=text)],
            full_text_annotation=types.SimpleNamespace(text=text, pages=pages),
        )

    def document_text_detection(self, image, timeout=None):
        return self._respond("document", image)

    def text_detection(self, image, timeout=None):
        return self._respond("text", image)


@pytest.fixture
def vision(monkeypatch):
    fake = _FakeVision()
    monkeypatch.setattr(ocr, "_vision_client", lambda: fake)
    monkeypatch.setattr(fh, "GCP_OCR_AVAILABLE", True)
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    return fake


def test_scan_is_read_at_full_resolution_in_document_mode(vision):
    pdf = _pdf(lambda d: [_scan_page(d, image=_jpeg(1500, 2122)) for _ in range(3)])
    text = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    assert text.count("Page text 1500x2122") == 3  # the photo itself, not a 150-DPI render
    assert vision.calls == ["document"] * 3
    assert [l for l in text.splitlines() if l.startswith("--- Page")] == [
        "--- Page 1 ---", "--- Page 2 ---", "--- Page 3 ---"]


def test_corrupt_photo_falls_back_to_rendering(vision):
    good = _jpeg(1500, 2122)
    broken = good[:200] + b"\x00" * 50 + good[250:600]  # valid header, undecodable body
    pdf = _pdf(lambda d: _scan_page(d, image=good))
    pdf = pdf.replace(good, broken + b" " * (len(good) - len(broken)))
    text = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    assert "--- Page 1 ---" in text and "Page text" in text  # read from the render
    assert "1500x2122" not in text


def test_inverted_cmyk_photo_is_rendered_not_sent_raw(vision):
    # Adobe CMYK JPEGs carry /Decode [1 0 1 0 ...]: raw, Vision would get a
    # colour negative. The page is rendered, which applies the inversion.
    pdf = _pdf(lambda d: _scan_page(d, image=_jpeg(1500, 2122, mode="CMYK")))
    assert _native(pdf) == [None]
    text = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    assert "Page text" in text and "1500x2122" not in text


def test_cmyk_jpeg_bytes_are_converted_before_sending():
    out = ocr.jpeg_for_vision(_jpeg(1500, 2122, mode="CMYK"))
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_huge_page_render_stays_under_the_pixel_cap(vision):
    def build(doc):
        page = doc.new_page(width=2384, height=3370)  # A0
        page.insert_image(fitz.Rect(0, 0, 2384, 3370), stream=_png(800, 1130))
    text = fh.FileProcessor.extract_text_from_pdf(_pdf(build), mark_pages=True)
    w, h = (int(x) for x in text.split("Page text ")[1].split(" ")[0].split("x"))
    assert w * h <= ocr.MAX_IMAGE_PIXELS


def test_transient_failure_is_retried_and_both_calls_billed(vision):
    from app.ai.usage import capture_usage

    vision.script = ["transient"]
    pdf = _pdf(lambda d: _scan_page(d))
    with capture_usage() as usage:
        text = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    assert "Page text" in text
    assert vision.calls == ["document", "document"]
    assert usage.ocr_units == 1  # the failed attempt returned no response: not billed


def test_document_mode_error_falls_back_to_the_original_text_mode(vision):
    vision.script = ["error"]
    text = fh.FileProcessor.extract_text_from_image(_jpeg())
    assert text.startswith("Page text")
    assert vision.calls == ["document", "text"]


def test_usage_is_counted_from_worker_threads(vision):
    from app.ai.usage import capture_usage

    pdf = _pdf(lambda d: [_scan_page(d) for _ in range(9)])
    with capture_usage() as usage:
        fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    assert usage.ocr_units == 9


def test_unreadable_page_is_marked_in_place_not_dropped(vision):
    # Page 2 fails in both modes (and its retry): it keeps its place.
    vision.script = ["ok", "error", "error"]
    pdf = _pdf(lambda d: [_scan_page(d) for _ in range(3)])
    text = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    markers = [l for l in text.splitlines() if l.startswith("--- Page")]
    assert markers == ["--- Page 1 ---", "--- Page 2 ---", "--- Page 3 ---"]
    assert "[OCR note: this page could not be read." in text.split("--- Page 3 ---")[0]
    # Without page marks (templates, pdf_to_docx) the old output is kept: no notes.
    vision.script = ["ok", "error", "error"]
    plain = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=False)
    assert "OCR note" not in plain and plain.count("Page text") == 2


def test_all_pages_unreadable_keeps_the_original_sentinel(vision):
    vision.script = ["empty", "empty"] * 2
    pdf = _pdf(lambda d: [_scan_page(d) for _ in range(2)])
    assert fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True) == (
        "[No text could be extracted from PDF images]"
    )


def test_low_confidence_page_is_flagged(vision):
    vision.confidence = 0.55
    pdf = _pdf(lambda d: [_scan_page(d), _scan_page(d)])
    text = fh.FileProcessor.extract_text_from_pdf(pdf, mark_pages=True)
    # Two identical notes survive: they are added after the boilerplate strip.
    assert text.count("[OCR note: this page was read with low confidence (55%)") == 2


def test_image_sentinels_are_unchanged(monkeypatch, vision):
    vision.script = ["empty", "empty"]
    assert fh.FileProcessor.extract_text_from_image(_jpeg()) == "[No text detected in image]"
    monkeypatch.setattr(fh, "GCP_OCR_AVAILABLE", False)
    assert fh.FileProcessor.extract_text_from_image(_jpeg()) == (
        "[Image content - GCP Vision API not available]"
    )


def test_missing_api_key_keeps_its_message(monkeypatch):
    monkeypatch.setattr(ocr, "_api_key", lambda: "")
    monkeypatch.setattr(fh, "GCP_OCR_AVAILABLE", True)
    assert fh.FileProcessor.extract_text_from_image(_jpeg()).startswith(
        "[Error: Vision API key not configured"
    )


def test_text_layer_pdfs_never_reach_ocr(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("OCR must not run for a PDF with a text layer")

    monkeypatch.setattr(fh.FileProcessor, "_extract_text_from_pdf_with_ocr", boom)

    def build(doc):
        for n in range(3):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {n + 1}: the respondent failed to pay the dues " * 3)
    text = fh.FileProcessor.extract_text_from_pdf(_pdf(build), mark_pages=True)
    assert text.count("the respondent failed to pay") >= 3


# ---------------------------------------------------------------------------
# Reading order rebuilt from word boxes
# ---------------------------------------------------------------------------


def _vision_words(words):
    """A fake document_text_detection response from (text, x0, y0, x1, y1,
    space_after) tuples, all in one block, in the (scrambled) order given."""
    def vertex(x, y):
        return types.SimpleNamespace(x=x, y=y)

    def word(text, x0, y0, x1, y1, space=True, rotated=False):
        v = [vertex(x0, y0), vertex(x1, y0), vertex(x1, y1), vertex(x0, y1)]
        if rotated:  # reading direction top to bottom
            v = [vertex(x0, y0), vertex(x0, y1), vertex(x1, y1), vertex(x1, y0)]
        brk = types.SimpleNamespace(type_=1 if space else 0)
        symbols = [types.SimpleNamespace(text=c, property=types.SimpleNamespace(detected_break=None))
                   for c in text]
        symbols[-1] = types.SimpleNamespace(text=text[-1], property=types.SimpleNamespace(detected_break=brk))
        return types.SimpleNamespace(bounding_box=types.SimpleNamespace(vertices=v), symbols=symbols)

    built = [word(*w) for w in words]
    page = types.SimpleNamespace(blocks=[types.SimpleNamespace(paragraphs=[types.SimpleNamespace(words=built)])])
    return types.SimpleNamespace(full_text_annotation=types.SimpleNamespace(pages=[page], text="vision order"))


def _line(words, y, x=100, tilt=0.0, h=24):
    """Words laid out like typed text: width follows the word's length, so
    gaps fall at different places on every line (aligned gaps are a table)."""
    out, x0 = [], x
    for text in words:
        x1 = x0 + 14 * len(text)
        dy = tilt * (x0 - x)
        out.append((text, x0, y - dy, x1, y - dy + h))
        x0 = x1 + 18
    return out


def test_scrambled_single_column_is_put_back_in_reading_order():
    lines = [
        _line(["renewed", "from", "02-05-2006", "to"], 100),  # line pitch 1.5x height
        _line(["02-05-2012", "by", "skipping", "the"], 136),
        _line(["again", "as", "of", "another"], 172, tilt=0.02),  # skewed photo
    ]
    scrambled = [lines[1][0], lines[0][2], lines[2][3], lines[0][0], lines[1][2],
                 lines[2][0], lines[0][3], lines[1][1], lines[0][1], lines[2][1],
                 lines[1][3], lines[2][2]]
    text = ocr.layout_text(_vision_words(scrambled))
    assert text.splitlines() == [
        "renewed from 02-05-2006 to",
        "02-05-2012 by skipping the",
        "again as of another",
    ]


def test_speck_does_not_capture_the_next_word():
    words = _line(["again", "as", "of", "the", "first", "another"], 300)
    end = words[-1][3]
    words.append(("blunder", end + 18, 296, end + 116, 320))
    words.append(("#", 196, 296, 207, 307))  # a dot of dirt inside the band
    words += _line(["mistake", "committed", "by", "the"], 336)
    text = ocr.layout_text(_vision_words(words))
    assert "again as of the first another blunder" in text


def test_spacing_follows_visions_breaks():
    words = [("no", 100, 100, 140, 124, False), (".", 141, 100, 148, 124, True),
             ("2", 160, 100, 175, 124, True), ("as", 200, 100, 240, 124, True)]
    for i, line in enumerate([
        ["the", "respondent", "failed", "to", "pay"],
        ["within", "the", "time", "allowed", "under"],
        ["clause", "seven", "of", "the", "agreement"],
        ["and", "the", "notice", "dated", "earlier"],
    ]):
        words += _line(line, 136 + 36 * i)
    assert ocr.layout_text(_vision_words(words)).splitlines()[0] == "no. 2 as"


def test_two_columns_keep_visions_order():
    left = [w for y in range(100, 700, 60) for w in _line(["left", "column", "text"], y, x=100)]
    right = [w for y in range(100, 700, 60) for w in _line(["right", "side", "words"], y, x=900)]
    assert ocr.layout_text(_vision_words(left + right)) is None


def test_rotated_text_keeps_visions_order():
    words = [(f"w{i}", 100, 100 + i * 80, 124, 170 + i * 80, True, True) for i in range(12)]
    assert ocr.layout_text(_vision_words(words)) is None


def test_too_few_words_keep_visions_order():
    assert ocr.layout_text(_vision_words(_line(["only", "three", "words"], 100))) is None
