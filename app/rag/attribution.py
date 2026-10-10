"""
Source attribution for panel items.

Panels are generated from long prompts and come back as lists of statements —
a dated event, a weakness, a sentence of the case summary. Asking every one of
ten prompts to cite, and trusting each to comply, is fragile. Instead each item
is attributed *after* generation, deterministically, against the stored
evidence:

* the item's figures and dates must appear in the passage, or enough of its
  distinctive words must;
* the quote is copied verbatim from the stored passage — never generated;
* the page is resolved from the quote's own position (``--- Page N ---``).

Two corrections fall out of this:

* **Wrong source document.** list_of_dates maps the model's
  ``sourceDocumentIndex`` to a document with no check. When the item's date is
  absent from that document but present in another, the source is re-pointed.
* **Guessed page numbers.** The page is replaced by the page the quote is on.

An item that matches no passage well enough simply gets no citation: a missing
citation is honest, a wrong one is not.
"""

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.rag.citations import (
    _best_quote,
    _figures,
    _normalised,
    contains_figure,
    sheet_location,
)
from app.rag.evidence import EvidencePack
from app.rag.sparse import tokens

#: Minimum share of an item's distinctive words found in a passage, when the
#: item carries no figure or date to anchor it.
_MIN_OVERLAP = 0.45
_MIN_WORDS = 3
_CACHE_TTL_SECONDS = 120.0
_cache: Dict[str, Tuple[float, int, "Attributor"]] = {}


