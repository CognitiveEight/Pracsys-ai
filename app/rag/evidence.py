"""
The evidence pack: every passage a prompt may rely on, under a stable label.

A citation is only as good as the thing it points at. Before this, chat context
was each document's first 40,000 characters under an "Annexure N" header, so
the most an answer could say was "see Annexure 2" — no page, no passage, and
nothing to check the claim against.

Each passage now gets a label the model must cite verbatim:

* ``E1…En`` — a passage of an uploaded document (chunk text, page range,
  annexure number, file name);
* ``C1…Cn`` — a case law from the Case Laws panel (title, citation, URL);
* ``S1…Sn`` — a statutory provision from Applicable Laws (act, section, URL);
* ``T1…Tn`` — a calculation the system computed over every row of a
  spreadsheet (app/rag/table_query.py): a count, total or filtered list, with
  the document, sheet and rows it covers;
* ``K1…Kn`` — a passage of the shared knowledge base (app/rag/knowledge_base.py):
  legal reference material, never one of the case's documents, cited by its
  admin-written title.

The pack is serialised into the chat state (``to_state``) so the citation
checker can map every label back to what it names, and so a label that is not
in the pack is recognised as invented.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

_PAGE_MARKER_RE = re.compile(r"---\s*Page\s+(\d+)\s*---", re.I)

#: Kinds whose text the citation checker reads figures from.
CHECKABLE_KINDS = ("document", "computed", "knowledge_base")


@dataclass
class EvidenceItem:
    label: str
    kind: str  # "document" | "case_law" | "statute" | "computed" | "knowledge_base"
    text: str
    document_id: Optional[str] = None
    annexure: Optional[int] = None
    file_name: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    chunk_id: Optional[str] = None
    heading: Optional[str] = None
    time_start: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    # Spreadsheet passages (and computed results): sheet, row range, and the
    # row of each line as [offset_in_passage, row, sheet].
    sheet: Optional[str] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    row_offsets: Optional[List[List[Any]]] = None
    #: Knowledge-base passages: the shelf category (judgment, statute, ...).
    category: Optional[str] = None

    def row_at(self, offset: int) -> Optional[int]:
        """Spreadsheet row of a character offset inside this passage."""
        row = None
        for entry in self.row_offsets or []:
            if entry[0] > offset:
                break
            row = entry[1]
        return row

    def header_line(self, offset: Optional[int] = None) -> Optional[str]:
        """The column header row governing ``offset`` in a spreadsheet passage:
        the line right after the last "Sheet: …" label before it, unless that
        line is itself a row (a block without a header). Lets a one-row quote
        say what its cells mean."""
        if not self.sheet:
            return None
        row_starts = {entry[0] for entry in self.row_offsets or []}
        limit = len(self.text) if offset is None else offset
        header = None
        pos = 0
        for line in self.text.split("\n"):
            if pos > limit:
                break
            if line.startswith("Sheet: "):
                nxt_at = pos + len(line) + 1
                end = self.text.find("\n", nxt_at)
                nxt = self.text[nxt_at : end if end != -1 else len(self.text)]
                header = nxt if nxt and nxt_at not in row_starts else None
            pos += len(line) + 1
        return header

    def page_at(self, offset: int) -> Optional[int]:
        """Physical page of a character offset inside this passage: the last
        page marker before it, else the passage's first page."""
        page = self.page_start
        for m in _PAGE_MARKER_RE.finditer(self.text):
            if m.start() > offset:
                break
            page = int(m.group(1))
        return page


