"""
Structure- and page-aware chunking for evidence documents.

Why this replaced the fixed 1,000-word split
--------------------------------------------
The old splitter cut every document into 1,000-word runs with no overlap and
rejoined words with single spaces. That:

* destroyed every newline, so headings, numbered paragraphs and tables ran
  together;
* split sentences — and figures, dates and section numbers — across chunks,
  where neither half matches a query;
* kept no page information, so no citation could say where a passage is.

This chunker splits on the document's own structure (page markers, blank lines,
numbered paragraphs, headings), packs whole blocks up to a target size, carries
a small overlap into the next chunk, and records where each chunk sits: page
range, character offsets in the full text, and the nearest heading.

Page markers
------------
PDF extraction writes ``--- Page N ---`` before each page (see
``app/processing/file_handlers.py``). They are kept in the chunk text — the
features that read whole documents resolve dates to pages from them — and are
also the source of ``page_start`` / ``page_end``. Documents without markers
(DOCX, TXT, legacy chunks) get ``page_start = page_end = None``.

Reassembly
----------
Chunks overlap, so concatenating them duplicates text. Each chunk records
``overlap_prev`` — how many of its leading characters repeat the previous
chunk — and :func:`reassemble` drops exactly those, giving back the original
text byte for byte.
"""

import bisect
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

#: Target and hard maximum chunk size, in characters (~350–450 tokens of English).
TARGET_CHARS = 1800
MAX_CHARS = 2400
#: Blocks smaller than this are merged into their neighbour.
MIN_CHARS = 300
#: Fraction of a chunk carried into the next one as overlap.
OVERLAP_FRACTION = 0.15

PAGE_MARKER_RE = re.compile(r"^---\s*Page\s+(\d+)\s*---\s*$", re.I | re.M)
_AUDIO_TS_RE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]", re.M)
_HEADING_RE = re.compile(
    r"^(?:"
    r"(?:ANNEXURE|SCHEDULE|EXHIBIT|APPENDIX|PART|CHAPTER|SECTION|ARTICLE|CLAUSE)\b.{0,80}"
    r"|(?:\d{1,2}(?:\.\d{1,2})*\.?|[IVXLC]{1,6}\.|\([a-z]\))\s+[A-Z][^\n]{2,80}"
    r"|[A-Z][A-Z0-9 ,.&'()/-]{3,80}"
    r")$"
)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?;:।])\s+")


@dataclass
class Chunk:
    """One retrievable unit of a document."""

    index: int
    text: str
    char_start: int
    char_end: int
    overlap_prev: int = 0
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    heading: Optional[str] = None
    time_start: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)

    def as_metadata(self) -> Dict[str, object]:
        """Chunk-level fields stored in the vector payload."""
        return {
            "chunk_index": self.index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "overlap_prev": self.overlap_prev,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "heading": self.heading,
            "time_start": self.time_start,
        }


def _page_map(text: str) -> List[tuple]:
    """``[(char_offset, page_number), ...]`` from ``--- Page N ---`` markers."""
    return [(m.start(), int(m.group(1))) for m in PAGE_MARKER_RE.finditer(text)]


def _page_at(pages: Sequence[tuple], offset: int) -> Optional[int]:
    if not pages:
        return None
    offsets = [p[0] for p in pages]
    i = bisect.bisect_right(offsets, offset) - 1
    return pages[i][1] if i >= 0 else pages[0][1]


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 90 or PAGE_MARKER_RE.match(line):
        return False
    return bool(_HEADING_RE.match(line))


def _blocks(text: str) -> List[tuple]:
    """Split into ``(start, end)`` spans on blank lines, page markers and
    headings, keeping every character of ``text`` inside exactly one span."""
    boundaries = {0, len(text)}
    for m in re.finditer(r"\n[ \t]*\n", text):
        boundaries.add(m.end())
    for m in PAGE_MARKER_RE.finditer(text):
        boundaries.add(m.start())
    offset = 0
    for line in text.splitlines(keepends=True):
        if _is_heading(line):
            boundaries.add(offset)
        offset += len(line)
    points = sorted(b for b in boundaries if 0 <= b <= len(text))
    return [(a, b) for a, b in zip(points, points[1:]) if text[a:b].strip()]


