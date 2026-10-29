"""
Citation checking, repair and numbering.

The model is told to cite evidence labels (``[E3]``, ``[C1]``, ``[S2]``,
``[T1]`` for a spreadsheet calculation, ``[K1]`` for a knowledge-base passage) next to the claims they support. Nothing it writes is trusted until checked here:

1. **Invented labels are removed.** A label that is not in the evidence pack
   names nothing; it is dropped rather than shown as a citation.
2. **Figures are checked against the cited passage.** Every number, amount and
   date in a sentence that carries a document citation must appear in at least
   one passage it cites (compared after normalising digit grouping, so
   ``6,00,000`` matches ``600000``, and only as a whole number: ``15.5`` is
   not found inside ``115.5``). If the figure is in a *different* passage,
   the citation is re-pointed there. If it is in no passage at all, the
   sentence is reported as unsupported.
3. **Uncited figures are cited.** A sentence stating a figure found in exactly
   one passage gets that passage's citation.
4. **Numbering.** Labels are renumbered ``[1]``, ``[2]``… in order of first
   appearance, and a structured citation list is built: document, annexure,
   page (resolved from the quote's own position, not just the chunk's first
   page), a verbatim quote, or the URL for case laws and statutes.

This module is deterministic — no model calls. The caller decides whether an
unsupported claim warrants a regeneration.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

from app.rag.evidence import CHECKABLE_KINDS, EvidenceItem, EvidencePack

_LABEL_GROUP_RE = re.compile(r"\[((?:[ECSTK]\d+)(?:\s*[,;]\s*[ECSTK]?\d+)*)\]")
_LABEL_RE = re.compile(r"([ECSTK])(\d+)")
# Dates first: otherwise "12/03/2024" is read as three separate numbers and
# every document from 2024 "supports" it. ISO dates (spreadsheet cells) too:
# read as numbers, only their year was ever checked.
_FIGURE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
    r"|(?:₹|rs\.?\s*|inr\s*)?\d[\d,]*(?:\.\d+)?",
    re.I,
)
#: Abbreviations whose full stop does not end a sentence.
_ABBREV_RE = re.compile(
    r"\b(no|nos|rs|sr|jr|mr|mrs|ms|dr|smt|shri|vs|v|u/s|sec|s|ss|art|cl|para|paras|p|pp|vol|ltd|pvt|co|hon'ble|ld|st|nd|rd|th|viz|etc|i\.e|e\.g)\.",
    re.I,
)
_HOLD = "\u2024"  # one-dot leader: stands in for a protected full stop


def _sentences(text: str):
    """(start, sentence) pairs covering ``text`` exactly; full stops after legal
    abbreviations and inside numbers (6.5, 2.1) do not end a sentence.

    Every character lands in some sentence, so joining them rebuilds the
    text: a line break straight after a full stop ("... [E2].\n* Row 3") is a
    sentence of its own. Requiring one non-break character dropped those
    breaks, which ran bullet lists and headings into one paragraph.
    """
    protected = _ABBREV_RE.sub(lambda m: m.group(0)[:-1] + _HOLD, text)
    protected = re.sub(r"(?<=\d)\.(?=\d)", _HOLD, protected)
    for m in re.finditer(r"[^.!?\n]*(?:[.!?]+|\n|$)", protected):
        if m.group(0):
            yield m.start(), text[m.start() : m.end()]
_WORD_RE = re.compile(r"[a-z0-9]+")
_QUOTE_MAX = 240


def _norm_figure(raw: str) -> str:
    return re.sub(r"[^\d./-]", "", raw.replace(",", "")).strip("./-")


def _figures(text: str) -> Set[str]:
    out = set()
    for m in _FIGURE_RE.finditer(text):
        # Leave out citation labels and page/section-sized small integers that
        # are too common to prove anything ("section 3", "page 2").
        val = _norm_figure(m.group(0))
        digits = re.sub(r"\D", "", val)
        if len(digits) >= 3 or re.search(r"[./-]", val):
            out.add(val)
    return out


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(",", "")).lower()


@lru_cache(maxsize=4096)
def _figure_pattern(fig: str) -> "re.Pattern[str]":
    """``fig`` as a whole number: never inside a longer one ("15.5" is not in
    "115.5"), and equal by value across trailing decimal zeros ("10547271.00"
    is "10547271", "15.50" is "15.5"), the way amounts are written. Dates and
    other multi-separator figures match literally. "rs.600000" still matches
    600000."""
    decimal = re.fullmatch(r"(\d+)\.(\d*?)0*", fig)
    if decimal:
        whole, frac = decimal.groups()
        core = re.escape(whole) + (r"\." + re.escape(frac) + r"0*" if frac else r"(?:\.0*)?")
    elif fig.isdigit():
        core = re.escape(fig) + r"(?:\.0+)?"
    else:
        core = re.escape(fig)
    return re.compile(r"(?<!\d)(?<!\d\.)" + core + r"(?!\d)(?!\.\d)")


def contains_figure(normalised_text: str, fig: str) -> bool:
    """Whether a normalised passage states ``fig`` as a whole number or date."""
    # Cheap substring pre-check on the integer part before the regex.
    return fig.split(".", 1)[0] in normalised_text and bool(
        _figure_pattern(fig).search(normalised_text)
    )


@dataclass
class CitationReport:
    invalid_labels: List[str] = field(default_factory=list)
    repointed: List[Tuple[str, str, str]] = field(default_factory=list)  # figure, from, to
    added: List[Tuple[str, str]] = field(default_factory=list)  # figure, label
    unsupported: List[Tuple[str, List[str]]] = field(default_factory=list)  # sentence, figures

    @property
    def clean(self) -> bool:
        return not self.unsupported


def _split_labels(group: str) -> List[str]:
    labels, prefix = [], "E"
    for part in re.split(r"\s*[,;]\s*", group):
        m = _LABEL_RE.fullmatch(part.strip()) or re.fullmatch(r"(\d+)", part.strip())
        if not m:
            continue
        if len(m.groups()) == 2:
            prefix, num = m.group(1), m.group(2)
        else:
            num = m.group(1)
        labels.append(f"{prefix}{num}")
    return labels


def _best_quote(item: EvidenceItem, claim: str) -> Tuple[str, int]:
    """The passage sentence sharing most words with the claim, and its offset."""
    claim_words = set(_WORD_RE.findall(claim.lower()))
    claim_figs = _figures(claim)
    best, best_score, best_at = "", -1.0, 0
    for start, raw in _sentences(item.text):
        sent = raw.strip()
        if len(sent) < 12 or sent.startswith("--- Page"):
            continue
        words = set(_WORD_RE.findall(sent.lower()))
        score = len(words & claim_words) + 3 * len(_figures(sent) & claim_figs)
        if score > best_score:
            best, best_score, best_at = sent, score, start
    if len(best) > _QUOTE_MAX:
        best = best[: _QUOTE_MAX - 1].rstrip() + "…"
    return best, best_at


def _clip(text: str) -> Optional[str]:
    text = (text or "").strip()
    if len(text) > _QUOTE_MAX:
        text = text[: _QUOTE_MAX - 1].rstrip() + "…"
    return text or None


def sheet_location(item: EvidenceItem, quote: str, at: int) -> Dict[str, Any]:
    """Citation fields for a quote from a spreadsheet passage: the sheet, the
    exact row the quoted line is, and the quote prefixed with the column
    header so "Player 2 | 14.0 | FALSE" says what its cells are."""
    row = item.row_at(at)
    out: Dict[str, Any] = {
        "sheet": item.sheet,
        "rowStart": row or item.row_start,
        "rowEnd": row or item.row_end,
    }
    header = item.header_line(at)
    if quote and header and quote.strip() != header.strip():
        out["quote"] = _clip(f"{header} — {quote}")
    return out


def verify_and_number(
    answer: str, pack: EvidencePack
) -> Tuple[str, List[Dict[str, Any]], CitationReport]:
    """Check, repair and number the citations in ``answer``.

    Returns ``(text_with_[n]_markers, citations, report)``.
    """
    report = CitationReport()
    if not answer:
        return answer, [], report
    docs = list(pack.checkable())
    doc_text_norm = {i.label: _normalised(i.text) for i in docs}

    def figure_home(fig: str) -> List[str]:
        return [lbl for lbl, txt in doc_text_norm.items() if contains_figure(txt, fig)]

    # --- sentence by sentence: validate labels, check figures, repair
    rebuilt: List[str] = []
    claim_for_label: Dict[str, str] = {}
    for _start, sentence in _sentences(answer):
        labels: List[str] = []
        for g in _LABEL_GROUP_RE.finditer(sentence):
            for lbl in _split_labels(g.group(1)):
                if lbl in pack.items:
                    labels.append(lbl)
                else:
                    report.invalid_labels.append(lbl)
        labels = list(dict.fromkeys(labels))
        bare = re.sub(r"[ \t]+([.,;:!?])", r"\1", _LABEL_GROUP_RE.sub("", sentence))
        doc_labels = [l for l in labels if pack.items[l].kind in CHECKABLE_KINDS]
        figs = _figures(bare)
        for fig in sorted(figs):
            if doc_labels and any(contains_figure(doc_text_norm.get(l, ""), fig) for l in doc_labels):
                continue
            homes = figure_home(fig)
            if homes:
                target = homes[0]
                if doc_labels:
                    report.repointed.append((fig, ",".join(doc_labels), target))
                else:
                    report.added.append((fig, target))
                if target not in labels:
                    labels.append(target)
            elif doc_labels or docs:
                report.unsupported.append((bare.strip(), [fig]))
        for lbl in labels:
            claim_for_label.setdefault(lbl, bare)
        # Re-emit the sentence with its (validated, repaired) labels at the end.
        body = bare.rstrip()
        trail = sentence[len(sentence.rstrip()):]
        if labels:
            # Keep terminal punctuation after the markers: "... paid [1]."
            punct = ""
            while body and body[-1] in ".!?":
                punct = body[-1] + punct
                body = body[:-1]
            body = body.rstrip() + " " + "".join(f"[{l}]" for l in labels) + punct
        rebuilt.append(body + trail)
    text = "".join(rebuilt)

    # --- renumber by first appearance and build the citation list
    order: List[str] = []
    for g in _LABEL_GROUP_RE.finditer(text):
        for lbl in _split_labels(g.group(1)):
            if lbl in pack.items and lbl not in order:
                order.append(lbl)
    number = {lbl: i for i, lbl in enumerate(order, 1)}

    def _renumber(mm: re.Match) -> str:
        nums = [number[l] for l in _split_labels(mm.group(1)) if l in number]
        return "".join(f"[{n}]" for n in dict.fromkeys(nums))

    text = _LABEL_GROUP_RE.sub(_renumber, text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)

    citations: List[Dict[str, Any]] = []
    for lbl in order:
        item = pack.items[lbl]
        entry: Dict[str, Any] = {"index": number[lbl], "type": item.kind}
        if item.kind == "computed":
            # Shown as a document citation (the workbook it was computed from)
            # so existing clients render it; ``computed`` marks what it is.
            entry.update(
                {
                    "type": "document",
                    "documentId": item.document_id,
                    "annexure": item.annexure,
                    "fileName": item.file_name,
                    "sheet": item.sheet,
                    "rowStart": item.row_start,
                    "rowEnd": item.row_end,
                    "computed": True,
                    "quote": _clip(item.text),
                }
            )
        elif item.kind == "document":
            quote, at = _best_quote(item, claim_for_label.get(lbl, ""))
            entry.update(
                {
                    "documentId": item.document_id,
                    "annexure": item.annexure,
                    "fileName": item.file_name,
                    "page": item.page_at(at),
                    "pageEnd": item.page_end,
                    "timestamp": item.time_start,
                    "heading": item.heading,
                    "quote": quote or None,
                }
            )
            if item.sheet:
                entry.update(sheet_location(item, quote, at))
        elif item.kind == "knowledge_base":
            # Shared reference material, named by its admin-written title;
            # never presented as one of the case's documents.
            quote, at = _best_quote(item, claim_for_label.get(lbl, ""))
            entry.update(
                {
                    "documentId": item.document_id,
                    "title": item.title,
                    "category": item.category,
                    "page": item.page_at(at),
                    "pageEnd": item.page_end,
                    "heading": item.heading,
                    "quote": quote or None,
                }
            )
            if item.sheet:
                entry.update(sheet_location(item, quote, at))
        else:
            entry.update({"title": item.title, "url": item.url, "quote": item.text[:_QUOTE_MAX] or None})
        citations.append(entry)
    return text, citations, report
