"""SIM-217: the lawyer's chosen court / letterhead format is applied to the
draft's Word file.

Pure formatter tests on python-docx files, plus the drafting export path with
a stubbed upload. No network, no model calls. Fictional data only.
"""

from __future__ import annotations

import copy
import struct
import zlib
from io import BytesIO

import pytest
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Mm, Pt

import app.langgraph.drafting_agent as drafting_module
from app.processing import draft_format
from app.processing.draft_format import (
    apply_format_layout,
    parse_length,
    select_format_layout,
    without_image_links,
)
from app.processing.format_images import fetch_format_image, image_kind

COURT = {
    "id": "fmt_court",
    "name": "Example court format",
    "type": "court",
    "formatting": {
        "page": {
            "font": "Times New Roman", "fontSize": "12pt", "lineSpacing": "1.5",
            "pageSize": "A4",
            "margins": {"top": "1in", "bottom": "1in", "left": "1.5in", "right": "1in"},
            "alignment": "justified", "pageNumber": "center_bottom",
        },
        "court": {
            "courtHeader": {"text": "in the high court of example", "bold": True,
                            "uppercase": True, "align": "center"},
            "jurisdiction": {"text": "(Civil Appellate Jurisdiction)", "align": "center"},
            "caseNumber": {"text": "Writ Petition No. ____ of 2026", "align": "center"},
            "customFields": [{"label": "CNR No", "value": "XXXX000000002026",
                              "text": "CNR No: XXXX000000002026"}],
        },
    },
}

LETTERHEAD = {
    "id": "fmt_lh",
    "name": "Example letterhead",
    "type": "letterhead",
    "formatting": {
        "page": {"font": "Calibri", "fontSize": "11pt", "watermark": "None"},
        "letterhead": {
            "headerType": "text", "headerTextLeft": "Example & Associates",
            "headerAlignLeft": "left", "headerBoldLeft": True, "headerShowCenter": False,
            "headerTextCenter": "Hidden centre text", "headerTextRight": "Advocates",
            "footerType": "text", "footerTextCenter": "1 Sample Road, Example City",
            "pageApplication": "all",
        },
    },
}

BODY = [
    "The petitioner most respectfully submits as follows.",
    "That the facts of the fictional matter are set out below.",
]


def _docx(paragraphs=BODY, header_text=None) -> bytes:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    if header_text:
        doc.sections[0].header.paragraphs[0].text = header_text
        doc.sections[0].footer.paragraphs[0].text = "Old footer"
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _open(result) -> Document:
    return Document(BytesIO(result.docx_bytes))


def _png() -> bytes:
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


def _variant(base, path, value):
    fl = copy.deepcopy(base)
    node = fl["formatting"]
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return fl


def _header_xml(doc, attr="header") -> str:
    return getattr(doc.sections[0], attr)._element.xml


# ---------------------------------------------------------------- page


def test_court_page_settings_applied():
    doc = _open(apply_format_layout(_docx(), COURT))
    s = doc.sections[0]
    assert abs(s.page_width - Mm(210)) < Mm(1) and abs(s.page_height - Mm(297)) < Mm(1)
    assert s.left_margin == Inches(1.5) and s.right_margin == Inches(1)
    assert s.top_margin == Inches(1) and s.bottom_margin == Inches(1)
    normal = doc.styles["Normal"]
    assert normal.font.name == "Times New Roman"
    fonts = normal.element.rPr.rFonts
    assert fonts.get(qn("w:eastAsia")) == "Times New Roman"
    assert normal.font.size == Pt(12)
    assert normal.paragraph_format.line_spacing == 1.5
    assert normal.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY
    body = [p for p in doc.paragraphs if p.text in BODY]
    assert all(p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY for p in body)
    heading_fonts = doc.styles["Heading 1"].element.rPr.rFonts
    assert heading_fonts.get(qn("w:ascii")) == "Times New Roman"
    assert heading_fonts.get(qn("w:asciiTheme")) is None


def test_justify_leaves_centred_paragraphs_and_line_blocks():
    doc = Document()
    centred = doc.add_paragraph("VERSUS")
    centred.alignment = WD_ALIGN_PARAGRAPH.CENTER
    block = doc.add_paragraph("Line one")
    block.add_run().add_break()
    block.add_run("Line two")
    buf = BytesIO()
    doc.save(buf)
    out = _open(apply_format_layout(buf.getvalue(), COURT))
    texts = {p.text: p for p in out.paragraphs}
    assert texts["VERSUS"].alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert texts["Line one\nLine two"].alignment is None


def test_landscape_swaps_width_and_height():
    fl = _variant(COURT, ["page", "orientation"], "landscape")
    s = _open(apply_format_layout(_docx(), fl)).sections[0]
    assert s.orientation == WD_ORIENT.LANDSCAPE
    assert s.page_width > s.page_height
    assert abs(s.page_width - Mm(297)) < Mm(1)