def _split_long(text: str, start: int, end: int) -> List[tuple]:
    """Break a span longer than MAX_CHARS at sentence ends, then at spaces."""
    if end - start <= MAX_CHARS:
        return [(start, end)]
    pieces: List[tuple] = []
    cursor = start
    while end - cursor > MAX_CHARS:
        window = text[cursor : cursor + MAX_CHARS]
        cut = None
        for m in _SENTENCE_END_RE.finditer(window):
            if m.end() >= MIN_CHARS:
                cut = m.end()
        if cut is None:
            space = window.rfind(" ", MIN_CHARS)
            cut = space + 1 if space > 0 else MAX_CHARS
        pieces.append((cursor, cursor + cut))
        cursor += cut
    pieces.append((cursor, end))
    return pieces


def _overlap_start(text: str, start: int, end: int) -> int:
    """Where the overlap carried into the next chunk begins: roughly the last
    OVERLAP_FRACTION of the span, moved forward to a sentence or line start."""
    want = int((end - start) * OVERLAP_FRACTION)
    if want <= 0:
        return end
    candidate = end - want
    for sep in ("\n", ". ", "; ", " "):
        pos = text.find(sep, candidate, end)
        if pos != -1 and pos + len(sep) < end:
            return pos + len(sep)
    return candidate


def chunk_text(text: str, with_overlap: bool = True) -> List[Chunk]:
    """Chunk extracted document text. Deterministic for the same input.

    ``with_overlap=False`` for spreadsheets: a repeated row is harmless in prose
    and wrong in a table, where reading two adjacent chunks would count it
    twice.
    """
    text = text or ""
    if not text.strip():
        return []
    pages = _page_map(text)

    spans: List[tuple] = []
    for a, b in _blocks(text):
        spans.extend(_split_long(text, a, b))

    # Greedily pack blocks up to TARGET_CHARS, never exceeding MAX_CHARS. A
    # page marker starts a new chunk when the current one is already useful, so
    # page ranges stay tight.
    packed: List[tuple] = []
    cur_start: Optional[int] = None
    cur_end = 0
    for a, b in spans:
        starts_page = bool(PAGE_MARKER_RE.match(text[a:b].lstrip("\n")))
        if cur_start is None:
            cur_start, cur_end = a, b
            continue
        size = cur_end - cur_start
        if (size + (b - a) > MAX_CHARS) or (
            size >= TARGET_CHARS
        ) or (starts_page and size >= MIN_CHARS * 2):
            packed.append((cur_start, cur_end))
            cur_start, cur_end = a, b
        else:
            cur_end = b
    if cur_start is not None:
        packed.append((cur_start, cur_end))

    # Fold a tiny trailing chunk into its predecessor.
    if len(packed) >= 2 and packed[-1][1] - packed[-1][0] < MIN_CHARS:
        last = packed.pop()
        packed[-1] = (packed[-1][0], last[1])

    # Headings, in document order, for "nearest heading above" lookup.
    headings: List[tuple] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if _is_heading(line):
            headings.append((offset, line.strip()))
        offset += len(line)
    heading_offsets = [h[0] for h in headings]

    chunks: List[Chunk] = []
    for i, (a, b) in enumerate(packed):
        start = a
        overlap = 0
        if i > 0 and with_overlap:
            start = _overlap_start(text, packed[i - 1][0], packed[i - 1][1])
            start = min(start, a)
            overlap = a - start
        body = text[start:b]
        # A heading just inside the chunk (typically right after its page
        # marker) names it better than the one before it.
        hi = bisect.bisect_right(heading_offsets, min(b - 1, a + 200)) - 1
        ts = _AUDIO_TS_RE.search(text, a, b)
        chunks.append(
            Chunk(
                index=i,
                text=body,
                char_start=start,
                char_end=b,
                overlap_prev=overlap,
                page_start=_page_at(pages, a),
                page_end=_page_at(pages, max(a, b - 1)),
                heading=headings[hi][1] if hi >= 0 else None,
                time_start=ts.group(0).strip("[]") if ts else None,
            )
        )
    return chunks


def reassemble(chunks: Sequence[Dict[str, object]]) -> str:
    """Rebuild a document from stored chunks in ``chunk_index`` order.

    ``chunks`` are ``{"content": str, "overlap_prev": int}`` dicts. Legacy
    chunks (no ``overlap_prev``) were cut on word boundaries and are rejoined
    with a single space, as before.
    """
    out: List[str] = []
    for ch in chunks:
        content = str(ch.get("content") or "")
        overlap = ch.get("overlap_prev")
        if overlap is None:
            if out:
                out.append(" ")
            out.append(content)
        else:
            out.append(content[int(overlap) :])
    return "".join(out)
