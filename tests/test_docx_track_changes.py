"""SIM-220: a revision written into the lawyer's own .docx as tracked changes.

Base files are built with python-docx and with the repo's own renderer. The
checks read the output XML directly, and two helpers settle every change
programmatically: accept-all must give the revised text, reject-all the base
text. Fictional data only.
"""

import io
import re
import zipfile
from datetime import datetime, timezone

from docx import Document
from docx.shared import Pt
from lxml import etree

from app.processing.docx_generator import render_markdown_to_docx
from app.processing.docx_markdown import docx_to_draft_markdown
from app.processing.docx_review_state import scan_review_state
from app.processing.docx_track_changes import (
    apply_revision_as_tracked_changes,
    build_tracked_revision,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
AUTHOR = "SimplerToday AI"
WHEN = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)


def W(tag):
    return f"{{{W_NS}}}{tag}"


NOTICE = """# LEGAL NOTICE

## Facts

The noticee bought fictional goods on credit from the sender in March.

The noticee has not paid the amount due despite reminders.

| Item | Amount |
| --- | --- |
| Principal | 1,00,000 |
| Interest | 18,000 |

## Demand

Pay the amount within fifteen days of this notice.
"""


def _rendered(md=NOTICE):
    return render_markdown_to_docx(md, template_docx_bytes=None, title="Legal Notice")


def _apply(base, revised, base_md=None, **kw):
    base_md = base_md if base_md is not None else docx_to_draft_markdown(base)
    return apply_revision_as_tracked_changes(base, base_md, revised, date=WHEN, **kw)


