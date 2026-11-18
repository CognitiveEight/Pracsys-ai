"""SIM-202: Markdown → .docx renderer fidelity. Fictional fixtures only."""

from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn

from app.processing.docx_generator import (
    docx_structure,
    render_markdown_to_docx,
    template_fill_lost_structure,
)
from app.processing.draft_markdown import normalize_draft_markdown

GOLDEN = Path(__file__).parent / "fixtures" / "drafting" / "golden_addendum.md"

TABLE_MD = "Intro.\n\n| Item | Amount |\n| :--- | ---: |\n| Fee | USD 10 |\n| Tax | USD 2 |\n"


def _doc(md: str, template: bytes = None) -> Document:
    return Document(BytesIO(render_markdown_to_docx(md, template_docx_bytes=template)))


def _template_bytes() -> bytes:
    tpl = Document()
    tpl.add_paragraph("SAMPLE TEMPLATE", style="Heading 1")
    tpl.add_paragraph("Body text of the template, long enough to be the body style.")
    out = BytesIO()
    tpl.save(out)
    return out.getvalue()


def _assert_table(doc: Document) -> None:
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert [c.text for c in t.rows[0].cells] == ["Item", "Amount"]
    assert [c.text for c in t.rows[2].cells] == ["Tax", "USD 2"]
    assert all(r.bold for r in t.rows[0].cells[0].paragraphs[0].runs)
    tr_pr = t.rows[0]._tr.trPr
    assert tr_pr is not None and tr_pr.find(qn("w:tblHeader")) is not None
    assert not any(p.text.lstrip().startswith("|") for p in doc.paragraphs)


def test_table_rendered_defaults():
    doc = _doc(TABLE_MD)
    _assert_table(doc)
    # right-aligned second column from ---:
    assert doc.tables[0].rows[1].cells[1].paragraphs[0].alignment == 2


def test_table_rendered_template_skeleton():
    _assert_table(_doc(TABLE_MD, _template_bytes()))


def test_nested_list_levels():
    md = "- Parent item\n  - Child bullet\n- Second\n\n1. First\n2. Second\n   - (a) labelled\n"
    doc = _doc(md)
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert "Parent item" in texts[0]
    styles = {p.text: p.style.name for p in doc.paragraphs}
    assert styles["Child bullet"] == "List Bullet 2"
    # ordered items carry their number as text; never Word auto-numbering
    assert any(t.startswith("1.\t") for t in texts)
    assert any(t.startswith("2.\t") for t in texts)
    labelled = next(p for p in doc.paragraphs if p.text.startswith("(a)"))
    assert labelled.paragraph_format.left_indent > 0
    assert labelled.paragraph_format.first_line_indent < 0


def test_second_ordered_list_restarts():
    doc = _doc("1. A\n2. B\n\nBreak.\n\n1. C\n2. D\n")
    nums = [p.text.split("\t")[0] for p in doc.paragraphs if "\t" in p.text]
    assert nums == ["1.", "2.", "1.", "2."]


def test_placeholders_highlighted():
    doc = _doc("Dated <Day> of <Month>, at <br> office.")
    runs = doc.paragraphs[0].runs
    hl = [r.text for r in runs if r.font.highlight_color == WD_COLOR_INDEX.YELLOW]
    assert hl == ["<Day>", "<Month>"]


def test_deep_headings_styled():
    doc = _doc("#### Level four\n\n###### Level six\n")
    names = [p.style.name for p in doc.paragraphs if p.text]
    assert names == ["Heading 4", "Heading 6"]
    assert not any(p.text.startswith("#") for p in doc.paragraphs)


def test_hard_breaks_kept():
    doc = _doc("Signed by  \nALEX SAMPLE  \nWitness Name: <Name>\n")
    p = doc.paragraphs[0]
    assert len(p._p.findall(".//" + qn("w:br"))) == 2


def test_signature_block_left_aligned_and_kept_together():
    # Word justifies every line ending in a manual break; a signature block
    # must be left-aligned or its short lines stretch across the page.
    md = ("Body paragraph that stays justified.\n\n"
          "SIGNED, SEALED, AND DELIVERED as a Deed by  \nALEX SAMPLE  \n"
          "In the presence of:  \nWitness Name: <Witness Name>\n")
    for tpl in (None, _template_bytes()):
        doc = _doc(md, tpl)
        block = next(p for p in doc.paragraphs if p.text.startswith("SIGNED"))
        assert block.alignment == 0  # LEFT
        assert block.paragraph_format.keep_together
        body = next(p for p in doc.paragraphs if p.text.startswith("Body"))
        assert body.alignment != 0


def test_signature_underscore_line_kept_inside_block():
    # "____" alone is a CommonMark thematic break; inside a signature block it
    # must stay a line of that block, so the block cannot split across pages.
    doc = _doc("Signed by  \nALEX SAMPLE  \n______________________________  \n"
               "*(Signature of Alex Sample)*  \nWitness Name: A\n")
    paras = [p for p in doc.paragraphs if p.text]
    assert len(paras) == 1
    lines = paras[0].text.split("\n")
    assert "______________________________" in lines
    assert lines[-1] == "Witness Name: A"
    assert paras[0].paragraph_format.keep_together


def test_standalone_underscore_line_kept():
    doc = _doc("Signature:\n\n______________________________\n\nDate:\n")
    assert any(set(p.text) == {"_"} for p in doc.paragraphs if p.text)