@pytest.mark.parametrize("size,width,height", [
    ("Legal", Inches(8.5), Inches(14)), ("Letter", Inches(8.5), Inches(11)),
])
def test_page_sizes(size, width, height):
    s = _open(apply_format_layout(_docx(), _variant(COURT, ["page", "pageSize"], size))).sections[0]
    assert s.page_width == width and s.page_height == height


@pytest.mark.parametrize("text,expected", [
    ("1in", Inches(1)), ("2.54cm", Mm(25.4)), ("25mm", Mm(25)), ("72pt", Pt(72)),
    ("1.25", Inches(1.25)), (1, Inches(1)), ("abc", None), ("-1in", None),
    ("99in", None), (None, None), (True, None),
])
def test_parse_length(text, expected):
    got = parse_length(text)
    if expected is None:
        assert got is None
    else:
        assert abs(got - int(expected)) <= 1


def test_watermark_in_header():
    fl = _variant(COURT, ["page", "watermark"], "CONFIDENTIAL")
    doc = _open(apply_format_layout(_docx(), fl))
    xml = _header_xml(doc)
    assert 'string="CONFIDENTIAL"' in xml
    assert "PowerPlusWaterMarkObject" in xml and "rotation:315" in xml
    # Applied again: still exactly one watermark.
    again = _open(apply_format_layout(_open_bytes(doc), fl))
    assert _header_xml(again).count("PowerPlusWaterMarkObject") == 1


def _open_bytes(doc) -> bytes:
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_watermark_none_removes_existing():
    with_mark = apply_format_layout(_docx(), _variant(COURT, ["page", "watermark"], "DRAFT"))
    cleared = _open(apply_format_layout(
        with_mark.docx_bytes, _variant(COURT, ["page", "watermark"], "None")))
    assert "PowerPlusWaterMarkObject" not in _header_xml(cleared)


@pytest.mark.parametrize("position,attr,align", [
    ("center_bottom", "footer", WD_ALIGN_PARAGRAPH.CENTER),
    ("right_bottom", "footer", WD_ALIGN_PARAGRAPH.RIGHT),
    ("right_top", "header", WD_ALIGN_PARAGRAPH.RIGHT),
    ("left_top", "header", WD_ALIGN_PARAGRAPH.LEFT),
])
def test_page_number_position(position, attr, align):
    result = apply_format_layout(_docx(), _variant(COURT, ["page", "pageNumber"], position))
    assert result.page_number == position
    story = getattr(_open(result).sections[0], attr)
    numbered = [p for p in story.paragraphs if "PAGE" in p._p.xml and "instrText" in p._p.xml]
    assert len(numbered) == 1 and numbered[0].alignment == align


def test_page_number_replaces_the_default_page_x_of_y():
    from app.processing.docx_generator import render_markdown_to_docx

    base = render_markdown_to_docx("Body text.", template_docx_bytes=None, title="Notice")
    doc = _open(apply_format_layout(base, COURT))
    footer_text = [p.text for p in doc.sections[0].footer.paragraphs]
    assert footer_text == ["1"]  # the PAGE field's placeholder; "Page 1 of 1" is gone


def test_unknown_page_number_is_ignored_and_reported():
    result = apply_format_layout(_docx(), _variant(COURT, ["page", "pageNumber"], "outside_margin"))
    assert result.page_number == "unchanged"
    assert "page.pageNumber" in result.invalid_values


# ---------------------------------------------------------------- letterhead


def test_letterhead_header_table_and_footer():
    result = apply_format_layout(_docx(), LETTERHEAD)
    doc = _open(result)
    header = doc.sections[0].header
    (table,) = header.tables
    cells = [c.text for c in table.rows[0].cells]
    assert cells == ["Example & Associates", "", "Advocates"]
    assert "Hidden centre text" not in header._element.xml
    left_run = table.rows[0].cells[0].paragraphs[0].runs[0]
    assert left_run.bold is True
    assert left_run.font.name == "Calibri"
    assert "w:val=\"nil\"" in table._tbl.tblPr.xml  # borderless
    assert [p.text for p in doc.sections[0].footer.paragraphs if p.text] == [
        "1 Sample Road, Example City"]
    assert result.letterhead_blocks == 3
    assert doc.sections[0].different_first_page_header_footer is False


def test_single_block_is_a_paragraph_not_a_table():
    fl = copy.deepcopy(LETTERHEAD)
    lh = fl["formatting"]["letterhead"]
    lh["headerShowRight"] = False
    header = _open(apply_format_layout(_docx(), fl)).sections[0].header
    assert not header.tables
    assert header.paragraphs[0].text == "Example & Associates"