@dataclass
class EvidencePack:
    items: Dict[str, EvidenceItem] = field(default_factory=dict)
    #: Knowledge-graph facts for the prompt, each ending in the [E#] label of
    #: the passage it was extracted from (see app/rag/chat_context.py).
    graph_facts: List[str] = field(default_factory=list)
    #: Prompt notes from spreadsheet calculations: each result line with its
    #: [T#] label, or a statement that a calculation could not be made.
    table_notes: List[str] = field(default_factory=list)
    _counters: Dict[str, int] = field(
        default_factory=lambda: {"E": 0, "C": 0, "S": 0, "T": 0, "K": 0}
    )

    def _next(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}{self._counters[prefix]}"

    def add_passage(
        self,
        chunk: Dict[str, Any],
        *,
        annexure: Optional[int],
        file_name: Optional[str],
        drop_overlap: bool = False,
    ) -> Optional[EvidenceItem]:
        """Add a stored chunk (Qdrant payload dict). ``drop_overlap`` strips the
        characters the chunk repeats from its predecessor — used when whole
        documents are laid out passage after passage."""
        meta = chunk.get("metadata") or {}
        text = str(chunk.get("content") or "")
        if drop_overlap and isinstance(meta.get("overlap_prev"), int):
            text = text[meta["overlap_prev"]:]
        if not text.strip():
            return None
        item = EvidenceItem(
            label=self._next("E"),
            kind="document",
            text=text.strip(),
            document_id=str(chunk.get("original_document_id") or meta.get("original_document_id") or ""),
            annexure=annexure,
            file_name=file_name or meta.get("filename"),
            page_start=meta.get("page_start"),
            page_end=meta.get("page_end"),
            chunk_id=chunk.get("document_id"),
            heading=meta.get("heading"),
            time_start=meta.get("time_start"),
            sheet=meta.get("sheet"),
            row_start=meta.get("row_start"),
            row_end=meta.get("row_end"),
            row_offsets=_shift_rows(
                meta.get("row_offsets"),
                len(str(chunk.get("content") or "")) - len(text.lstrip()),
            ),
        )
        self.items[item.label] = item
        return item

    def add_computed(
        self,
        text: str,
        *,
        document_id: str,
        annexure: Optional[int],
        file_name: Optional[str],
        sheet: str,
        row_start: Optional[int],
        row_end: Optional[int],
    ) -> EvidenceItem:
        """A result computed by the system over a spreadsheet's rows."""
        item = EvidenceItem(
            label=self._next("T"),
            kind="computed",
            text=text,
            document_id=document_id,
            annexure=annexure,
            file_name=file_name,
            sheet=sheet,
            row_start=row_start,
            row_end=row_end,
        )
        self.items[item.label] = item
        return item

    def add_knowledge_base(self, chunk: Dict[str, Any]) -> Optional[EvidenceItem]:
        """A passage of the shared knowledge base (a stored shelf chunk)."""
        meta = chunk.get("metadata") or {}
        text = str(chunk.get("content") or "").strip()
        if not text:
            return None
        item = EvidenceItem(
            label=self._next("K"),
            kind="knowledge_base",
            text=text,
            document_id=str(
                chunk.get("original_document_id") or meta.get("original_document_id") or ""
            ),
            title=str(meta.get("title") or "").strip() or None,
            category=meta.get("category"),
            page_start=meta.get("page_start"),
            page_end=meta.get("page_end"),
            chunk_id=chunk.get("document_id"),
            heading=meta.get("heading"),
            sheet=meta.get("sheet"),
            row_start=meta.get("row_start"),
            row_end=meta.get("row_end"),
            row_offsets=_shift_rows(
                meta.get("row_offsets"),
                len(str(chunk.get("content") or "")) - len(str(chunk.get("content") or "").lstrip()),
            ),
        )
        self.items[item.label] = item
        return item

    def add_case_law(self, case: Dict[str, Any]) -> EvidenceItem:
        title = str(case.get("title") or "").strip()
        citation = str(case.get("citation") or "").strip()
        item = EvidenceItem(
            label=self._next("C"),
            kind="case_law",
            text=" — ".join(p for p in [title, citation, str(case.get("summary") or "").strip()] if p),
            url=case.get("sourceUrl") or case.get("referenceUrl"),
            title=" — ".join(p for p in [title, citation] if p) or None,
        )
        self.items[item.label] = item
        return item

    def add_statute(self, law: Dict[str, Any]) -> EvidenceItem:
        act = str(law.get("act") or "").strip()
        section = str(law.get("section") or "").strip()
        title = str(law.get("title") or "").strip() or " ".join(p for p in [act, section] if p)
        item = EvidenceItem(
            label=self._next("S"),
            kind="statute",
            text=" — ".join(p for p in [title, str(law.get("description") or "").strip()] if p),
            url=law.get("sourceUrl"),
            title=title or None,
        )
        self.items[item.label] = item
        return item

    def documents(self) -> Iterable[EvidenceItem]:
        return (i for i in self.items.values() if i.kind == "document")

    def checkable(self) -> Iterable[EvidenceItem]:
        """Items whose figures the citation checker verifies: document passages,
        computed spreadsheet results and knowledge-base passages."""
        return (i for i in self.items.values() if i.kind in CHECKABLE_KINDS)

    def to_state(self) -> Dict[str, Dict[str, Any]]:
        return {label: asdict(item) for label, item in self.items.items()}

    @classmethod
    def from_state(cls, state: Optional[Dict[str, Dict[str, Any]]]) -> "EvidencePack":
        pack = cls()
        for label, data in (state or {}).items():
            try:
                pack.items[label] = EvidenceItem(**data)
            except TypeError:
                continue
            prefix, num = label[0], label[1:]
            if num.isdigit():
                pack._counters[prefix] = max(pack._counters.get(prefix, 0), int(num))
        return pack


def _shift_rows(row_offsets: Any, dropped: int) -> Optional[List[List[Any]]]:
    """Row offsets re-based after ``dropped`` leading characters were removed
    (overlap, whitespace). Spreadsheet chunks carry no overlap, so this is
    normally zero."""
    if not isinstance(row_offsets, list):
        return None
    out = []
    for entry in row_offsets:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[0] - dropped >= 0:
            out.append([entry[0] - dropped, *entry[1:]])
    return out or None


def location_label(item: EvidenceItem) -> str:
    """Where a passage is: pages, an audio timestamp, or a sheet and rows."""
    where = []
    if item.page_start:
        where.append(
            f"p. {item.page_start}"
            if not item.page_end or item.page_end == item.page_start
            else f"pp. {item.page_start}–{item.page_end}"
        )
    if item.time_start:
        where.append(f"at {item.time_start}")
    if item.sheet:
        rows = ""
        if item.row_start:
            rows = (
                f", row {item.row_start}"
                if not item.row_end or item.row_end == item.row_start
                else f", rows {item.row_start}–{item.row_end}"
            )
        where.append(f'sheet "{item.sheet}"{rows}')
    return ", ".join(where)


def render_document_block(items: List[EvidenceItem]) -> str:
    """Passages of ONE document, each preceded by its label and location."""
    out: List[str] = []
    for item in items:
        where = location_label(item)
        suffix = f" ({where})" if where else ""
        out.append(f"[{item.label}]{suffix}\n{item.text}")
    return "\n\n".join(out)