def test_short_lines_break_but_prose_joins():
    doc = _doc("Witness Name: A\nWitness Address: B\n")
    assert len(doc.paragraphs[0]._p.findall(".//" + qn("w:br"))) == 1
    long_a = "word " * 30
    doc2 = _doc(long_a + "\n" + long_a + "\n")
    assert not doc2.paragraphs[0]._p.findall(".//" + qn("w:br"))


def test_schedule_page_break():
    doc = _doc("# Title\n\nBody.\n\n## SCHEDULE 1\n\nRows.\n\n### EXECUTION PAGE\n")
    by_text = {p.text: p for p in doc.paragraphs}
    assert by_text["SCHEDULE 1"].paragraph_format.page_break_before
    assert by_text["EXECUTION PAGE"].paragraph_format.page_break_before
    assert not by_text["Title"].paragraph_format.page_break_before


def test_hr_not_rendered_as_dashes():
    doc = _doc("A\n\n---\n\nB\n")
    assert [p.text for p in doc.paragraphs if p.text] == ["A", "B"]


def test_golden_addendum():
    md = normalize_draft_markdown(GOLDEN.read_text(encoding="utf-8"))
    for tpl in (None, _template_bytes()):
        data = render_markdown_to_docx(md, template_docx_bytes=tpl)
        s = docx_structure(data)
        assert s["tables"] == 3
        assert s["leftover_markup"] == 0
        assert s["highlighted_runs"] >= 4
        doc = Document(BytesIO(data))
        cap = doc.tables[2]
        assert cap.rows[1].cells[2].text == "600,000"


def test_golden_lease():
    md = normalize_draft_markdown(
        (GOLDEN.parent / "golden_lease.md").read_text(encoding="utf-8"))
    for tpl in (None, _template_bytes()):
        data = render_markdown_to_docx(md, template_docx_bytes=tpl)
        s = docx_structure(data)
        assert s["tables"] == 6
        assert s["leftover_markup"] == 0
        doc = Document(BytesIO(data))
        # (i)/(ii) sit one level deeper than (a)/(b)/(c)
        a = next(p for p in doc.paragraphs if p.text.startswith("(a) any unpaid rent"))
        i = next(p for p in doc.paragraphs if p.text.startswith("(i) damage"))
        assert i.paragraph_format.left_indent > a.paragraph_format.left_indent
        # parties keep their numbers across the "AND" between them (1, 2), and
        # the clause 4 and clause 5 lists each restart at 1
        nums = [p.text.split("\t")[0] for p in doc.paragraphs if p.text[:2] in ("1.", "2.", "3.", "4.")
                and "\t" in p.text]
        assert nums[:9] == ["1.", "2.", "1.", "2.", "3.", "4.", "1.", "2.", "3."]


def test_golden_unnormalised_still_has_no_markup_in_tables():
    # Even without the normaliser, the renderer must not print headings as #.
    data = render_markdown_to_docx(GOLDEN.read_text(encoding="utf-8"), template_docx_bytes=None)
    doc = Document(BytesIO(data))
    assert not any(p.text.startswith("#") for p in doc.paragraphs)


def test_draft_render_has_stable_route_key():
    # LLM_ROUTES={"draft_render": [...]} must keep matching the draft body
    # call, whatever the enclosing function is called.
    import ast

    src = (Path(__file__).parents[1] / "app" / "langgraph" / "drafting_agent.py").read_text(
        encoding="utf-8")
    calls = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "generate_longform"
        and any(k.arg == "system_instruction" and getattr(k.value, "id", "")
                == "DRAFT_RENDER_INSTRUCTION" for k in n.keywords)
    ]
    assert len(calls) == 1
    task = next(k.value.value for k in calls[0].keywords if k.arg == "task")
    assert task == "draft_render"


def test_roman_labels_not_double_marked():
    doc = _doc("- i. first\n- ii. second\n- iv. fourth\n- b. letter\n")
    for p in (p for p in doc.paragraphs if p.text):
        assert p.style.name != "List Bullet"  # label is the marker, no bullet
        assert not p.text.startswith("•")
        assert p.paragraph_format.first_line_indent < 0


def test_line_breaks_inside_table_cells():
    md = "| A | B |\n| --- | --- |\n| Line one<br>Line two | x<br/>y |\n"
    doc = _doc(md)
    cell = doc.tables[0].rows[1].cells[0]
    assert cell.text == "Line one\nLine two"
    assert "<br" not in doc.tables[0].rows[1].cells[1].text


def test_headerless_table_from_empty_header_row():
    md = "|  |  |\n| --- | --- |\n| **First Party**<br>Address 1 | **Second Party**<br>Address 2 |\n"
    doc = _doc(md)
    t = doc.tables[0]
    assert len(t.rows) == 1
    assert t.rows[0].cells[0].text.startswith("First Party")
    tr_pr = t.rows[0]._tr.trPr
    assert tr_pr.find(qn("w:tblHeader")) is None
    assert not t.rows[0].cells[0]._tc.tcPr.findall(qn("w:shd"))


def test_page_number_footer_defaults_only():
    doc = _doc("Body.")
    footer_xml = doc.sections[0].footer._element.xml
    assert "PAGE" in footer_xml and "NUMPAGES" in footer_xml
    tdoc = _doc("Body.", _template_bytes())
    assert "NUMPAGES" not in tdoc.sections[0].footer._element.xml


def test_template_structure_check():
    md = normalize_draft_markdown(GOLDEN.read_text(encoding="utf-8"))
    assert template_fill_lost_structure(md, _template_bytes()) is True
    full = render_markdown_to_docx(md, template_docx_bytes=None)
    assert template_fill_lost_structure(md, full) is False