class Attributor:
    """Matches statements to stored evidence passages for one workspace."""

    def __init__(self, chunks: List[Dict[str, Any]], annexure_of: Dict[str, int], names: Dict[str, str]):
        self.pack = EvidencePack()
        self.items = []
        for chunk in chunks:
            did = str(chunk.get("original_document_id") or "")
            item = self.pack.add_passage(
                chunk, annexure=annexure_of.get(did), file_name=names.get(did) or None
            )
            if item is not None:
                self.items.append(
                    (item, _normalised(item.text), set(tokens(item.text)))
                )

    def locate(
        self, text: str, *, document_id: Optional[str] = None, anchor: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Best-supported citation for ``text``, or None.

        ``anchor`` (e.g. the ISO date of a chronology entry, or its original
        date string) must appear in the passage when given. ``document_id``
        restricts the search to that document first; if nothing there
        supports the item, the other documents are tried.
        """
        claim = (text or "").strip()
        if not claim or not self.items:
            return None
        figs = _figures(claim + " " + anchor)
        words = {t for t in tokens(claim) if len(t) > 3 and not t.isdigit()}
        best: Optional[Tuple[float, Any]] = None
        for restrict in ([document_id] if document_id else []) + [None]:
            for item, norm, toks in self.items:
                if restrict and item.document_id != restrict:
                    continue
                fig_hits = sum(1 for f in figs if contains_figure(norm, f))
                overlap = len(words & toks) / len(words) if words else 0.0
                if figs and fig_hits == 0:
                    continue
                if not figs and (len(words) < _MIN_WORDS or overlap < _MIN_OVERLAP):
                    continue
                score = fig_hits * 5 + overlap * 10
                if best is None or score > best[0]:
                    best = (score, item)
            if best is not None:
                break
        if best is None:
            return None
        item = best[1]
        quote, at = _best_quote(item, claim + " " + anchor)
        cite = {
            "type": "document",
            "documentId": item.document_id,
            "annexure": item.annexure,
            "fileName": item.file_name,
            "page": item.page_at(at),
            "timestamp": item.time_start,
            "quote": quote or None,
        }
        if item.sheet:
            # Spreadsheets have no pages: the sheet and exact row locate it.
            cite.update(sheet_location(item, quote, at))
        return cite


async def attributor_for(workspace_id: str) -> Optional[Attributor]:
    """A (briefly cached) attributor over the workspace's current evidence."""
    from app.vectorstore.qdrant import vector_store

    version = vector_store.evidence_version(workspace_id)
    cached = _cache.get(workspace_id)
    if cached and cached[1] == version and time.time() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[2]
    chunks = await vector_store.get_document_chunks(workspace_id)
    if not chunks:
        return None
    docs = await vector_store.get_all_documents(workspace_id)
    annexure_of = {d["document_id"]: i for i, d in enumerate(docs, 1)}
    names = {d["document_id"]: d.get("filename") or "" for d in docs}
    attributor = Attributor(chunks, annexure_of, names)
    _cache[workspace_id] = (time.time(), version, attributor)
    return attributor


def _item_text(item: Any, *keys: str) -> str:
    if isinstance(item, dict):
        return " ".join(str(item.get(k) or "") for k in keys).strip()
    return str(item or "")


def _attribute_dated(entries: Any, attributor: Attributor) -> int:
    """list_of_dates / procedural_timeline entries: snippet, page, citation,
    and a corrected source document when the claimed one lacks the date."""
    count = 0
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        text = _item_text(entry, "title", "description")
        anchor = str(entry.get("date") or "")
        claimed = str(entry.get("sourceDocId") or "") or None
        cite = attributor.locate(text, document_id=claimed, anchor=_date_forms(anchor))
        if not cite:
            continue
        if claimed and cite["documentId"] != claimed:
            logger.info(
                "Chronology entry {!r} re-attributed: {}  {} (its date is not in the "
                "claimed source)",
                entry.get("title"),
                claimed,
                cite["documentId"],
            )
            entry["sourceDocId"] = cite["documentId"]
            if cite.get("fileName"):
                entry["sourceDocName"] = entry["source"] = entry["sourceAnswer"] = cite["fileName"]
        if cite.get("page"):
            entry["pageNumber"] = cite["page"]
        if cite.get("quote"):
            entry["evidenceSnippet"] = cite["quote"]
        entry["citation"] = cite
        count += 1
    return count


def _date_forms(iso: str) -> str:
    """Common written forms of an ISO date, so a passage that says
    12/03/2024 or 12.03.2024 anchors an entry dated 2024-03-12. The ISO form
    itself is included: spreadsheet cells are written that way, and without it
    no chronology entry taken from a sheet could ever be cited."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso.strip())
    if not m:
        return iso
    y, mo, d = m.groups()
    return " ".join(sorted({f"{y}-{mo}-{d}", f"{d}/{mo}/{y}", f"{d}.{mo}.{y}", f"{d}-{mo}-{y}",
                            f"{int(d)}/{int(mo)}/{y}", f"{int(d)}.{int(mo)}.{y}"}))


def _attribute_statements(items: Any, attributor: Attributor, *keys: str) -> int:
    count = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue  # a bare string cannot carry a citation without changing its type
        cite = attributor.locate(_item_text(item, *keys))
        if cite:
            item["citations"] = [cite]
            count += 1
    return count


def _summary_citations(summary: str, attributor: Attributor) -> List[Dict[str, Any]]:
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", summary or ""):
        sentence = sentence.strip()
        if len(sentence) < 25:
            continue
        cite = attributor.locate(sentence)
        if cite:
            out.append({"sentence": sentence, **cite})
    return out


async def attach_panel_citations(workspace_id: str, feature_type: str, payload: Any) -> int:
    """Attach verified citations to one panel payload, in place.

    Additive only: new keys (``citation``, ``citations``, ``evidenceSnippet``,
    ``caseSummaryCitations``) and corrected ``pageNumber`` / source fields.
    Returns the number of items attributed. Never raises.
    """
    if not isinstance(payload, dict):
        return 0
    try:
        attributor = await attributor_for(workspace_id)
        if attributor is None:
            return 0
        if feature_type == "list_of_dates":
            return _attribute_dated(payload.get("listOfDates"), attributor)
        if feature_type == "procedural_timeline":
            return _attribute_dated(payload.get("proceduralTimeline"), attributor)
        if feature_type == "analysis_report":
            n = 0
            for key in ("weaknesses", "risks"):
                n += _attribute_statements(payload.get(key), attributor, "text")
            n += _attribute_statements(payload.get("strengths"), attributor, "point", "text")
            return n
        if feature_type == "strategy_objective":
            cites = _summary_citations(str(payload.get("caseSummary") or ""), attributor)
            if cites:
                payload["caseSummaryCitations"] = cites
            return len(cites)
        if feature_type == "applicable_laws":
            return _attribute_statements(
                payload.get("applicableLaws") or payload.get("laws"), attributor, "description"
            )
    except Exception as err:
        logger.warning("Panel citation attach failed for {} (non-fatal): {}", feature_type, err)
    return 0
