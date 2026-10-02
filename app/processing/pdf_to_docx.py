"""PDF→DOCX conversion for template ingest.

Lawyers upload templates as both ``.docx`` and ``.pdf``. The drafting
assistant's high-fidelity ``llm_diff`` render path can only edit DOCX
bytes — it has no PDF-editing equivalent. To make PDF templates work
under the same pipeline, we convert at ingest time and store the
resulting DOCX as the editable skeleton.

The conversion is best-effort with a three-tier fallback:

1. **pdf2docx** — fast, preserves text-PDF layout reasonably well. The
   primary path for digitally-authored PDFs (e.g. notices typed in
   Word and exported to PDF).
2. **OCR-text → synthesized DOCX** — when tier 1 fails or produces an
   empty document (common for Adobe-Scan image PDFs), fall back to the
   project's existing PDF OCR pipeline (``FileProcessor
   .extract_text_from_pdf`` already routes through Google Vision when
   raw text extraction yields nothing) and synthesize a standard-legal
   DOCX from the OCR'd text.
3. **Give up** — return ``None``. The caller (``ingest_template``)
   logs and falls back to the non-templated draft path.

All helpers are synchronous so callers should invoke them via
``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import tempfile
from io import BytesIO
from typing import Optional

from loguru import logger


_MIN_USEFUL_TEXT_CHARS = 50  # below this, tier-1 output is considered empty


def convert_pdf_to_docx(pdf_bytes: bytes, filename: str) -> Optional[bytes]:
    """Convert a PDF to a DOCX skeleton suitable for ``llm_diff`` editing.

    Tries pdf2docx first; falls back to OCR-synthesized DOCX if that
    yields nothing useful. Returns the DOCX bytes on success, ``None``
    when no usable output could be produced.
    """
    if not pdf_bytes:
        return None

    # Tier 1: pdf2docx.
    try:
        docx_bytes = _convert_with_pdf2docx(pdf_bytes, filename)
        if _docx_has_useful_text(docx_bytes):
            logger.info(
                "PDFDOCX: converted via pdf2docx ({} bytes in, {} bytes out, "
                "filename={!r})",
                len(pdf_bytes), len(docx_bytes or b""), filename,
            )
            return docx_bytes
        logger.info(
            "PDFDOCX: pdf2docx output had insufficient text — trying OCR fallback "
            "(filename={!r})",
            filename,
        )
    except Exception as exc:
        logger.warning(
            "PDFDOCX: pdf2docx raised {} — trying OCR fallback (filename={!r})",
            exc, filename,
        )

    # Tier 2: OCR + synthesized DOCX.
    try:
        docx_bytes = _convert_via_ocr(pdf_bytes, filename)
        if _docx_has_useful_text(docx_bytes):
            logger.info(
                "PDFDOCX: converted via OCR ({} bytes in, {} bytes out, "
                "filename={!r})",
                len(pdf_bytes), len(docx_bytes or b""), filename,
            )
            return docx_bytes
        logger.warning(
            "PDFDOCX: OCR fallback produced no usable text (filename={!r})",
            filename,
        )
    except Exception as exc:
        logger.warning(
            "PDFDOCX: OCR fallback raised {} (filename={!r})",
            exc, filename,
        )

    return None


# ---------------------------------------------------------------------------
# Tier 1 — pdf2docx
# ---------------------------------------------------------------------------


def _convert_with_pdf2docx(pdf_bytes: bytes, filename: str) -> Optional[bytes]:
    """Convert a PDF via the pdf2docx library.

    pdf2docx works file-to-file, so we round-trip through tempfiles. The
    library throws on malformed PDFs and silently produces empty DOCXs
    on image-only PDFs — both cases are handled by the caller via
    ``_docx_has_useful_text``.
    """
    try:
        from pdf2docx import Converter  # type: ignore
    except ImportError as exc:
        logger.warning("pdf2docx is not installed: {}", exc)
        return None

    with tempfile.NamedTemporaryFile(
        prefix="pdf2docx_in_", suffix=".pdf", delete=False
    ) as pdf_tmp:
        pdf_tmp.write(pdf_bytes)
        pdf_path = pdf_tmp.name
    docx_path = pdf_path.replace(".pdf", ".docx")

    try:
        cv = Converter(pdf_path)
        try:
            # multi_processing=False is safer inside async-server processes
            # where the converter could spawn workers that fight the event loop.
            cv.convert(docx_path, start=0, end=None, multi_processing=False)
        finally:
            cv.close()
        if not os.path.exists(docx_path):
            return None
        with open(docx_path, "rb") as fh:
            return fh.read()
    finally:
        for path in (pdf_path, docx_path):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Tier 2 — OCR + synthesized DOCX
# ---------------------------------------------------------------------------


def _convert_via_ocr(pdf_bytes: bytes, filename: str) -> Optional[bytes]:
    """OCR the PDF then build a clean DOCX from the extracted text.

    The OCR work is delegated to the existing project pipeline at
    ``FileProcessor.extract_text_from_pdf`` (which already chains
    PyPDF2 raw extraction → pdf2image + Google Vision OCR). This keeps
    PDF handling consistent with the rest of the codebase.
    """
    from app.processing.file_handlers import FileProcessor

    text = FileProcessor.extract_text_from_pdf(pdf_bytes) or ""
    text = text.strip()
    if len(text) < _MIN_USEFUL_TEXT_CHARS:
        return None
    return _synthesize_docx_from_text(text, filename)


def _synthesize_docx_from_text(text: str, filename: str) -> bytes:
    """Build a DOCX from plain text using standard legal styling.

    Paragraphs are split on blank lines. Single newlines inside a
    paragraph become soft line breaks. Styling matches the system's
    "defaults" path so the synthesized skeleton looks like a clean
    Indian-court filing (TNR 12pt, justified, 1.5 line spacing, 1"
    margins).
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    doc = Document()

    # Page margins.
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    # Normal style.
    try:
        normal = doc.styles["Normal"]
        normal.font.name = "Times New Roman"
        normal.font.size = Pt(12)
        pf = normal.paragraph_format
        pf.line_spacing = 1.5
        pf.space_after = Pt(6)
        pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    except Exception:
        pass

    # Heading sizes (used only if we infer any).
    for level, size in ((1, 16), (2, 14), (3, 12)):
        try:
            hs = doc.styles[f"Heading {level}"]
            hs.font.name = "Times New Roman"
            hs.font.size = Pt(size)
            hs.font.bold = True
        except Exception:
            pass

    # Split into paragraph blocks. PDF OCR often produces single-newline
    # wraps inside a paragraph and double-newlines between paragraphs.
    # Normalize CRLF → LF first.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in normalized.split("\n\n") if b.strip()]

    for block in blocks:
        # Pull individual lines so we can re-emit soft breaks.
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        para = doc.add_paragraph()
        # Heuristic: short ALL-CAPS or "IN THE COURT OF…" lines are
        # likely headings — embolden the first run so the DOCX has
        # some visual hierarchy. This is purely cosmetic; the llm_diff
        # path doesn't rely on it.
        first = lines[0]
        is_heading_like = (
            len(first) < 100
            and (first.isupper() or first.startswith(
                ("IN THE COURT", "BEFORE THE", "IN THE HIGH",
                 "BEFORE HON", "TO,", "TO ")
            ))
        )
        run = para.add_run(first)
        if is_heading_like:
            run.bold = True
        for line in lines[1:]:
            para.add_run().add_break()
            para.add_run(line)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _docx_has_useful_text(docx_bytes: Optional[bytes]) -> bool:
    """Return True when the DOCX has at least one non-trivial paragraph."""
    if not docx_bytes:
        return False
    try:
        from docx import Document

        doc = Document(BytesIO(docx_bytes))
    except Exception:
        return False
    total = 0
    for p in doc.paragraphs:
        total += len((p.text or "").strip())
        if total >= _MIN_USEFUL_TEXT_CHARS:
            return True
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    total += len((p.text or "").strip())
                    if total >= _MIN_USEFUL_TEXT_CHARS:
                        return True
    return False
