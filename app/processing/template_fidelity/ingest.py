"""Template upload → clean, editable .docx (SIM-205 / SIM-206).

``build_template_docx`` picks the reader for what was uploaded:

- ``.docx``: used as is (it already is the template).
- text PDF: ``pdf_reader`` (fonts, positions, tables, header/footer, logo).
- scanned PDF, or a photo (jpg/png/webp/tif): ``scan_reader`` (OCR word
  positions + pixel styles + optional Gemini vision pass).

The result is rebuilt with ``docx_builder``. Returns None when nothing usable
came out, so the caller falls back to its previous converter.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from loguru import logger

IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "tif", "tiff", "bmp")
_RENDER_DPI = 200


def _render_pages(pdf_bytes: bytes) -> List[tuple]:
    import pymupdf

    out = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=_RENDER_DPI, colorspace=pymupdf.csGRAY)
            out.append((pix.tobytes("png"), page.rect.width, page.rect.height))
    finally:
        doc.close()
    return out


async def build_template_docx(data: bytes, extension: str) -> Optional[bytes]:
    """Clean template .docx for an upload, or None to fall back."""
    from app.core.config import settings
    from app.processing.template_fidelity.docx_builder import build_docx
    from app.processing.template_fidelity.pdf_reader import is_text_pdf, read_pdf
    from app.processing.template_fidelity.scan_reader import ScanPageInput, gemini_vision, read_scan

    ext = (extension or "").lstrip(".").lower()
    if not data:
        return None
    if ext == "docx":
        return data
    vision = gemini_vision if getattr(settings, "template_scan_vision", True) else None
    if ext == "pdf":
        if await asyncio.to_thread(is_text_pdf, data):
            ir = await asyncio.to_thread(read_pdf, data)
        else:
            pages = await asyncio.to_thread(_render_pages, data)
            ir = await read_scan([ScanPageInput(img, w, h) for img, w, h in pages], vision=vision)
    elif ext in IMAGE_EXTENSIONS:
        ir = await read_scan([ScanPageInput(data)], vision=vision)
    else:
        return None
    if not ir.paragraphs():
        logger.warning("template fidelity read nothing usable source={}", ir.source)
        return None
    out = await asyncio.to_thread(build_docx, ir)
    logger.info("template fidelity built source={} paragraphs={} tables={} header={} "
                "page_numbers={}", ir.source, len(ir.paragraphs()), len(ir.tables()),
                bool(ir.header), ir.page_numbers)
    return out