def test_html_overrides_text_and_bold_and_is_sanitised():
    fl = copy.deepcopy(LETTERHEAD)
    lh = fl["formatting"]["letterhead"]
    lh["headerHtmlLeft"] = (
        '<p onclick="x()">Example <b>Partners</b></p><div><i>Advocates</i>'
        '<script>alert(1)</script><u>Solicitors</u></div>'
    )
    header = _open(apply_format_layout(_docx(), fl)).sections[0].header
    cell = header.tables[0].rows[0].cells[0]
    runs = cell.paragraphs[0].runs
    texts = {r.text: r for r in runs if r.text}
    assert "Example & Associates" not in cell.text  # html overrides the plain text
    assert texts["Partners"].bold is True
    assert texts["Example "].bold is not True  # headerBoldLeft no longer applies
    assert texts["Advocates"].italic is True
    assert texts["Solicitors"].underline is True
    assert "alert" not in header._element.xml and "onclick" not in header._element.xml
    assert cell.text.split("\n")[0].strip() == "Example Partners"


def test_empty_html_hides_the_plain_text():
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"]["headerHtmlRight"] = ""
    header = _open(apply_format_layout(_docx(), fl)).sections[0].header
    assert not header.tables
    assert "Advocates" not in header._element.xml


def test_page_application_first_sets_title_page():
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"]["pageApplication"] = "first"
    s = _open(apply_format_layout(_docx(), fl)).sections[0]
    assert s.different_first_page_header_footer is True
    assert "Example &amp; Associates" in s.first_page_header._element.xml
    assert "Example" not in s.header._element.xml
    assert "Sample Road" in s.first_page_footer._element.xml
    assert "Sample Road" not in s.footer._element.xml


def test_letterhead_replaces_existing_header():
    doc = _open(apply_format_layout(_docx(header_text="Template firm header"), LETTERHEAD))
    assert "Template firm header" not in _header_xml(doc)
    assert "Old footer" not in doc.sections[0].footer._element.xml


def test_court_format_keeps_the_existing_header():
    doc = _open(apply_format_layout(_docx(header_text="Template firm header"), COURT))
    assert "Template firm header" in _header_xml(doc)


def test_letterhead_image_ok_and_failure_counted():
    fl = copy.deepcopy(LETTERHEAD)
    lh = fl["formatting"]["letterhead"]
    lh.update({"headerType": "image", "headerImageUrl": "https://images.example/banner.png",
               "footerType": "image", "footerImageUrl": "https://images.example/missing.png"})
    calls = []

    def fetch(url):
        calls.append(url)
        return _png() if url.endswith("banner.png") else None

    result = apply_format_layout(_docx(), fl, fetch_image=fetch)
    assert result.images_ok == 1 and result.images_failed == 1
    doc = _open(result)
    assert "<pic:pic" in _header_xml(doc)
    assert "<pic:pic" not in doc.sections[0].footer._element.xml


def test_image_fetch_exception_or_non_image_never_crashes():
    fl = copy.deepcopy(LETTERHEAD)
    lh = fl["formatting"]["letterhead"]
    lh.update({"headerType": "image", "headerImageLeft": "https://images.example/a.png",
               "headerImageRight": "https://images.example/b.gif"})

    def fetch(url):
        if url.endswith("a.png"):
            raise TimeoutError("simulated")
        return b"GIF89a not allowed"

    result = apply_format_layout(_docx(), fl, fetch_image=fetch)
    assert result.images_failed == 2 and result.images_ok == 0
    _open(result)


def test_no_fetcher_means_images_skipped():
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"].update(
        {"headerType": "image", "headerImageUrl": "https://images.example/x.png"})
    result = apply_format_layout(_docx(), fl)
    assert result.images_failed == 1


# ---------------------------------------------------------------- court


def test_court_blocks_inserted_in_order_and_styled():
    result = apply_format_layout(_docx(), COURT)
    doc = _open(result)
    texts = [p.text for p in doc.paragraphs]
    assert texts[:4] == [
        "IN THE HIGH COURT OF EXAMPLE",
        "(Civil Appellate Jurisdiction)",
        "Writ Petition No. ____ of 2026",
        "CNR No: XXXX000000002026",
    ]
    assert texts[4:] == BODY
    first = doc.paragraphs[0]
    assert first.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert first.runs[0].bold is True
    assert result.court_blocks == 4


def test_custom_field_without_text_uses_label_and_value():
    fl = copy.deepcopy(COURT)
    fl["formatting"]["court"]["customFields"] = [{"label": "Diary No", "value": "123/2026"}]
    texts = [p.text for p in _open(apply_format_layout(_docx(), fl)).paragraphs]
    assert "Diary No: 123/2026" in texts


def test_no_duplicate_when_the_draft_starts_with_the_court_header():
    body = ["In the High Court of Example", "Writ Petition No. 45 of 2026"] + BODY
    result = apply_format_layout(_docx(paragraphs=body), COURT)
    texts = [p.text for p in _open(result).paragraphs]
    assert sum(1 for t in texts if "high court of example" in t.lower()) == 1
    assert sum(1 for t in texts if t.lower().startswith("writ petition")) == 1
    assert result.court_duplicates_skipped == 2
    assert texts[0] == "(Civil Appellate Jurisdiction)"


def test_applying_twice_does_not_duplicate_the_court_block():
    once = apply_format_layout(_docx(), COURT)
    twice = apply_format_layout(once.docx_bytes, COURT)
    assert twice.court_blocks == 0
    assert [p.text for p in _open(twice).paragraphs] == [p.text for p in _open(once).paragraphs]


