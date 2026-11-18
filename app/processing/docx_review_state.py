"""Open tracked changes and comments in a .docx (SIM-219).

Before the drafting agent revises the lawyer's latest version of a draft, it
checks that the file has no open review state. A revision rewrites the whole
document, so it would silently accept every pending insertion and deletion
and drop every comment. The lawyer must settle them in the editor first.

The element list is the backend <-> AI contract ("open changes" check), and
both sides use it:

- parts: ``word/document.xml``, every ``word/header*.xml`` and
  ``word/footer*.xml``, ``word/footnotes.xml``, ``word/endnotes.xml``;
- tracked-change elements: ``w:ins w:del w:moveFrom w:moveTo w:rPrChange
  w:pPrChange w:sectPrChange w:tblPrChange w:trPrChange w:tcPrChange
  w:tblGridChange w:cellIns w:cellDel``;
- comments: every ``w:comment`` in ``word/comments.xml`` (a resolved comment
  still counts until ONLYOFFICE's marking of resolved comments is tested).

Pure: bytes in, counts out. No I/O, no logging of content. The XML is parsed
namespace-aware with a hardened parser (no entities, no network), and the
uncompressed size of the parts read is capped, so a zip bomb or a huge part
is reported as unreadable rather than expanded.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Optional

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

TRACKED_CHANGE_ELEMENTS = frozenset(
    f"{{{W_NS}}}{name}"
    for name in (
        "ins",
        "del",
        "moveFrom",
        "moveTo",
        "rPrChange",
        "pPrChange",
        "sectPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "tblGridChange",
        "cellIns",
        "cellDel",
    )
)
COMMENT_ELEMENT = f"{{{W_NS}}}comment"

DOCUMENT_PART = "word/document.xml"
COMMENTS_PART = "word/comments.xml"
_FIXED_PARTS = (DOCUMENT_PART, "word/footnotes.xml", "word/endnotes.xml")
_HEADER_FOOTER_RE = re.compile(r"^word/(?:header|footer)[^/]*\.xml$")

# Uncompressed bytes read across every part inspected. A real draft's parts
# are a few hundred KB; 50 MB is far beyond any legal document.
DEFAULT_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class ReviewState:
    """What the scan found. ``readable`` False means: do not trust the file."""

    readable: bool
    tracked_changes: int = 0
    comments: int = 0
    reason: Optional[str] = None  # why unreadable; a fixed code, never content

    @property
    def has_open_changes(self) -> bool:
        return self.readable and (self.tracked_changes > 0 or self.comments > 0)


class _TooLarge(Exception):
    pass


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        remove_comments=True,
        remove_pis=True,
    )


def _read_capped(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes:
    """Read one member, never more than ``budget`` bytes (the declared size
    in the zip header can lie, so the read itself is bounded too)."""
    if info.file_size > budget:
        raise _TooLarge()
    with zf.open(info) as fh:
        data = fh.read(budget + 1)
    if len(data) > budget:
        raise _TooLarge()
    return data


def scan_review_state(
    docx_bytes: bytes,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> ReviewState:
    """Count open tracked-change elements and comments in a .docx.

    Returns ``ReviewState(readable=False, reason=...)`` for anything that is
    not a readable Word document: not a zip, no ``word/document.xml``, a part
    over the size cap, or malformed XML in a part the check must read.
    """
    if not docx_bytes:
        return ReviewState(readable=False, reason="empty")
    try:
        zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except (zipfile.BadZipFile, ValueError, OSError):
        return ReviewState(readable=False, reason="not_a_zip")

    with zf:
        try:
            infos = {i.filename: i for i in zf.infolist() if not i.is_dir()}
        except Exception:
            return ReviewState(readable=False, reason="not_a_zip")
        if DOCUMENT_PART not in infos:
            return ReviewState(readable=False, reason="not_a_docx")

        change_parts = [p for p in _FIXED_PARTS if p in infos]
        change_parts += sorted(n for n in infos if _HEADER_FOOTER_RE.match(n))

        budget = max_uncompressed_bytes
        tracked = 0
        comments = 0
        try:
            for name in change_parts:
                data = _read_capped(zf, infos[name], budget)
                budget -= len(data)
                root = etree.fromstring(data, _parser())
                tracked += sum(1 for el in root.iter() if el.tag in TRACKED_CHANGE_ELEMENTS)
            if COMMENTS_PART in infos:
                data = _read_capped(zf, infos[COMMENTS_PART], budget)
                budget -= len(data)
                root = etree.fromstring(data, _parser())
                comments = sum(1 for _ in root.iter(COMMENT_ELEMENT))
        except _TooLarge:
            return ReviewState(readable=False, reason="too_large")
        except etree.XMLSyntaxError:
            return ReviewState(readable=False, reason="bad_xml")
        except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError, EOFError, ValueError):
            # Corrupt member, encrypted entry, truncated stream.
            return ReviewState(readable=False, reason="corrupt")

    return ReviewState(readable=True, tracked_changes=tracked, comments=comments)
