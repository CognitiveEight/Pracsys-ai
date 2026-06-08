"""Placeholder-based DOCX template filler — and catalog-section subsetter.

When a lawyer's template uses placeholders to mark fill slots, we can
produce a final DOCX that is **byte-for-byte identical to the template
except where placeholder text is replaced**. That gives lawyers true
template fidelity — the original fonts, spacing, indentation, tables,
headers/footers, and theme are all preserved because we never regenerate
the document; we only edit the placeholder text in place.

Two filling modes are exposed:

- :func:`fill_template` — replace placeholder text everywhere in the
  document. Used for single-document templates (one bail application,
  one affidavit, one petition).

- :func:`fill_template_sectional` — clone the template, **drop the
  numbered top-level sections that the lawyer did NOT request**, then
  fill placeholders in the surviving sections. Used for catalog
  templates where one DOCX defines multiple variant documents (e.g. a
  pack of 8 affidavit types, lawyer picks 4).

Placeholder conventions supported (matched anywhere in the document
including tables, headers, and footers):

- ``[Anything]``                 — square brackets (most common in legal)
- ``<Anything>``                 — angle brackets
- ``{{Anything}}``               — handlebar-style
- ``(PascalCase_snake_case)``    — parenthesized PascalCase+snake_case
- ``___`` (3+ underscores)       — fill-in-the-blank lines

Section detection (for the catalog path) looks for paragraphs matching
``^N. <Title>`` where ``<Title>`` ends in (or contains) a known
document-type keyword — Affidavit, Application, Petition, Notice,
Statement, Declaration, Agreement, Memorandum, Power of Attorney, Will,
Undertaking, Indemnity, Plaint, Reply, Rejoinder, Bond, or Deed.

The filler is intentionally conservative:

- Replacements happen only inside paragraph text. Other XML (drawings,
  comments, content controls) is left untouched.
- A multi-line replacement (i.e. one containing ``\\n``) inserts proper
  ``<w:br/>`` soft-line-breaks rather than literal newline characters.
- Multi-run placeholders (e.g. ``[Name of Accused]`` split across runs
  because Word stored ``[Name`` in one run and ``of Accused]`` in
  another) are joined for matching, then replaced with the first run's
  formatting retained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple

from docx import Document
from docx.document import Document as _DocxDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from loguru import logger


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


# Tuples of (compiled-regex, kind). The regex must capture the inner
# label text (without delimiters) in group 1 — except the underscore
# pattern, which has no inner label and uses the surrounding context.
PLACEHOLDER_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # [Anything in single brackets but no newlines, max 120 chars]
    (re.compile(r"\[([^\[\]\n]{1,120})\]"), "bracket"),
    # <Anything in angle brackets>
    (re.compile(r"<([^<>\n]{1,120})>"), "angle"),
    # {{handlebars}}
    (re.compile(r"\{\{([^{}\n]{1,120})\}\}"), "handlebar"),
    # (PascalCase_snake_case) — labels like (ApplicantNew_Name) used by
    # form-builder-style templates. Conservative pattern: leading capital
    # letter and at least one underscore so we don't match parenthetical
    # asides like "(City Name)" or "(Please Enter)".
    (re.compile(r"\(([A-Z][A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)\)"), "paren_label"),
]

# Three or more underscores — a fill blank. No inner label.
_UNDERSCORE_BLANK = re.compile(r"_{3,}")


@dataclass(frozen=True)
class PlaceholderHit:
    """One detected placeholder occurrence."""

    raw: str  # full match incl. delimiters, e.g. "[Name of Accused]"
    label: str  # inner text or contextual label for underscore blanks
    kind: str  # "bracket" | "angle" | "handlebar" | "blank"


def _iter_all_paragraphs(doc: _DocxDocument) -> Iterable[Paragraph]:
    """Yield every Paragraph in body, tables, headers, and footers."""
    for p in doc.paragraphs:
        yield p
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p
                # nested tables
                for inner_t in cell.tables:
                    for ir in inner_t.rows:
                        for ic in ir.cells:
                            for p in ic.paragraphs:
                                yield p
    for section in doc.sections:
        for part in (section.header, section.footer,
                     section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            try:
                for p in part.paragraphs:
                    yield p
            except Exception:
                continue


def _label_for_blank(text: str, match: re.Match) -> Optional[str]:
    """Pick a contextual label for an ``___`` fill blank.

    Looks backward up to 60 chars for a "Label:" prefix and returns it.
    Returns ``None`` when no meaningful label can be inferred — those
    anonymous blanks are skipped (the lawyer fills them manually) since
    the AI has nothing useful to map them to.
    """
    start = match.start()
    window = text[max(0, start - 80):start]
    m = re.search(r"([A-Za-z][A-Za-z0-9 /'\-]{1,40})\s*[:\-]\s*$", window)
    if m:
        return m.group(1).strip()
    return None


def extract_placeholders(template_bytes: bytes) -> List[PlaceholderHit]:
    """Return all unique placeholders found in the template.

    De-duplicated by ``raw`` (the exact text including delimiters) so
    callers can ask the AI for one value per unique slot, then have it
    applied wherever that slot occurs.
    """
    doc = Document(BytesIO(template_bytes))
    seen: Dict[str, PlaceholderHit] = {}
    for paragraph in _iter_all_paragraphs(doc):
        text = paragraph.text
        if not text:
            continue
        # Delimited placeholders.
        for pattern, kind in PLACEHOLDER_PATTERNS:
            for m in pattern.finditer(text):
                raw = m.group(0)
                label = (m.group(1) or "").strip()
                if not label:
                    continue
                if raw not in seen:
                    seen[raw] = PlaceholderHit(raw=raw, label=label, kind=kind)
        # Underscore blanks (``___``) are intentionally NOT auto-filled.
        # They share raw text across the document so a single AI value
        # would clobber every occurrence regardless of its surrounding
        # label (e.g. ``____________`` appears for both "Place" and
        # "Date" with no way to distinguish at the raw-text level).
        # Lawyers fill these manually after download — the bracket-style
        # placeholders ([Name of Accused], [COURT NAME], …) cover the
        # auto-fillable slots cleanly.
    return list(seen.values())


def has_placeholders(template_bytes: bytes) -> bool:
    """Cheap check used to choose between filler and skeleton-clone paths."""
    return len(extract_placeholders(template_bytes)) > 0


# ---------------------------------------------------------------------------
# In-place replacement
# ---------------------------------------------------------------------------


def _set_run_text_with_breaks(run, text: str) -> None:
    """Set a run's text, converting ``\\n`` into proper ``<w:br/>`` elements.

    Preserves the run's formatting (font, bold, etc.) — only its inner
    ``<w:t>`` / ``<w:br>`` nodes are rewritten.
    """
    r = run._r
    # Strip existing text / break children only — keep rPr (formatting).
    for child in list(r):
        if child.tag in (qn("w:t"), qn("w:br"), qn("w:tab")):
            r.remove(child)

    lines = text.split("\n")
    first = True
    for line in lines:
        if not first:
            r.append(OxmlElement("w:br"))
        if line:
            t = OxmlElement("w:t")
            t.text = line
            t.set(qn("xml:space"), "preserve")
            r.append(t)
        first = False


def _replace_one_in_paragraph(paragraph: Paragraph, needle: str, value: str) -> bool:
    """Replace the first occurrence of ``needle`` in the paragraph.

    Handles the placeholder spanning multiple runs by concatenating runs
    to locate the match, then editing the start run's text (with line-
    break support) and clearing the overlap from any intermediate / end
    runs. Returns True if a replacement happened.
    """
    runs = paragraph.runs
    if not runs:
        return False
    full = "".join(r.text or "" for r in runs)
    if needle not in full:
        return False

    start = full.index(needle)
    end = start + len(needle)

    # Locate the start / end run indices and offsets.
    char_idx = 0
    start_run = None
    start_offset = 0
    end_run = None
    end_offset = 0
    for i, r in enumerate(runs):
        rlen = len(r.text or "")
        if start_run is None and char_idx + rlen > start:
            start_run = i
            start_offset = start - char_idx
        if char_idx + rlen >= end:
            end_run = i
            end_offset = end - char_idx
            break
        char_idx += rlen

    if start_run is None or end_run is None:
        return False

    if start_run == end_run:
        r = runs[start_run]
        before = (r.text or "")[:start_offset]
        after = (r.text or "")[end_offset:]
        if "\n" in value or before or after:
            # Use multi-line-safe setter when needed.
            _set_run_text_with_breaks(r, before + value + after)
        else:
            r.text = value
        return True

    # Multi-run case.
    first = runs[start_run]
    last = runs[end_run]
    before = (first.text or "")[:start_offset]
    after = (last.text or "")[end_offset:]

    _set_run_text_with_breaks(first, before + value + after_marker(after))
    # Clear intermediate runs.
    for i in range(start_run + 1, end_run):
        runs[i].text = ""
    # The "after" tail belongs on its own run for clarity — but since we
    # already appended it to the start run, blank the end run's content
    # that was part of the placeholder.
    last.text = ""
    return True


def after_marker(s: str) -> str:
    """No-op helper kept for readability at the call site."""
    return s


_MAX_REPLACEMENTS_PER_PARAGRAPH = 5


def _replace_all_in_paragraph(
    paragraph: Paragraph,
    needle: str,
    value: str,
) -> int:
    """Replace every non-overlapping occurrence of ``needle`` in the paragraph.

    Safety properties:

    - Replacements are found **before any mutation**, then applied
      right-to-left so positions stay stable and we never iterate over
      mutated text. This eliminates the runaway-replacement class of
      bugs where ``find`` appears inside ``replace`` (e.g. a poorly
      scoped ``find=" "`` with ``replace=" X "`` would otherwise
      explode into thousands of replacements).
    - A per-paragraph cap (``_MAX_REPLACEMENTS_PER_PARAGRAPH``)
      protects against pathological matches when a ``needle`` happens
      to occur many times (e.g. the LLM returned ``find="___"`` and
      the paragraph has a dozen underscore blanks). When the cap
      trips we log and skip the whole paragraph for that needle.
    """
    if not needle:
        return 0
    runs = paragraph.runs
    if not runs:
        return 0
    full = "".join(r.text or "" for r in runs)
    if needle not in full:
        return 0

    # Find all non-overlapping positions before any replacement.
    positions: List[int] = []
    start = 0
    while True:
        idx = full.find(needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + len(needle)  # non-overlapping next search

    if not positions:
        return 0
    if len(positions) > _MAX_REPLACEMENTS_PER_PARAGRAPH:
        logger.warning(
            "Suspicious edit: find={!r} matched {} times in one paragraph "
            "(cap={}). Skipping this paragraph for that edit.",
            needle[:40],
            len(positions),
            _MAX_REPLACEMENTS_PER_PARAGRAPH,
        )
        return 0

    # Apply right-to-left so earlier positions remain valid.
    applied = 0
    for pos in reversed(positions):
        if _replace_at_position(paragraph, pos, len(needle), value):
            applied += 1
    return applied


def _replace_at_position(
    paragraph: Paragraph,
    start: int,
    length: int,
    value: str,
) -> bool:
    """Replace ``length`` chars starting at ``start`` with ``value``.

    Run-boundary-aware: the start run gets the replacement appended in
    place, intermediate runs get their text cleared, and the end run
    keeps its trailing portion. Run formatting of the start run is
    preserved.
    """
    runs = paragraph.runs
    if not runs:
        return False
    end = start + length
    char_idx = 0
    start_run = None
    start_offset = 0
    end_run = None
    end_offset = 0
    for i, r in enumerate(runs):
        rlen = len(r.text or "")
        if start_run is None and char_idx + rlen > start:
            start_run = i
            start_offset = start - char_idx
        if char_idx + rlen >= end:
            end_run = i
            end_offset = end - char_idx
            break
        char_idx += rlen
    if start_run is None or end_run is None:
        return False

    if start_run == end_run:
        r = runs[start_run]
        before = (r.text or "")[:start_offset]
        after = (r.text or "")[end_offset:]
        if "\n" in value or (before or after):
            _set_run_text_with_breaks(r, before + value + after)
        else:
            r.text = value
        return True

    first = runs[start_run]
    last = runs[end_run]
    before = (first.text or "")[:start_offset]
    after = (last.text or "")[end_offset:]
    _set_run_text_with_breaks(first, before + value + after)
    for i in range(start_run + 1, end_run):
        runs[i].text = ""
    last.text = ""
    return True


def fill_template(
    template_bytes: bytes,
    values: Dict[str, str],
) -> bytes:
    """Return new DOCX bytes with placeholders replaced.

    ``values`` is a map of ``placeholder.raw`` (exact match text including
    delimiters) to the replacement value. Replacement runs across body
    paragraphs, table cells (including nested tables), headers, and
    footers.

    Placeholders not present in ``values`` are left untouched so the
    lawyer can fill them manually if the AI didn't know the answer.
    """
    if not values:
        # Nothing to do — return the bytes unchanged.
        return template_bytes

    doc = Document(BytesIO(template_bytes))

    # Apply longest needles first to avoid partial matches (e.g. "[Name]"
    # before "[Name of Accused]" would otherwise corrupt the longer one).
    ordered = sorted(values.items(), key=lambda kv: -len(kv[0]))
    paragraphs = list(_iter_all_paragraphs(doc))
    for needle, value in ordered:
        if not needle:
            continue
        replacement = value if value is not None else ""
        for paragraph in paragraphs:
            _replace_all_in_paragraph(paragraph, needle, replacement)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Catalog section detection + subsetting
# ---------------------------------------------------------------------------


# Document-type keywords that signal a numbered paragraph is a top-level
# section title (rather than a body list item like "1. I am a Citizen of
# India."). Tested case-insensitively against the title text.
_SECTION_KEYWORDS = re.compile(
    r"\b("
    r"Affidavit|Application|Petition|Notice|Statement|Declaration|"
    r"Agreement|Memorandum|Power\s+of\s+Attorney|Will|Undertaking|"
    r"Indemnity|Plaint|Reply|Rejoinder|Brief|Bond|Deed|Letter"
    r")\b",
    re.IGNORECASE,
)

_SECTION_HEADER = re.compile(r"^\s*(\d+)\s*[.)]\s+(.{2,150})\s*$")


@dataclass(frozen=True)
class TemplateSection:
    """One numbered top-level section detected in a catalog template."""

    number: int  # the "N" in "N. <Title>"
    title: str  # the <Title> portion, stripped
    start_para_idx: int  # body-paragraph index where this section begins
    end_para_idx: int  # inclusive index of the last paragraph in this section


# Section titles are short ("Only Child Affidavit"), not full sentences.
# Body lines that happen to contain "affidavit" (e.g. "9. That my statement
# is true and correct. This affidavit is also executed...") are much longer
# and must be filtered out.
_SECTION_TITLE_MAX_CHARS = 60


_SECTION_SUBTITLE_LOOKAHEAD = 5
_SECTION_SUBTITLE_PREFIX = re.compile(
    r"^(?:affidavit|application|petition|notice|statement|declaration|"
    r"agreement|memorandum|power\s+of\s+attorney|will|undertaking|"
    r"indemnity|deed|letter)\s+(?:for|of|in|to|by|under|on|regarding)\b",
    re.IGNORECASE,
)


def detect_template_sections(template_bytes: bytes) -> List[TemplateSection]:
    """Find numbered top-level sections in a catalog template.

    A section start is a numbered paragraph (``^N. <Title>``) with a
    short title (≤ 60 chars) AND a corroborating signal that this is a
    top-level section. We accept any of these signals:

    1. The title itself contains a document-type keyword (Affidavit,
       Application, …). This is the common case ("1. Only Child
       Affidavit").
    2. A nearby (within 5) following paragraph starts with
       "Affidavit for ...", "Application for ...", etc. — the
       structural sub-header pattern. This catches inconsistently
       named sections where the lawyer forgot the keyword in the
       title itself (e.g. "9. Second / Third Born Child" followed by
       "Affidavit for Second / Third Born Child").
    3. The paragraph is preceded within 3 lines by an empty paragraph
       AND followed within 3 lines by a centered paragraph — the
       visual section-break signature.

    After candidate collection, duplicates by number are dropped
    (keeping the earliest) so that "9." re-appearing later in a
    verification block doesn't fragment the catalog.
    """
    doc = Document(BytesIO(template_bytes))
    body_paragraphs = doc.paragraphs

    def _title_matches_section(idx: int, title: str) -> bool:
        # Signal 1: keyword in title.
        if _SECTION_KEYWORDS.search(title):
            return True
        # Signal 2: subtitle within lookahead window starts with
        # "Affidavit for …" / "Application for …" / etc.
        horizon = min(idx + 1 + _SECTION_SUBTITLE_LOOKAHEAD, len(body_paragraphs))
        for k in range(idx + 1, horizon):
            sub = (body_paragraphs[k].text or "").strip()
            if not sub:
                continue
            if _SECTION_SUBTITLE_PREFIX.match(sub):
                return True
            # Stop at the first non-empty, non-matching paragraph so
            # we don't over-reach into body text.
            break
        # Signal 3: visually distinct (centered subtitle nearby).
        for k in range(idx + 1, horizon):
            if (body_paragraphs[k].text or "").strip():
                try:
                    if body_paragraphs[k].alignment is not None and (
                        int(body_paragraphs[k].alignment) == 1  # WD_ALIGN.CENTER
                    ):
                        return True
                except Exception:
                    pass
                break
        return False

    candidates: List[Tuple[int, int, str]] = []
    for i, p in enumerate(body_paragraphs):
        text = (p.text or "").strip()
        if not text:
            continue
        m = _SECTION_HEADER.match(text)
        if not m:
            continue
        title = m.group(2).strip()
        if len(title) > _SECTION_TITLE_MAX_CHARS:
            continue
        if not _title_matches_section(i, title):
            continue
        try:
            number = int(m.group(1))
        except ValueError:
            continue
        candidates.append((i, number, title))

    if len(candidates) < 2:
        return []

    # Drop duplicate numbers (keep first occurrence). Numbering inside
    # an affidavit's body ("9. That my statement is …") is sometimes
    # short enough to slip through other filters, and earlier sections
    # always come first in the document.
    seen_numbers: set = set()
    deduped: List[Tuple[int, int, str]] = []
    for idx, number, title in candidates:
        if number in seen_numbers:
            continue
        deduped.append((idx, number, title))
        seen_numbers.add(number)

    if len(deduped) < 2:
        return []

    sections: List[TemplateSection] = []
    for j, (idx, number, title) in enumerate(deduped):
        end = (
            deduped[j + 1][0] - 1
            if j + 1 < len(deduped)
            else len(body_paragraphs) - 1
        )
        sections.append(TemplateSection(
            number=number, title=title,
            start_para_idx=idx, end_para_idx=end,
        ))
    return sections


def _strip_diacritics_lower(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def select_template_sections(
    sections: List[TemplateSection],
    requested_titles: List[str],
    requested_numbers: Optional[List[int]] = None,
) -> List[TemplateSection]:
    """Return the sections that should be kept in the output.

    Matching strategy:
    1. If ``requested_numbers`` is non-empty, match by exact section
       number first (most reliable when AI emits a "1.", "2." numbering
       in its markdown headings).
    2. For any remaining requested titles, fuzzy-match against section
       titles (case- and whitespace-insensitive substring match).

    Sections matched in either step are returned in the order they
    appear in the template (not in the order they were requested), so
    the output preserves the template's natural section ordering.
    """
    if not sections:
        return []

    matched: Dict[int, TemplateSection] = {}
    nums = set(requested_numbers or [])
    if nums:
        for s in sections:
            if s.number in nums:
                matched[s.number] = s

    if requested_titles:
        normalized_section_titles = [
            (s, _strip_diacritics_lower(s.title)) for s in sections
        ]
        for raw_title in requested_titles:
            needle = _strip_diacritics_lower(raw_title)
            if not needle:
                continue
            # Skip ones we already matched by number.
            best: Optional[TemplateSection] = None
            best_score = 0
            for s, snorm in normalized_section_titles:
                if s.number in matched:
                    continue
                # Substring match in either direction.
                if needle == snorm or needle in snorm or snorm in needle:
                    # Score: longer overlap wins.
                    score = min(len(needle), len(snorm))
                    if score > best_score:
                        best_score = score
                        best = s
            if best:
                matched[best.number] = best

    return sorted(matched.values(), key=lambda s: s.start_para_idx)


def fill_template_sectional(
    template_bytes: bytes,
    kept_sections: List[TemplateSection],
    values: Dict[str, str],
) -> bytes:
    """Drop unrequested sections then apply placeholder replacements.

    Paragraphs preserved:
    - Everything BEFORE the first detected section in the catalog
      (genuine pre-catalog preamble).
    - All paragraphs inside any section in ``kept_sections``.
    Paragraphs (and tables) in non-kept sections are removed from the
    body — body iteration walks both ``<w:p>`` and ``<w:tbl>``
    elements so tables in dropped sections don't orphan.

    Headers, footers, and section properties are left intact. After
    section-trimming, ``values`` are applied across the remaining
    document using the same in-place run-aware replacement as
    :func:`fill_template`.
    """
    if not kept_sections:
        return fill_template(template_bytes, values)

    doc = Document(BytesIO(template_bytes))

    # Pre-catalog preamble is "before the FIRST detected section". Use
    # the kept-sections list to know what to retain in-catalog, but
    # take the absolute first index from a fresh detection so dropping
    # section 1 doesn't leave its paragraphs masquerading as preamble.
    all_sections = detect_template_sections(template_bytes)
    if all_sections:
        first_section_start = min(s.start_para_idx for s in all_sections)
    else:
        first_section_start = min(s.start_para_idx for s in kept_sections)
    keep_ranges = [(s.start_para_idx, s.end_para_idx) for s in kept_sections]

    def _is_kept_para(idx: int) -> bool:
        if idx < first_section_start:
            return True
        return any(a <= idx <= b for (a, b) in keep_ranges)

    # Walk body.iterchildren() so tables get dropped alongside their
    # paragraphs. A table attaches to the section of the paragraph
    # immediately preceding it.
    body = doc.element.body
    para_counter = -1
    last_para_idx = -1
    for child in list(body.iterchildren()):
        tag = child.tag
        if tag == qn("w:sectPr"):
            continue
        if tag == qn("w:p"):
            para_counter += 1
            last_para_idx = para_counter
            if _is_kept_para(para_counter):
                continue
            body.remove(child)
        elif tag == qn("w:tbl"):
            attaches_to = last_para_idx
            if attaches_to == -1 or _is_kept_para(attaches_to):
                continue
            body.remove(child)

    if values:
        ordered = sorted(values.items(), key=lambda kv: -len(kv[0]))
        live_paragraphs = list(_iter_all_paragraphs(doc))
        for needle, value in ordered:
            if not needle:
                continue
            replacement = value if value is not None else ""
            for paragraph in live_paragraphs:
                _replace_all_in_paragraph(paragraph, needle, replacement)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def extract_template_text(template_bytes: bytes) -> str:
    """Return the template's visible text, one paragraph per line.

    Used to feed the template into the LLM-diff prompt. Empty paragraphs
    are preserved as blank lines because they often carry structural
    meaning (section breaks).
    """
    doc = Document(BytesIO(template_bytes))
    lines: List[str] = []
    for p in doc.paragraphs:
        lines.append(p.text or "")
    # Append table cell text too, separated so the LLM sees it but
    # paragraph-line counts stay aligned to body paragraphs.
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    lines.append(p.text or "")
    return "\n".join(lines)


# INDEX-table detection: first-row header keywords. If a table's first
# row's normalized text matches 2+ of these, we treat the table as an
# INDEX/contents table — kept structurally, excluded from text edits.
_INDEX_TABLE_KEYWORDS = (
    "s.no", "s. no", "s.n.", "sno", "sl.no", "sl no", "serial",
    "particulars", "description",
    "page no", "page no.", "pages", "page nos",
    "annexure", "exhibit",
)


def _is_index_table(table) -> bool:
    """Heuristic: does this table look like an INDEX/contents table?

    Matches the first row's normalized text against a list of common
    legal-pleading index column headers. Two or more matches → INDEX.
    """
    try:
        rows = table.rows
        if not rows:
            return False
        first_row_text = " | ".join(
            (cell.text or "").strip().lower() for cell in rows[0].cells
        )
    except Exception:
        return False
    if not first_row_text:
        return False
    matches = sum(1 for kw in _INDEX_TABLE_KEYWORDS if kw in first_row_text)
    return matches >= 2


def apply_template_diff(
    template_bytes: bytes,
    keep_sections: List[int],
    text_edits: List[Dict[str, str]],
) -> bytes:
    """Clone the template, drop unkept sections, then apply text edits.

    The LLM-guided render path. Combines:

    - **Catalog subsetting** (when ``keep_sections`` is non-empty):
      detect sections, retain only those whose numbers appear in
      ``keep_sections``, remove the rest from the body. Body
      iteration walks ``body.iterchildren()`` so both ``<w:p>`` and
      ``<w:tbl>`` elements are considered — tables that fall inside
      a dropped section are removed alongside their paragraphs;
      tables inside kept sections are preserved verbatim.
    - **Text edits**: each ``{"find": s, "replace": r}`` is applied as
      an exact-substring replacement using the run-aware machinery in
      :func:`_replace_all_in_paragraph`. INDEX-style tables (S.No /
      Particulars / Page No. headers) are kept structurally but
      excluded from the edit loop so AI-driven find/replace never
      corrupts the index. Edits whose ``find`` doesn't occur are
      silently skipped.

    Headers, footers, styles, and section properties are preserved.
    The function never raises on bad edits — it just does fewer
    replacements.
    """
    doc = Document(BytesIO(template_bytes))
    n_edits_in = len(text_edits) if text_edits else 0
    logger.info(
        "apply_template_diff: keep_sections={} text_edits={}",
        list(keep_sections) if keep_sections else [],
        n_edits_in,
    )

    # ----- Catalog subsetting -----
    removed_paragraphs = 0
    removed_tables = 0
    if keep_sections:
        sections = detect_template_sections(template_bytes)
        if sections:
            keep_set: set = set(keep_sections)
            kept = [s for s in sections if s.number in keep_set]
            logger.info(
                "Sections detected={} kept={} → {}",
                len(sections), len(kept),
                [(s.number, s.title) for s in kept],
            )
            if kept:
                # Preamble is whatever sits before the FIRST detected
                # section in the catalog — using the kept set here
                # would mistakenly preserve dropped-section content as
                # "preamble" when section 1 is dropped.
                first_section_start = min(s.start_para_idx for s in sections)
                keep_ranges = [(s.start_para_idx, s.end_para_idx) for s in kept]

                def _is_kept_para(idx: int) -> bool:
                    if idx < first_section_start:
                        return True  # genuine preamble preserved
                    return any(a <= idx <= b for (a, b) in keep_ranges)

                body = doc.element.body
                para_counter = -1
                last_para_idx = -1
                # Snapshot children so removal doesn't invalidate iteration.
                for child in list(body.iterchildren()):
                    tag = child.tag
                    if tag == qn("w:sectPr"):
                        continue
                    if tag == qn("w:p"):
                        para_counter += 1
                        last_para_idx = para_counter
                        if _is_kept_para(para_counter):
                            continue
                        body.remove(child)
                        removed_paragraphs += 1
                    elif tag == qn("w:tbl"):
                        # A table belongs to the section of the
                        # paragraph immediately preceding it. If there's
                        # no preceding paragraph (table at top) keep it
                        # as preamble. Otherwise drop iff the preceding
                        # paragraph's section was dropped.
                        attaches_to = last_para_idx
                        if attaches_to == -1 or _is_kept_para(attaches_to):
                            continue
                        body.remove(child)
                        removed_tables += 1
            logger.info(
                "Removed {} paragraphs and {} tables (kept sections={})",
                removed_paragraphs, removed_tables,
                [s.number for s in kept],
            )
        else:
            logger.info(
                "apply_template_diff: keep_sections requested but no "
                "catalog sections detected — leaving body intact"
            )

    # ----- Text edits -----
    edits_applied = 0
    zero_match: List[str] = []
    if text_edits:
        # Identify INDEX-table paragraphs so we can skip them during edits.
        index_table_para_ids: set = set()
        for t in doc.tables:
            if not _is_index_table(t):
                continue
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        index_table_para_ids.add(id(p._element))

        live_paragraphs = [
            p for p in _iter_all_paragraphs(doc)
            if id(p._element) not in index_table_para_ids
        ]
        if index_table_para_ids:
            logger.info(
                "Excluded {} INDEX-table paragraphs from edit loop",
                len(index_table_para_ids),
            )

        # Apply longest needles first so longer placeholders don't
        # get clobbered by shorter substrings that share a prefix.
        ordered = sorted(
            (
                (str(e.get("find") or ""), str(e.get("replace") or ""))
                for e in text_edits
                if isinstance(e, dict)
            ),
            key=lambda kv: -len(kv[0]),
        )
        for needle, value in ordered:
            if not needle:
                continue
            matches = 0
            for paragraph in live_paragraphs:
                matches += _replace_all_in_paragraph(paragraph, needle, value)
            if matches > 0:
                edits_applied += 1
            else:
                zero_match.append(needle)

        if zero_match:
            preview = ", ".join(repr(n[:60]) for n in zero_match[:5])
            logger.warning(
                "{} of {} text edits matched zero paragraphs — first few: {}",
                len(zero_match), n_edits_in, preview,
            )
        logger.info(
            "Edits applied: {}/{}", edits_applied, n_edits_in,
        )

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


_MARKDOWN_SECTION_NUM = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*(\d+)\s*[.)]\s+(.{2,150}?)\**\s*$",
    re.MULTILINE,
)


def extract_markdown_section_selections(
    markdown: str,
) -> Tuple[List[int], List[str]]:
    """Parse the AI's Markdown to find which numbered sections it produced.

    Matches headings or numbered top-level paragraphs like:

    - ``## 1. Only Child Affidavit``
    - ``**6. Only Girl Child Affidavit**``
    - ``8. First Born Child Affidavit``

    Returns ``(numbers, titles)``. Both lists are de-duplicated while
    preserving discovery order. Empty lists when no section markers
    look like top-level catalog selections.
    """
    numbers: List[int] = []
    titles: List[str] = []
    seen_n: set = set()
    seen_t: set = set()
    for m in _MARKDOWN_SECTION_NUM.finditer(markdown or ""):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        title = (m.group(2) or "").strip().rstrip("*").strip()
        if not title:
            continue
        # Skip if the line looks like a body list item: e.g. "1. I am a
        # citizen of India." — these don't contain document-type
        # keywords. Use the same keyword test as the template detector
        # so the two sides agree on what counts as a section.
        if not _SECTION_KEYWORDS.search(title):
            continue
        if n not in seen_n:
            numbers.append(n)
            seen_n.add(n)
        if title.lower() not in seen_t:
            titles.append(title)
            seen_t.add(title.lower())
    return numbers, titles