def _doc_xml(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return etree.fromstring(zf.read("word/document.xml"))


def _parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _with_document(data, root):
    parts = _parts(data)
    parts["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in parts.items():
            zf.writestr(n, b)
    return out.getvalue()


def _unwrap(el):
    parent = el.getparent()
    idx = parent.index(el)
    for child in list(el):
        parent.insert(idx, child)
        idx += 1
    parent.remove(el)


def _settle(data, *, accept):
    """Accept or reject every tracked change, as an editor would."""
    root = _doc_xml(data)
    gone, kept = (W("del"), W("ins")) if accept else (W("ins"), W("del"))
    # Rows: an inserted row is removed on reject, a deleted row on accept.
    for tr in list(root.iter(W("tr"))):
        trpr = tr.find(W("trPr"))
        if trpr is not None and trpr.find(gone) is not None:
            tr.getparent().remove(tr)
    # Paragraphs whose mark goes away (all their text goes with it here).
    for p in list(root.iter(W("p"))):
        ppr = p.find(W("pPr"))
        rpr = ppr.find(W("rPr")) if ppr is not None else None
        if rpr is not None and rpr.find(gone) is not None:
            p.getparent().remove(p)
    for el in list(root.iter(gone)):
        if el.getparent() is not None and el.getparent().tag in (W("rPr"), W("trPr")):
            el.getparent().remove(el)
        elif el.getparent() is not None:
            el.getparent().remove(el)
    for el in list(root.iter(kept)):
        if el.getparent().tag in (W("rPr"), W("trPr")):
            el.getparent().remove(el)
        else:
            _unwrap(el)
    for el in list(root.iter(W("delText"))):
        el.tag = W("t")
    return _with_document(data, root)


def _plain(md):
    lines = []
    for ln in md.splitlines():
        if re.match(r"^\s*\|?\s*:?-{3,}", ln):
            continue
        ln = re.sub(r"^\s*#+\s+", "", ln)
        ln = re.sub(r"^\s*-\s+", "", ln)
        ln = ln.replace("*", "").replace("|", " ").replace("<br>", " ")
        lines.append(ln)
    return " ".join(" ".join(lines).split())


def _changes(data):
    root = _doc_xml(data)
    return [el for el in root.iter(W("ins"), W("del"))]


def _text_of(el):
    return "".join(t.text or "" for t in el.iter(W("t"), W("delText")))


# ------------------------------------------------------------------ basics


def test_one_word_changed_is_tracked_alone():
    base = _rendered()
    revised = docx_to_draft_markdown(base).replace("in March", "in April")
    res = _apply(base, revised)
    assert res is not None and res.changed_blocks == 1
    changes = _changes(res.docx)
    assert [(c.tag, _text_of(c)) for c in changes] == [(W("del"), "March"), (W("ins"), "April")]
    # The rest of that paragraph is untouched text.
    root = _doc_xml(res.docx)
    para = next(p for p in root.iter(W("p")) if "noticee bought" in "".join(t.text or "" for t in p.iter(W("t"))))
    kept = "".join(t.text or "" for t in para.iter(W("t")) if t.getparent().getparent().tag != W("ins"))
    assert kept == "The noticee bought fictional goods on credit from the sender in ."


def test_paragraph_inserted_and_deleted():
    base = _rendered()
    md = docx_to_draft_markdown(base)
    revised = md.replace(
        "The noticee has not paid the amount due despite reminders.\n",
        "A fictional new paragraph about the reminder letters.\n",
    )
    res = _apply(base, revised)
    assert res is not None and res.inserted == 1 and res.deleted == 1
    root = _doc_xml(res.docx)
    marks = {el.getparent().getparent().getparent().tag: el.tag for el in root.iter(W("ins"), W("del"))
             if el.getparent().tag == W("rPr") and el.getparent().getparent().tag == W("pPr")}
    assert marks  # paragraph marks carry the change
    new_p = [p for p in root.iter(W("p")) if "reminder letters" in _text_of(p)][0]
    assert new_p.find("w:pPr/w:rPr/w:ins", NS) is not None
    old_p = [p for p in root.iter(W("p")) if "despite reminders" in _text_of(p)][0]
    assert old_p.find("w:pPr/w:rPr/w:del", NS) is not None
    assert old_p.find(".//w:t", NS) is None  # all its text is delText now


def test_table_cell_changed_row_added_and_row_removed():
    base = _rendered()
    md = docx_to_draft_markdown(base)
    revised = md.replace("| Interest | 18,000 |", "| Costs | 5,000 |").replace(
        "| Principal | 1,00,000 |", "| Principal | 1,20,000 |"
    )
    res = _apply(base, revised)
    assert res is not None
    root = _doc_xml(res.docx)
    rows = list(root.iter(W("tr")))
    deleted_rows = [r for r in rows if r.find("w:trPr/w:del", NS) is not None]
    inserted_rows = [r for r in rows if r.find("w:trPr/w:ins", NS) is not None]
    assert len(deleted_rows) == 1 and "18,000" in _text_of(deleted_rows[0])
    assert len(inserted_rows) == 1 and "5,000" in _text_of(inserted_rows[0])
    principal = [r for r in rows if "Principal" in _text_of(r)][0]
    assert [_text_of(d) for d in principal.iter(W("del"))] == ["1,00,000"]
    assert [_text_of(i) for i in principal.iter(W("ins"))] == ["1,20,000"]


def test_inserted_runs_copy_neighbour_font_and_markdown_bold():
    doc = Document()
    p = doc.add_paragraph()
    run = p.add_run("The fictional tenant shall vacate the premises.")
    run.font.name = "Georgia"
    run.font.size = Pt(13)
    for k in range(3):
        doc.add_paragraph(f"Fictional unchanged clause {k}.")
    buf = io.BytesIO()
    doc.save(buf)
    base = buf.getvalue()
    revised = docx_to_draft_markdown(base).replace("shall vacate", "shall **promptly** vacate")
    res = _apply(base, revised)
    assert res is not None
    ins = [el for el in _changes(res.docx) if el.tag == W("ins")]
    assert len(ins) == 1
    runs = list(ins[0].iter(W("r")))
    bold_run = [r for r in runs if "promptly" in _text_of(r)][0]
    rpr = bold_run.find(W("rPr"))
    assert rpr.find(W("b")) is not None
    assert rpr.find(W("rFonts")).get(W("ascii")) == "Georgia"
    assert rpr.find(W("sz")).get(W("val")) == "26"


def test_placeholder_is_inserted_as_text():
    base = _rendered()
    revised = docx_to_draft_markdown(base) + "\nSigned by <Sender Name>.\n"
    res = _apply(base, revised)
    assert res is not None
    assert any("<Sender Name>" in _text_of(c) for c in _changes(res.docx))


# ------------------------------------------------------------ untouched parts


def _docx_with_header_footer_comments():
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Fictional Chambers letterhead"
    doc.sections[0].footer.paragraphs[0].text = "Page footer"
    doc.add_paragraph("The fictional landlord served notice on the tenant.")
    doc.add_paragraph("The tenant replied within a week.")
    for k in range(3):
        doc.add_paragraph(f"Fictional unchanged clause {k}.")
    buf = io.BytesIO()
    doc.save(buf)
    parts = _parts(buf.getvalue())
    parts["word/comments.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:comments xmlns:w="{W_NS}">'
        '<w:comment w:id="0" w:author="Fictional"><w:p><w:r><w:t>note</w:t></w:r></w:p></w:comment>'
        "</w:comments>"
    ).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in parts.items():
            zf.writestr(n, b)
    return out.getvalue()


def test_headers_footers_comments_and_section_properties_are_untouched():
    base = _docx_with_header_footer_comments()
    revised = docx_to_draft_markdown(base).replace("within a week", "within ten days")
    res = _apply(base, revised)
    assert res is not None
    before, after = _parts(base), _parts(res.docx)
    assert set(before) == set(after)
    for name in before:
        if name != "word/document.xml":
            assert before[name] == after[name], name
    sect_before = etree.tostring(_doc_xml(base).find("w:body/w:sectPr", NS))
    sect_after = etree.tostring(_doc_xml(res.docx).find("w:body/w:sectPr", NS))
    assert sect_before == sect_after


# --------------------------------------------------------- accept / reject


def test_accept_all_gives_the_revision_and_reject_all_gives_the_base():
    base = _rendered()
    md = docx_to_draft_markdown(base)
    revised = (
        md.replace("in March", "in **early** April")
        .replace("The noticee has not paid the amount due despite reminders.\n", "")
        .replace("| Interest | 18,000 |", "| Interest | 19,000 |\n| Costs | 5,000 |")
        + "\nA fictional closing paragraph.\n"
    )
    # 3 of 7 blocks change here; the default cut-off (0.4) would fall back.
    res = _apply(base, revised, max_changed_ratio=0.6)
    assert res is not None
    accepted = _settle(res.docx, accept=True)
    rejected = _settle(res.docx, accept=False)
    assert _plain(docx_to_draft_markdown(accepted)) == _plain(revised)
    assert _plain(docx_to_draft_markdown(rejected)) == _plain(md)
    assert scan_review_state(accepted).tracked_changes == 0
    assert scan_review_state(rejected).tracked_changes == 0


def test_output_is_counted_as_open_changes_by_the_scanner():
    base = _rendered()
    res = _apply(base, docx_to_draft_markdown(base).replace("fifteen", "thirty"))
    assert res is not None
    state = scan_review_state(res.docx)
    assert state.readable and state.tracked_changes == 2 and state.has_open_changes


def test_output_reopens_and_every_change_has_unique_id_author_and_date():
    base = _rendered()
    md = docx_to_draft_markdown(base)
    revised = md.replace("in March", "in April").replace("| Interest | 18,000 |", "") + "\nNew text.\n"
    res = _apply(base, revised)
    assert res is not None
    Document(io.BytesIO(res.docx))  # re-opens
    changes = _changes(res.docx)
    assert len(changes) >= 4
    ids = [c.get(W("id")) for c in changes]
    assert len(ids) == len(set(ids))
    assert all(c.get(W("author")) == AUTHOR for c in changes)
    assert all(c.get(W("date")) == "2026-11-17T10:00:00Z" for c in changes)


# --------------------------------------------------------------- fallbacks


def test_completely_different_text_falls_back():
    base = _rendered()
    revised = "# AGREEMENT\n\nAn entirely different fictional document.\n\nWith other words throughout.\n"
    assert _apply(base, revised) is None
    assert build_tracked_revision(base, docx_to_draft_markdown(base), revised).reason == "too_many_changes"


def test_column_count_change_falls_back():
    base = _rendered()
    md = docx_to_draft_markdown(base)
    revised = (
        md.replace("| Item | Amount |", "| Item | Amount | Note |")
        .replace("| --- | --- |", "| --- | --- | --- |")
        .replace("| Principal | 1,00,000 |", "| Principal | 1,00,000 | due |")
        .replace("| Interest | 18,000 |", "| Interest | 18,000 | due |")
    )
    res = build_tracked_revision(base, md, revised)
    assert res.docx is None and res.reason == "table_columns_changed"


def test_not_a_docx_falls_back():
    assert build_tracked_revision(b"not a zip", "x", "y").reason == "not_a_docx"


def _ten_paragraphs():
    doc = Document()
    for k in range(10):
        doc.add_paragraph(f"Fictional clause number {k} binds the parties fully.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_changed_ratio_threshold_both_sides():
    base = _ten_paragraphs()
    md = docx_to_draft_markdown(base)

    def change(n):
        out = md
        for k in range(n):
            out = out.replace(f"number {k} binds", f"number {k} obliges")
        return out

    at = build_tracked_revision(base, md, change(4), max_changed_ratio=0.4)
    over = build_tracked_revision(base, md, change(5), max_changed_ratio=0.4)
    assert at.applied and at.changed_blocks == 4
    assert not over.applied and over.reason == "too_many_changes"
    assert build_tracked_revision(base, md, change(5), max_changed_ratio=0.5).applied


def test_no_changes_returns_the_base_unchanged():
    base = _rendered()
    res = build_tracked_revision(base, docx_to_draft_markdown(base), docx_to_draft_markdown(base))
    assert res.applied and res.reason == "no_changes" and not res.has_changes
    assert res.docx == base


def test_base_markdown_differences_are_not_shown_as_changes():
    # B carries a totals marker the file does not have; R keeps it. Only the
    # one word the revision changed is tracked.
    base = _rendered(NOTICE.replace("| Interest | 18,000 |", "| Interest | 18,000 |\n| Total | 1,10,000 |"))
    md = docx_to_draft_markdown(base).replace("| Total | 1,10,000 |", "| Total | 1,10,000 <check total: 1,18,000> |")
    revised = md.replace("fifteen", "thirty")
    res = _apply(base, revised, base_md=md)
    assert res is not None
    assert sorted(_text_of(c) for c in _changes(res.docx)) == ["fifteen", "thirty"]


def test_word_numbered_list_keeps_word_numbering():
    doc = Document()
    doc.add_paragraph("Grounds of the fictional appeal")
    for text in ("The order ignored the receipt.", "The order ignored the ledger.", "The order ignored the witness."):
        doc.add_paragraph(text, style="List Number")
    doc.add_paragraph("Prayer for fictional relief follows below.")
    doc.add_paragraph("Place and date of signing.")
    buf = io.BytesIO()
    doc.save(buf)
    base = buf.getvalue()
    md = docx_to_draft_markdown(base)
    lines = md.splitlines()
    witness = next(k for k, ln in enumerate(lines) if "witness" in ln)
    lines.insert(witness + 1, "")
    lines.insert(witness + 2, "4. The order ignored the fictional contract.")
    revised = "\n".join(lines).replace("the ledger", "the cash ledger")
    res = _apply(base, revised)
    assert res is not None and res.inserted == 1 and res.changed_blocks == 1
    root = _doc_xml(res.docx)
    new_p = next(p for p in root.iter(W("p")) if "fictional contract" in _text_of(p))
    assert new_p.find("w:pPr/w:pStyle", NS).get(W("val")) == "ListNumber"
    assert not _text_of(new_p).lstrip().startswith("4")  # Word draws the number
    assert [_text_of(c) for c in _changes(res.docx) if c.tag == W("ins") and _text_of(c) == "cash "]


# ------------------------------------------------- stricter checks (tester)


def _block_signature(data):
    """Every body paragraph as (paragraph properties, [(char, run rPr), ...]).

    An empty ``w:rPr`` left in a paragraph's ``w:pPr`` by settling a mark (and
    an empty ``w:pPr`` it leaves behind) carries no formatting, so it is
    dropped before comparing; everything else must match exactly.
    """
    out = []
    for p in _doc_xml(data).find(W("body")).iter(W("p")):
        ppr = p.find(W("pPr"))
        if ppr is not None:
            ppr = etree.fromstring(etree.tostring(ppr))
            mark = ppr.find(W("rPr"))
            if mark is not None and len(mark) == 0:
                ppr.remove(mark)
            if len(ppr) == 0 and not ppr.attrib:
                ppr = None
        seq = []
        for r in p.iter(W("r")):
            rpr = r.find(W("rPr"))
            key = etree.tostring(rpr) if rpr is not None else b""
            for t in r.iter(W("t")):
                seq.extend((ch, key) for ch in (t.text or ""))
        out.append((etree.tostring(ppr) if ppr is not None else b"", seq))
    return out


def _mixed_revision(md):
    return (
        md.replace("in March", "in **early** April")
        .replace("The noticee has not paid the amount due despite reminders.\n", "")
        .replace("| Interest | 18,000 |", "| Interest | 19,000 |\n| Costs | 5,000 |")
        + "\nA fictional closing paragraph.\n"
    )


def test_reject_all_restores_base_text_and_run_formatting_exactly():
    # AC-8, exact form: no whitespace folding, no markup stripping. The
    # rejected file converts to the very same Markdown, and every character
    # of every paragraph keeps its original run formatting.
    base = _rendered()
    md = docx_to_draft_markdown(base)
    res = _apply(base, _mixed_revision(md), max_changed_ratio=0.6)
    assert res is not None
    rejected = _settle(res.docx, accept=False)
    assert docx_to_draft_markdown(rejected) == md
    assert _block_signature(rejected) == _block_signature(base)


def test_accept_all_markdown_equals_the_revision_exactly():
    # AC-8, exact form: bold markup and table cells included (the plain-text
    # comparison above strips '*' and '|').
    base = _rendered()
    md = docx_to_draft_markdown(base)
    revised = _mixed_revision(md)
    res = _apply(base, revised, max_changed_ratio=0.6)
    assert res is not None
    accepted = docx_to_draft_markdown(_settle(res.docx, accept=True))
    assert accepted.strip() == re.sub(r"\n{3,}", "\n\n", revised).strip()
    assert "**early**" in accepted


def test_change_ids_never_reuse_an_existing_id_and_default_date_is_iso_utc():
    # AC-10: ids stay unique against ids already in the package (a comment
    # here), and with no date passed the stamp is the current UTC time in
    # ISO form with a Z suffix. A non-UTC date is converted to UTC.
    base = _docx_with_header_footer_comments()
    existing = set()
    for name, blob in _parts(base).items():
        if name.startswith("word/") and name.endswith(".xml"):
            existing.update(m.decode() for m in re.findall(rb'w:id="(-?\d+)"', blob))
    assert existing  # the comment's id at least
    md = docx_to_draft_markdown(base)
    revised = md.replace("within a week", "within ten days")
    before = datetime.now(timezone.utc).replace(microsecond=0)
    res = build_tracked_revision(base, md, revised)
    after = datetime.now(timezone.utc)
    assert res.applied and res.docx is not None
    changes = _changes(res.docx)
    ids = [c.get(W("id")) for c in changes]
    assert ids and len(ids) == len(set(ids)) and not (set(ids) & existing)
    for c in changes:
        stamp = c.get(W("date"))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp), stamp
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert before <= when <= after
    from datetime import timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    res_ist = build_tracked_revision(base, md, revised, date=datetime(2026, 11, 17, 15, 30, tzinfo=ist))
    assert {c.get(W("date")) for c in _changes(res_ist.docx)} == {"2026-11-17T10:00:00Z"}


def test_written_file_is_refused_as_the_next_revision_base():
    # AC-9: the SIM-219 base inspection (scan, then convert) refuses the file
    # this service wrote, because its suggested changes are still open.
    from app.processing.draft_revision_base import OUTCOME_OPEN_CHANGES, _inspect

    base = _rendered()
    res = _apply(base, docx_to_draft_markdown(base).replace("fifteen", "thirty"))
    assert res is not None
    inspected = _inspect(res.docx)
    assert inspected.outcome == OUTCOME_OPEN_CHANGES
    assert inspected.refusal() == {"code": OUTCOME_OPEN_CHANGES, "trackedChanges": 2, "comments": 0}
    assert inspected.docx_bytes == b""  # a refused base is not kept