def test_court_format_ignores_letterhead_section():
    fl = copy.deepcopy(COURT)
    fl["formatting"]["letterhead"] = LETTERHEAD["formatting"]["letterhead"]
    doc = _open(apply_format_layout(_docx(), fl))
    assert "Advocates" not in _header_xml(doc)


# ---------------------------------------------------------------- robustness


def test_invalid_values_ignored_valid_ones_applied():
    fl = {"type": "court", "formatting": {
        "page": {"font": "<script>", "fontSize": "huge", "lineSpacing": "lots",
                 "pageSize": "B5", "orientation": "sideways",
                 "margins": {"top": "wide", "left": "2cm"}, "alignment": "diagonal",
                 "watermark": "SECRET"},
        "court": {"courtHeader": 7, "customFields": "nope",
                  "caseNumber": {"text": "Case No. 1 of 2026", "fontSize": "x", "align": "up"}},
    }}
    result = apply_format_layout(_docx(), fl)
    doc = _open(result)
    assert abs(doc.sections[0].left_margin - Mm(20)) < Mm(0.1)
    assert doc.paragraphs[0].text == "Case No. 1 of 2026"
    for path in ("page.font", "page.fontSize", "page.lineSpacing", "page.pageSize",
                 "page.orientation", "page.margins.top", "page.alignment",
                 "page.watermark", "court.courtHeader", "court.customFields"):
        assert path in result.invalid_values, path
    assert result.watermark == "unchanged"


def test_unknown_keys_reported_by_name_only():
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"]["contactBarPhone"] = "+00 0000 000000"
    fl["formatting"]["page"]["gutter"] = "1in"
    fl["formatting"]["theme"] = {}
    result = apply_format_layout(_docx(), fl)
    assert set(result.unknown_keys) == {"letterhead.contactBarPhone", "page.gutter", "theme"}
    fields = draft_format.summary_fields(result)
    assert fields["unknown_keys"] == 3
    assert "0000" not in fields["unknown_names"]


@pytest.mark.parametrize("bad", [None, "", "not json", 42, [], {"formatting": "x"}, {}])
def test_unusable_layout_returns_file_unchanged(bad):
    raw = _docx()
    result = apply_format_layout(raw, bad)
    assert result.docx_bytes == raw and result.format_type == "none"


def test_unreadable_docx_raises():
    with pytest.raises(Exception):
        apply_format_layout(b"not a docx", COURT)


def test_output_reopens_and_body_text_unchanged():
    for fl in (COURT, LETTERHEAD):
        doc = _open(apply_format_layout(_docx(), fl))
        assert [p.text for p in doc.paragraphs if p.text in BODY] == BODY


def test_json_string_layout_accepted():
    import json

    result = apply_format_layout(_docx(), json.dumps(COURT))
    assert result.court_blocks == 4


# ---------------------------------------------------------------- helpers


def test_select_format_layout_prefers_top_level_then_draft_origin():
    assert select_format_layout(None, COURT) is COURT
    assert select_format_layout(LETTERHEAD, COURT) is LETTERHEAD
    assert select_format_layout({"id": "x"}, None) is None


def test_without_image_links_strips_only_links():
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"].update(
        {"headerImageUrl": "https://x.example/a.png", "headerImageLeft": "https://x.example/b.png"})
    cleaned = without_image_links(fl)
    lh = cleaned["formatting"]["letterhead"]
    assert "headerImageUrl" not in lh and "headerImageLeft" not in lh
    assert lh["headerTextLeft"] == "Example & Associates"
    assert "headerImageUrl" in fl["formatting"]["letterhead"]  # original untouched


def test_default_fetcher_refuses_non_https_and_private_hosts():
    assert fetch_format_image("http://images.example/a.png") is None
    assert fetch_format_image("https://127.0.0.1/a.png") is None
    assert fetch_format_image("https://localhost/a.png") is None
    assert fetch_format_image("file:///etc/hosts") is None
    assert fetch_format_image("") is None


def test_image_kind_by_magic_bytes():
    assert image_kind(_png()) == "png"
    assert image_kind(b"\xff\xd8\xff\xe0rest") == "jpeg"
    assert image_kind(b"GIF89a") is None


# ---------------------------------------------------------------- export path


@pytest.fixture
def export_env(monkeypatch):
    from app.core.config import settings
    from app.storage import draft_files

    uploads = []
    monkeypatch.setattr(settings, "gcs_docx_bucket_name", "fictional-bucket", raising=False)

    def _upload(workspace_id, data):
        uploads.append(data)
        return "https://storage.example/drafts/x.docx?sig=y"

    monkeypatch.setattr(draft_files, "upload_draft_docx", _upload)
    return uploads


async def _export(markdown="# Petition\n\nThe petitioner submits as follows."):
    return await drafting_module.drafting_agent._export_draft_docx(
        markdown=markdown, draft_title="Writ Petition", tpl=None, user_id="u_fictional",
        template_id=None, workspace_id="ws_fictional", actionable_id="draft_fictional",
        version=1,
    )


