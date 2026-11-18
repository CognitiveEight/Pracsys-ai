"""SIM-203..207: follow any uploaded template's format.

Every test runs on the fictional corpus (``template_fidelity.corpus``): six
document types x four deliberately different layouts, each as .docx, text
PDF and simulated scan. No OCR, Vision or model call is made: the scan tests
use the word boxes a perfect OCR would return and a stand-in for the vision
pass that only supplies tables and the font family. Bold, underline, logo,
sizes, spacing and alignment on scans are measured from the pixels.
"""

import asyncio
from io import BytesIO

import pytest
from docx import Document
from docx.oxml.ns import qn

from app.processing.template_fidelity.checklist import check_format, failed
from app.processing.template_fidelity.corpus import DOC_TYPES, VARIANTS, build_corpus, build_template
from app.processing.template_fidelity.docx_builder import build_docx
from app.processing.template_fidelity.docx_fill import apply_ops, parse_ops
from app.processing.template_fidelity.docx_map import build_map, load, map_text
from app.processing.template_fidelity.pdf_reader import canonical_font, is_text_pdf, read_pdf
from app.processing.template_fidelity.pdf_writer import make_scan, simulated_scan_words, write_pdf
from app.processing.template_fidelity.scan_reader import ScanPageInput, _skew_angle, read_scan

CORPUS = build_corpus()
IDS = ["{}-{}".format(t, v.name) for t, v, _ in CORPUS]


def _scan_inputs(ir, pdf):
    _scan, meta, jpegs = make_scan(pdf)
    words = simulated_scan_words(pdf, meta)
    by_img = {j: w for j, w in zip(jpegs, words)}
    tables = [{"rows": [[c.text.replace("\n", " ") for c in r] for r in t.rows],
               "header_rows": t.header_rows, "borders": t.borders} for t in ir.tables()]

    async def vision(_img, _lines):
        return {"font": "sans" if ir.font == "Arial" else "serif", "tables": tables}

    pages = [ScanPageInput(j, ir.page.width, ir.page.height) for j in jpegs]
    return pages, (lambda img: by_img[img]), vision


# ---------------------------------------------------------------------------
# SIM-207: corpus and checklist
# ---------------------------------------------------------------------------

def test_corpus_covers_every_type_and_layout():
    assert {t for t, _, _ in CORPUS} == set(DOC_TYPES)
    assert len(CORPUS) == len(DOC_TYPES) * len(VARIANTS)
    fonts = {v.font for v in VARIANTS}
    numbering = {v.numbering for v in VARIANTS}
    assert len(fonts) >= 2 and len(numbering) >= 3
    assert any(v.letterhead for v in VARIANTS) and any(not v.table_borders for v in VARIANTS)


@pytest.mark.parametrize("doc_type,variant,ir", CORPUS, ids=IDS)
def test_builder_reproduces_template(doc_type, variant, ir):
    assert failed(check_format(ir, build_docx(ir))) == []


def test_checklist_catches_a_wrong_format():
    ir = build_template("residential_rent", VARIANTS[1])  # Arial, letterhead, page numbers
    wrong = build_template("residential_rent", VARIANTS[0])  # Times, neither
    bad = failed(check_format(ir, build_docx(wrong)))
    assert "body_font" in bad and "letterhead_text" in bad and "page_numbers" in bad


# ---------------------------------------------------------------------------
# SIM-205: text PDF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc_type,variant,ir", CORPUS, ids=IDS)
def test_pdf_reader_reproduces_template(doc_type, variant, ir):
    pdf = write_pdf(ir)
    assert is_text_pdf(pdf)
    got = read_pdf(pdf)
    assert failed(check_format(ir, build_docx(got))) == []


def test_canonical_font_names():
    assert canonical_font("ABCDEF+TimesNewRomanPS-BoldMT") == "Times New Roman"
    assert canonical_font("NimbusRoman-Regular") == "Times New Roman"
    assert canonical_font("ArialMT") == "Arial"
    assert canonical_font("NimbusSans-Bold") == "Arial"
    assert canonical_font("Calibri-Bold") == "Calibri"


def test_pdf_ligatures_expanded():
    ir = build_template("complex_will", VARIANTS[0])
    got = read_pdf(write_pdf(ir))
    text = " ".join(p.text for p in got.paragraphs())
    assert "ﬁ" not in text and "ﬂ" not in text


