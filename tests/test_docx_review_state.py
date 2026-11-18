"""SIM-219: the "open changes" check on a draft .docx.

Before the drafting agent revises the lawyer's latest version it counts open
tracked changes and comments (contract section 4). Files are built in-test
with python-docx, then their XML parts are edited inside the zip. Fictional
text only; no network, no model.
"""

import io
import zipfile

from docx import Document

from app.processing.docx_review_state import scan_review_state

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CHANGE_ATTRS = 'w:id="{}" w:author="Fictional Editor" w:date="2026-11-01T10:00:00Z"'


def _base_docx(*, with_header: bool = False) -> bytes:
    doc = Document()
    doc.add_heading("Legal Notice", level=1)
    doc.add_paragraph("The noticee owes the sender a fictional sum.")
    if with_header:
        doc.sections[0].header.paragraphs[0].text = "Fictional Chambers"
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _parts(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _rezip(parts: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            zf.writestr(name, body)
    return out.getvalue()


def _inject_body(data: bytes, xml: str) -> bytes:
    parts = _parts(data)
    doc = parts["word/document.xml"].decode("utf-8")
    idx = doc.index("<w:sectPr")
    parts["word/document.xml"] = (doc[:idx] + xml + doc[idx:]).encode("utf-8")
    return _rezip(parts)


def test_clean_document_has_no_open_changes():
    state = scan_review_state(_base_docx())
    assert state.readable
    assert (state.tracked_changes, state.comments) == (0, 0)
    assert not state.has_open_changes


def test_insertion_and_deletion_are_counted():
    xml = (
        "<w:p>"
        f"<w:ins {CHANGE_ATTRS.format(1)}><w:r><w:t>added words</w:t></w:r></w:ins>"
        f"<w:del {CHANGE_ATTRS.format(2)}><w:r><w:delText>removed words</w:delText></w:r></w:del>"
        "</w:p>"
    )
    state = scan_review_state(_inject_body(_base_docx(), xml))
    assert state.readable
    assert state.tracked_changes == 2
    assert state.comments == 0
    assert state.has_open_changes


def test_move_is_counted():
    xml = (
        f"<w:p><w:moveFrom {CHANGE_ATTRS.format(3)}><w:r><w:t>moved</w:t></w:r></w:moveFrom></w:p>"
        f"<w:p><w:moveTo {CHANGE_ATTRS.format(4)}><w:r><w:t>moved</w:t></w:r></w:moveTo></w:p>"
    )
    state = scan_review_state(_inject_body(_base_docx(), xml))
    assert state.tracked_changes == 2
    assert state.has_open_changes


def test_formatting_change_alone_is_an_open_change():
    xml = (
        "<w:p><w:r><w:rPr><w:b/>"
        f"<w:rPrChange {CHANGE_ATTRS.format(5)}><w:rPr/></w:rPrChange>"
        "</w:rPr><w:t>now bold</w:t></w:r></w:p>"
    )
    state = scan_review_state(_inject_body(_base_docx(), xml))
    assert state.tracked_changes == 1
    assert state.comments == 0
    assert state.has_open_changes


def test_change_only_in_a_header_is_found():
    data = _base_docx(with_header=True)
    parts = _parts(data)
    headers = [n for n in parts if n.startswith("word/header") and n.endswith(".xml")]
    assert headers, "python-docx wrote a header part"
    name = headers[0]
    hdr = parts[name].decode("utf-8")
    idx = hdr.rindex("</w:hdr>")
    parts[name] = (
        hdr[:idx]
        + f"<w:p><w:ins {CHANGE_ATTRS.format(6)}><w:r><w:t>new header</w:t></w:r></w:ins></w:p>"
        + hdr[idx:]
    ).encode("utf-8")
    state = scan_review_state(_rezip(parts))
    assert state.tracked_changes == 1
    assert state.has_open_changes
    # The body itself is clean: the header alone carries the change.
    assert scan_review_state(data).tracked_changes == 0


def test_comment_only_counts_every_comment():
    parts = _parts(_base_docx())
    parts["word/comments.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:comments xmlns:w="{W}">'
        f'<w:comment {CHANGE_ATTRS.format(7)}><w:p><w:r><w:t>Check this.</w:t></w:r></w:p></w:comment>'
        f'<w:comment {CHANGE_ATTRS.format(8)}><w:p><w:r><w:t>Resolved?</w:t></w:r></w:p></w:comment>'
        "</w:comments>"
    ).encode("utf-8")
    state = scan_review_state(_rezip(parts))
    assert state.readable
    assert state.tracked_changes == 0
    assert state.comments == 2
    assert state.has_open_changes


def test_empty_comments_part_is_clean():
    parts = _parts(_base_docx())
    parts["word/comments.xml"] = f'<w:comments xmlns:w="{W}"/>'.encode("utf-8")
    state = scan_review_state(_rezip(parts))
    assert state.readable and not state.has_open_changes


def test_parsing_is_namespace_aware_not_substring():
    # The words "w:ins" in the text and an <ins> in another namespace are not
    # tracked changes.
    xml = (
        "<w:p><w:r><w:t>See &lt;w:ins&gt; and w:del in the manual.</w:t></w:r></w:p>"
        '<w:p><x:ins xmlns:x="urn:fictional:other"/></w:p>'
    )
    state = scan_review_state(_inject_body(_base_docx(), xml))
    assert state.readable
    assert state.tracked_changes == 0


def test_invalid_zip_is_unreadable():
    state = scan_review_state(b"this is not a word file")
    assert not state.readable
    assert state.reason == "not_a_zip"
    assert not state.has_open_changes


def test_zip_without_document_part_is_unreadable():
    state = scan_review_state(_rezip({"hello.txt": b"fictional"}))
    assert not state.readable
    assert state.reason == "not_a_docx"


def test_malformed_xml_is_unreadable():
    parts = _parts(_base_docx())
    parts["word/document.xml"] = b"<w:document><w:body>"
    state = scan_review_state(_rezip(parts))
    assert not state.readable
    assert state.reason == "bad_xml"


def test_oversized_part_is_unreadable_without_expanding_it():
    # ~60 MB of whitespace compresses to a few dozen KB: a small zip bomb.
    parts = _parts(_base_docx())
    doc = parts["word/document.xml"].decode("utf-8")
    idx = doc.index("<w:body>")
    parts["word/document.xml"] = (
        doc[:idx] + (" " * (60 * 1024 * 1024)) + doc[idx:]
    ).encode("utf-8")
    bomb = _rezip(parts)
    assert len(bomb) < 2 * 1024 * 1024
    state = scan_review_state(bomb)
    assert not state.readable
    assert state.reason == "too_large"


def test_oversized_part_is_rejected_before_decompression():
    # AC-9 "not expanded": the 60 MB part must never be inflated in memory.
    import tracemalloc

    parts = _parts(_base_docx())
    doc = parts["word/document.xml"].decode("utf-8")
    idx = doc.index("<w:body>")
    parts["word/document.xml"] = (
        doc[:idx] + (" " * (60 * 1024 * 1024)) + doc[idx:]
    ).encode("utf-8")
    bomb = _rezip(parts)
    del parts, doc
    tracemalloc.start()
    try:
        state = scan_review_state(bomb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert state.reason == "too_large"
    assert peak < 10 * 1024 * 1024, f"scan allocated {peak} bytes"


def test_size_cap_is_configurable():
    state = scan_review_state(_base_docx(), max_uncompressed_bytes=100)
    assert not state.readable
    assert state.reason == "too_large"


def test_empty_bytes_are_unreadable():
    assert not scan_review_state(b"").readable