async def test_export_applies_the_turn_format(export_env):
    token = drafting_module._TURN_FORMAT_LAYOUT.set(COURT)
    try:
        url = await _export()
    finally:
        drafting_module._TURN_FORMAT_LAYOUT.reset(token)
    assert url and len(export_env) == 1
    doc = Document(BytesIO(export_env[0]))
    assert doc.paragraphs[0].text == "IN THE HIGH COURT OF EXAMPLE"
    assert doc.sections[0].left_margin == Inches(1.5)


async def test_export_without_format_is_unchanged(export_env):
    await _export()
    doc = Document(BytesIO(export_env[0]))
    assert doc.paragraphs[0].text == "Petition"


async def test_export_survives_a_formatter_exception(export_env, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated formatter bug")

    monkeypatch.setattr(draft_format, "apply_format_layout", _boom)
    token = drafting_module._TURN_FORMAT_LAYOUT.set(COURT)
    try:
        url = await _export()
    finally:
        drafting_module._TURN_FORMAT_LAYOUT.reset(token)
    assert url, "the unformatted draft is still uploaded"
    doc = Document(BytesIO(export_env[0]))
    assert doc.paragraphs[0].text == "Petition"


# ---------------------------------------------------------------- session


def _install_turn_fakes(monkeypatch, session):
    saved = []
    seen = []

    async def _load(**_kw):
        return copy.deepcopy(session) if session is not None else None

    async def _save(*_a, **kw):
        saved.append(copy.deepcopy(kw["session_state"]))

    async def _chat(**kw):
        seen.append(drafting_module._TURN_FORMAT_LAYOUT.get())
        state = kw["session_state"]
        state["last_assistant_message"] = "Hello."
        return state

    monkeypatch.setattr(drafting_module.drafting_agent, "_load_session_state", _load)
    monkeypatch.setattr(drafting_module.drafting_agent, "_save_session_state", _save)
    monkeypatch.setattr(drafting_module.drafting_agent, "_handle_chat_question", _chat)
    return saved, seen


def _request(**extra):
    base = {"actionableId": "draft_fictional", "workspaceId": "ws_fictional",
            "userId": "u_fictional"}
    base.update(extra)
    return base


async def test_turn_stores_format_from_draft_origin_without_image_links(monkeypatch):
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"]["headerImageUrl"] = "https://x.example/a.png"
    saved, seen = _install_turn_fakes(monkeypatch, session=None)
    await drafting_module.drafting_agent.handle_turn(
        user_message="hi", case_context={},
        request_fields=_request(draftOrigin={"formatLayout": fl}),
        conversation_history=[],
    )
    assert seen == [fl]  # the turn itself uses the links
    stored = saved[-1]["format_layout"]
    assert stored["type"] == "letterhead"
    assert "headerImageUrl" not in stored["formatting"]["letterhead"]
    assert drafting_module._TURN_FORMAT_LAYOUT.get() is None  # reset after the turn


async def test_turn_without_format_keeps_the_session_format(monkeypatch):
    saved, seen = _install_turn_fakes(monkeypatch, session={"format_layout": COURT})
    await drafting_module.drafting_agent.handle_turn(
        user_message="hi", case_context={}, request_fields=_request(),
        conversation_history=[],
    )
    assert seen == [COURT]
    assert saved[-1]["format_layout"] == COURT


def test_template_direct_formatting_follows_the_chosen_font_and_size():
    doc = Document()
    for text in BODY:
        run = doc.add_paragraph().add_run(text)
        run.font.name = "Arial"
        run.font.size = Pt(10)
    big = doc.add_paragraph().add_run("Schedule")
    big.font.size = Pt(16)
    buf = BytesIO()
    doc.save(buf)
    out = _open(apply_format_layout(buf.getvalue(), COURT))
    body_runs = [p.runs[0] for p in out.paragraphs if p.text in BODY]
    assert all(r.font.name == "Times New Roman" for r in body_runs)
    assert all(r.font.size == Pt(12) for r in body_runs)
    schedule = next(p.runs[0] for p in out.paragraphs if p.text == "Schedule")
    assert schedule.font.size == Pt(16)  # a deliberately larger run keeps its size


# ------------------------------------------------- added in verification (QA)


def test_unknown_page_number_leaves_the_existing_footer_as_it_was():
    # AC-7 "file unchanged there": the default "Page X of Y" footer survives.
    from app.processing.docx_generator import render_markdown_to_docx

    base = render_markdown_to_docx("Body text.", template_docx_bytes=None, title="Notice")
    before = [p.text for p in Document(BytesIO(base)).sections[0].footer.paragraphs]
    fl = _variant(COURT, ["page", "pageNumber"], "outside_margin")
    after_doc = _open(apply_format_layout(base, fl))
    after = [p.text for p in after_doc.sections[0].footer.paragraphs]
    assert before == after
    assert any(t.strip() for t in before), "the base file has a page-number footer"


def test_default_fetcher_makes_no_request_for_refused_links(monkeypatch):
    # AC-24 "refused without a request": no HTTP client is ever opened.
    import httpx

    opened = []

    class _Recorder:
        def __init__(self, *a, **kw):
            opened.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, *a, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("a request was made")

    monkeypatch.setattr(httpx, "Client", _Recorder)
    for url in ("http://images.example/a.png", "https://127.0.0.1/a.png",
                "https://localhost/a.png", "https://169.254.169.254/latest",
                "https://10.0.0.5/a.png", "file:///etc/hosts", ""):
        assert fetch_format_image(url) is None, url
    assert opened == []


async def test_later_turn_uses_the_format_the_first_turn_stored(monkeypatch):
    # AC-26 end to end: turn 1 stores the format (no image links); turn 2,
    # sent without a format, uses exactly what turn 1 stored.
    from app.ai.gemini_client import gemini_client

    async def _no_model(*_a, **_kw):
        raise AssertionError("no model call in this test")

    monkeypatch.setattr(gemini_client, "generate", _no_model)
    monkeypatch.setattr(gemini_client, "generate_longform", _no_model)
    fl = copy.deepcopy(LETTERHEAD)
    fl["formatting"]["letterhead"]["headerImageUrl"] = "https://x.example/a.png"
    saved, seen = _install_turn_fakes(monkeypatch, session=None)
    await drafting_module.drafting_agent.handle_turn(
        user_message="hi", case_context={},
        request_fields=_request(draftOrigin={"formatLayout": fl}),
        conversation_history=[],
    )
    stored = saved[-1]
    # Turn 2's session carries only what turn 1 persisted for the format.
    saved2, seen2 = _install_turn_fakes(
        monkeypatch, session={"format_layout": stored["format_layout"]})
    await drafting_module.drafting_agent.handle_turn(
        user_message="hi", case_context={}, request_fields=_request(),
        conversation_history=[],
    )
    assert seen2 == [stored["format_layout"]]
    assert "headerImageUrl" not in seen2[0]["formatting"]["letterhead"]
    assert seen2[0]["formatting"]["letterhead"]["headerTextLeft"] == "Example & Associates"


# ---------------------------------------------------------------- SIM-226
# A draft built from an uploaded template keeps the template's page layout.


def _indent_laid_out_template() -> bytes:
    """Like a PDF-converted template: near-zero margins, text placed by indents.

    US Letter, so the format's A4 ``pageSize`` would visibly change it.
    """
    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Inches(8.5), Inches(11)
    s.left_margin, s.right_margin = Inches(0.42), Inches(0.08)
    s.top_margin, s.bottom_margin = Inches(0.99), Inches(0.88)
    title = doc.add_paragraph("SAMPLE AGREEMENT")
    title.paragraph_format.left_indent = Inches(3.12)
    title.paragraph_format.right_indent = Inches(3.45)
    for text in BODY:
        p = doc.add_paragraph(text)
        p.paragraph_format.left_indent = Inches(0.63)
        p.paragraph_format.right_indent = Inches(0.96)
        p.runs[0].font.name = "Bookman Old Style"
        p.runs[0].font.size = Pt(13)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


@pytest.mark.parametrize("fmt", [COURT, LETTERHEAD], ids=["court", "letterhead"])
def test_template_draft_keeps_page_layout(fmt):
    before = Document(BytesIO(_indent_laid_out_template()))
    result = apply_format_layout(_indent_laid_out_template(), fmt, keep_body_layout=True)
    doc = _open(result)
    s, s0 = doc.sections[0], before.sections[0]
    for side in ("left", "right", "top", "bottom"):
        assert getattr(s, f"{side}_margin") == getattr(s0, f"{side}_margin")
    assert (s.page_width, s.page_height) == (s0.page_width, s0.page_height)
    assert s.orientation == s0.orientation
    # Style-level body formatting (what an unformatted run renders in).
    n, n0 = doc.styles["Normal"], before.styles["Normal"]
    assert (n.font.name, n.font.size) == (n0.font.name, n0.font.size)
    assert n.paragraph_format.line_spacing == n0.paragraph_format.line_spacing
    assert n.paragraph_format.alignment == n0.paragraph_format.alignment
    paras = [p for p in doc.paragraphs if p.text in ["SAMPLE AGREEMENT", *BODY]]
    paras0 = [p for p in before.paragraphs if p.text in ["SAMPLE AGREEMENT", *BODY]]
    assert len(paras) == len(paras0) == 3
    for p, p0 in zip(paras, paras0):
        pf, pf0 = p.paragraph_format, p0.paragraph_format
        assert (pf.left_indent, pf.right_indent) == (pf0.left_indent, pf0.right_indent)
        assert pf.alignment == pf0.alignment
        assert pf.line_spacing == pf0.line_spacing
        assert [(r.font.name, r.font.size) for r in p.runs] == [
            (r.font.name, r.font.size) for r in p0.runs
        ]
    assert result.body_layout == "kept"
    assert result.body_settings_skipped > 0


def test_template_draft_still_gets_letterhead_court_and_page_number():
    lh = apply_format_layout(_indent_laid_out_template(), LETTERHEAD, keep_body_layout=True)
    assert "Example &amp; Associates" in _header_xml(_open(lh))
    assert lh.letterhead_blocks > 0

    court = apply_format_layout(_indent_laid_out_template(), COURT, keep_body_layout=True)
    doc = _open(court)
    assert doc.paragraphs[0].text.upper().startswith("IN THE HIGH COURT OF EXAMPLE")
    assert court.court_blocks > 0
    assert court.page_number == "center_bottom"
    assert "PAGE" in _header_xml(doc, "footer")


def test_no_template_draft_still_gets_format_page_settings():
    result = apply_format_layout(_indent_laid_out_template(), COURT)
    doc = _open(result)
    s = doc.sections[0]
    assert s.left_margin == Inches(1.5) and s.right_margin == Inches(1)
    assert s.top_margin == Inches(1) and s.bottom_margin == Inches(1)
    assert abs(s.page_width - Mm(210)) < Mm(1) and abs(s.page_height - Mm(297)) < Mm(1)
    body = [p for p in doc.paragraphs if p.text in BODY]
    assert body and all(p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY for p in body)
    assert doc.styles["Normal"].font.name == "Times New Roman"
    assert result.body_layout == "applied"
    assert result.body_settings_skipped == 0


def test_summary_fields_report_body_layout():
    kept = apply_format_layout(_indent_laid_out_template(), COURT, keep_body_layout=True)
    f = draft_format.summary_fields(kept)
    assert f["body_layout"] == "kept" and f["body_skipped"] == kept.body_settings_skipped
    assert draft_format.summary_fields(apply_format_layout(_docx(), COURT))["body_layout"] == "applied"


@pytest.mark.parametrize(
    "route,keep",
    [("defaults", False), ("llm_diff", True), ("template_fill", True),
     ("heuristic_fill", True), ("skeleton_clone", True)],
)
def test_chosen_format_keeps_layout_on_every_template_route(monkeypatch, route, keep):
    seen = {}

    def fake_apply(docx_bytes, format_layout, *, fetch_image=None, keep_body_layout=False):
        seen["keep"] = keep_body_layout
        return draft_format.FormatResult(docx_bytes=docx_bytes)

    monkeypatch.setattr(draft_format, "apply_format_layout", fake_apply)
    agent = drafting_module.DraftingAgent.__new__(drafting_module.DraftingAgent)
    token = drafting_module._TURN_FORMAT_LAYOUT.set(COURT)
    try:
        import asyncio

        out = asyncio.run(agent._apply_chosen_format(b"docx", route=route))
    finally:
        drafting_module._TURN_FORMAT_LAYOUT.reset(token)
    assert out == b"docx"
    assert seen["keep"] is keep


# ---------------------------------------------------------------------------
# SIM-229: a "Page X of Y" footer whose fields were flattened to plain text
# ---------------------------------------------------------------------------

_GALLERY_FOOTER = (
    '<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:sdtPr><w:docPartObj><w:docPartGallery w:val="Page Numbers (Bottom of Page)"/>'
    '<w:docPartUnique/></w:docPartObj></w:sdtPr><w:sdtContent>'
    '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Page Numbers (Top of Page)"/>'
    '<w:docPartUnique/></w:docPartObj></w:sdtPr><w:sdtContent>'
    '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
    '<w:r><w:t xml:space="preserve">{lead}</w:t></w:r>'
    '<w:r><w:rPr><w:b/></w:rPr><w:t>3</w:t></w:r>'
    '<w:r><w:t xml:space="preserve"> of </w:t></w:r>'
    '<w:r><w:rPr><w:b/></w:rPr><w:t>16</w:t></w:r></w:p>'
    '</w:sdtContent></w:sdt></w:sdtContent></w:sdt>'
)


def _frozen_docx(lead: str = "Page ", gallery: bool = True, footer_text: str = "") -> bytes:
    from docx.oxml.parser import parse_xml

    doc = Document()
    for text in BODY:
        doc.add_paragraph(text)
    ftr = doc.sections[0].footer._element
    for p in list(ftr):
        ftr.remove(p)
    if footer_text:
        doc.sections[0].footer.add_paragraph(footer_text)
    elif gallery:
        ftr.append(parse_xml(_GALLERY_FOOTER.format(lead=lead)))
    else:
        p = doc.sections[0].footer.add_paragraph()
        p.add_run(lead)
        p.add_run("3")
        p.add_run(" of ")
        p.add_run("16")
    doc.sections[0].footer.add_paragraph()
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _footer_fields(data: bytes):
    doc = Document(BytesIO(data))
    ftr = doc.sections[0].footer._element
    return [(t.text or "").strip() for t in ftr.iter(qn("w:instrText"))]


def _footer_text(data: bytes) -> str:
    doc = Document(BytesIO(data))
    return "".join(t.text or "" for t in doc.sections[0].footer._element.iter(qn("w:t")))


@pytest.mark.parametrize("gallery", [True, False])
def test_frozen_page_x_of_y_becomes_live_fields(gallery):
    fixed, n = draft_format.repair_frozen_page_numbers_bytes(_frozen_docx(gallery=gallery))
    assert n == 1
    assert _footer_fields(fixed) == ["PAGE", "NUMPAGES"]
    # The wording and the bold numbers stay as the template had them.
    assert _footer_text(fixed) == "Page 3 of 16"
    if not gallery:
        return
    doc = Document(BytesIO(fixed))
    bold = [r for r in doc.sections[0].footer._element.iter(qn("w:r"))
            if r.find(qn("w:instrText")) is not None]
    assert all(r.find(qn("w:rPr")).find(qn("w:b")) is not None for r in bold)


def test_bare_number_is_repaired_only_inside_a_page_number_gallery():
    fixed, n = draft_format.repair_frozen_page_numbers_bytes(_frozen_docx(lead=""))
    assert n == 1 and _footer_fields(fixed) == ["PAGE", "NUMPAGES"]
    # Outside the gallery a bare "3 of 16" could be anything: left alone.
    data = _frozen_docx(lead="", gallery=False)
    assert draft_format.repair_frozen_page_numbers_bytes(data) == (data, 0)


@pytest.mark.parametrize("text", [
    "Registered office: Plot 16, Example Road",
    "Page 3 of the schedule",
    "Example Firm LLP",
])
def test_other_footer_text_is_never_touched(text):
    data = _frozen_docx(footer_text=text)
    assert draft_format.repair_frozen_page_numbers_bytes(data) == (data, 0)


def test_body_text_is_never_touched():
    doc = Document()
    doc.add_paragraph("Page 3 of 16")
    out = BytesIO()
    doc.save(out)
    data = out.getvalue()
    assert draft_format.repair_frozen_page_numbers_bytes(data) == (data, 0)


def test_template_draft_keeps_its_page_x_of_y_and_adds_no_second_number():
    fmt = {"type": "letterhead", "formatting": {"page": {"pageNumber": "bottom-center"}}}
    r = apply_format_layout(_frozen_docx(), fmt, keep_body_layout=True)
    assert r.page_numbers_repaired == 1
    assert r.page_number == "template"
    assert _footer_fields(r.docx_bytes) == ["PAGE", "NUMPAGES"]


def test_no_template_draft_takes_the_formats_page_number_once():
    fmt = {"type": "letterhead", "formatting": {"page": {"pageNumber": "bottom-center"}}}
    r = apply_format_layout(_frozen_docx(), fmt, keep_body_layout=False)
    assert r.page_number == "center_bottom"
    assert _footer_fields(r.docx_bytes) == ["PAGE"]
    # The emptied nested page-number content controls are removed, not left empty.
    ftr = Document(BytesIO(r.docx_bytes)).sections[0].footer._element
    assert all(len(c) for c in ftr.iter(qn("w:sdtContent")))


def test_page_number_none_removes_the_repaired_number():
    fmt = {"type": "letterhead", "formatting": {"page": {"pageNumber": "none"}}}
    r = apply_format_layout(_frozen_docx(), fmt, keep_body_layout=True)
    assert r.page_number == "none"
    assert _footer_fields(r.docx_bytes) == []
    assert "16" not in _footer_text(r.docx_bytes)


def test_format_page_number_elsewhere_replaces_the_templates():
    fmt = {"type": "letterhead", "formatting": {"page": {"pageNumber": "top-right"}}}
    r = apply_format_layout(_frozen_docx(), fmt, keep_body_layout=True)
    assert r.page_number == "right_top"
    assert _footer_fields(r.docx_bytes) == []
    hdr = Document(BytesIO(r.docx_bytes)).sections[0].header._element
    assert [(t.text or "").strip() for t in hdr.iter(qn("w:instrText"))] == ["PAGE"]


def test_summary_reports_the_repair():
    fmt = {"type": "letterhead", "formatting": {"page": {"watermark": "DRAFT"}}}
    r = apply_format_layout(_frozen_docx(), fmt, keep_body_layout=True)
    assert draft_format.summary_fields(r)["page_numbers_repaired"] == 1
    assert _footer_fields(r.docx_bytes) == ["PAGE", "NUMPAGES"]


async def test_template_draft_without_a_format_is_repaired(monkeypatch):
    agent = drafting_module.DraftingAgent.__new__(drafting_module.DraftingAgent)
    token = drafting_module._TURN_FORMAT_LAYOUT.set(None)
    try:
        out = await agent._apply_chosen_format(_frozen_docx(), route="skeleton_clone")
        assert _footer_fields(out) == ["PAGE", "NUMPAGES"]
        # A no-template draft is left exactly as built.
        data = _frozen_docx()
        assert await agent._apply_chosen_format(data, route="defaults") is data
    finally:
        drafting_module._TURN_FORMAT_LAYOUT.reset(token)