# ---------------------------------------------------------------------------
# SIM-206: scans
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc_type,variant,ir", CORPUS, ids=IDS)
def test_scan_reader_reproduces_template(doc_type, variant, ir):
    pdf = write_pdf(ir)
    scan, _meta, _jpegs = make_scan(pdf)
    assert not is_text_pdf(scan)  # image-only, like a real scan
    pages, ocr, vision = _scan_inputs(ir, pdf)
    got = asyncio.run(read_scan(pages, ocr=ocr, vision=vision))
    assert failed(check_format(ir, build_docx(got))) == []


def test_scan_without_vision_still_measures_layout():
    """No model call: headings, numbering, underline, letterhead and page
    numbers all come from the pixels and the word positions."""
    ir = build_template("residential_rent", VARIANTS[2])  # letterhead, underlined headings
    pages, ocr, _vision = _scan_inputs(ir, write_pdf(ir))
    got = asyncio.run(read_scan(pages, ocr=ocr, vision=None))
    checks = check_format(ir, build_docx(got))
    for key in ("title", "headings", "numbering", "underline", "letterhead_text",
                "letterhead_logo", "page_numbers", "margins", "line_spacing", "body_size"):
        assert checks.get(key) is True, key


@pytest.mark.parametrize("doc_type", ["residential_rent", "complex_will", "complex_sha"])
def test_scan_tilted_two_degrees(doc_type):
    """A clearly tilted scan: words, pixels and underlines are straightened."""
    ir = build_template(doc_type, VARIANTS[2])  # letterhead, underlined headings
    pdf = write_pdf(ir)
    _scan, meta, jpegs = make_scan(pdf, angle=2.0)
    words = simulated_scan_words(pdf, meta)
    by_img = {j: w for j, w in zip(jpegs, words)}
    tables = [{"rows": [[c.text.replace("\n", " ") for c in r] for r in t.rows],
               "header_rows": t.header_rows, "borders": t.borders} for t in ir.tables()]

    async def vision(_img, _lines):
        return {"font": "serif", "tables": tables}

    pages = [ScanPageInput(j, ir.page.width, ir.page.height) for j in jpegs]
    got = asyncio.run(read_scan(pages, ocr=lambda img: by_img[img], vision=vision))
    assert failed(check_format(ir, build_docx(got))) == []


def test_skew_is_measured():
    ir = build_template("simple_will", VARIANTS[0])
    pdf = write_pdf(ir)
    _scan, meta, _ = make_scan(pdf, angle=0.8)
    words = simulated_scan_words(pdf, meta)[0]
    # Tilted counter-clockwise on screen: words climb to the right.
    assert -1.0 < _skew_angle(words) < -0.6


def test_scan_vision_failure_is_not_fatal():
    ir = build_template("simple_sha", VARIANTS[0])
    pages, ocr, _ = _scan_inputs(ir, write_pdf(ir))

    async def broken(_img, _lines):
        raise RuntimeError("vision down")

    got = asyncio.run(read_scan(pages, ocr=ocr, vision=broken))
    assert got.paragraphs()


# ---------------------------------------------------------------------------
# SIM-204: map and fill
# ---------------------------------------------------------------------------

def _template(doc_type="residential_rent", variant=1):
    ir = build_template(doc_type, VARIANTS[variant])
    return ir, build_docx(ir)


def test_map_lists_paragraphs_rows_and_labels():
    _ir, tpl = _template()
    text = map_text(build_map(load(tpl)))
    assert "p0 [" in text and "RESIDENTIAL RENTAL AGREEMENT" in text
    assert "t0 TABLE 4 rows x 3 columns" in text
    assert "t0.r0 [header] | Particular | Amount (INR) | Due" in text
    assert "label=Clause 1." in text
    assert "EXAMPLE & ASSOCIATES" not in text  # header (letterhead) is never listed


def test_parse_ops_formats():
    lines = '{"op":"replace","id":"p1","text":"a"}\nnot json\n{"op":"delete","id":"p2"},'
    assert [o["op"] for o in parse_ops(lines)] == ["replace", "delete"]
    assert len(parse_ops('[{"op":"keep","id":"p0"}]')) == 1
    assert len(parse_ops('{"ops":[{"op":"keep","id":"p0"}]}')) == 1
    assert parse_ops('{"op":"replace","id":"p1","text":"a\tb"}')[0]["text"] == "a\tb"


def test_fill_keeps_template_format():
    ir, tpl = _template()
    entries = {e.id: e for e in build_map(load(tpl)).entries}
    clause = next(i for i, e in entries.items() if e.label == "1.2")
    ops = parse_ops("\n".join([
        '{"op":"replace","id":"p1","text":"Made at **Pune** on <Day> March 2027."}',
        '{"op":"replace","id":"t0.r1.c1","text":"30,000"}',
        '{"op":"add_row","id":"t0.r3","cells":["Parking","1,500","Monthly"]}',
        '{"op":"insert_after","id":"%s","text":"1.3\\tElectricity is payable by the Lessee.","like":"%s"}'
        % (clause, clause),
        '{"op":"delete","id":"p6"}',
        '{"op":"replace","id":"nope","text":"x"}',
    ]))
    out, stats = apply_ops(tpl, ops)
    assert (stats.applied, stats.inserted, stats.rows_added, stats.deleted, stats.unknown) == (
        2, 1, 1, 1, 1)
    doc = Document(BytesIO(out))
    p1 = doc.paragraphs[1]
    assert p1.text == "Made at Pune on <Day> March 2027."
    assert [r.text for r in p1.runs if r.bold] == ["Pune"]
    assert any(r.font.highlight_color is not None for r in p1.runs if r.text == "<Day>")
    rows = [[c.text for c in r.cells] for r in doc.tables[0].rows]
    assert rows[1][1] == "30,000" and rows[-1] == ["Parking", "1,500", "Monthly"]
    new_row_pr = doc.tables[0].rows[-1]._tr.trPr
    assert new_row_pr is None or new_row_pr.find(qn("w:tblHeader")) is None
    src = next(p for p in doc.paragraphs if p.text.startswith("1.2"))
    new = next(p for p in doc.paragraphs if p.text.startswith("1.3"))
    assert new.paragraph_format.left_indent == src.paragraph_format.left_indent
    assert new.paragraph_format.first_line_indent == src.paragraph_format.first_line_indent
    # Untouched parts of the format survive: font, letterhead, page numbers.
    checks = check_format(ir, out)
    for key in ("body_font", "body_size", "title", "letterhead_text", "letterhead_logo",
                "page_numbers", "margins", "table_columns"):
        assert checks[key] is True, key


def test_fill_keeps_literal_label_and_strips_auto_number():
    doc = Document()
    doc.add_paragraph("1.\tOld clause text.")
    doc.add_paragraph("Auto numbered item", style="List Number")
    buf = BytesIO()
    doc.save(buf)
    ops = parse_ops('{"op":"replace","id":"p0","text":"New clause text."}\n'
                    '{"op":"replace","id":"p1","text":"2. Model added a number"}')
    out, _ = apply_ops(buf.getvalue(), ops)
    paras = Document(BytesIO(out)).paragraphs
    assert paras[0].text == "1.\tNew clause text."
    assert paras[1].text == "Model added a number"  # Word numbers it


def test_fill_never_touches_header_or_footer():
    _ir, tpl = _template()
    before = Document(BytesIO(tpl)).sections[0]
    out, _ = apply_ops(tpl, [{"op": "replace", "id": "p0", "text": "A DIFFERENT TITLE"}])
    after = Document(BytesIO(out)).sections[0]
    assert before.header._element.xml == after.header._element.xml
    assert before.footer._element.xml == after.footer._element.xml


def test_inserts_after_one_anchor_keep_their_order():
    _ir, tpl = _template()
    ops = [{"op": "insert_after", "id": "p6", "text": t} for t in ("first", "second", "third")]
    out, _ = apply_ops(tpl, ops)
    texts = [p.text for p in Document(BytesIO(out)).paragraphs]
    k = texts.index("first")
    assert texts[k:k + 3] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Ingest entry point
# ---------------------------------------------------------------------------

def test_ingest_text_pdf_builds_clean_docx():
    from app.processing.template_fidelity.ingest import build_template_docx

    ir = build_template("commercial_rent", VARIANTS[0])
    out = asyncio.run(build_template_docx(write_pdf(ir), "pdf"))
    assert out and failed(check_format(ir, out)) == []
    assert asyncio.run(build_template_docx(b"docx-bytes", "docx")) == b"docx-bytes"
    assert asyncio.run(build_template_docx(b"x", "xlsx")) is None
