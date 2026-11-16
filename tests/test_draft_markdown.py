"""SIM-202: draft Markdown normaliser. Fictional fixtures only."""

from pathlib import Path

from app.processing.draft_markdown import count_placeholders, normalize_draft_markdown

GOLDEN = Path(__file__).parent / "fixtures" / "drafting" / "golden_addendum.md"


def _table_blocks(md: str) -> list:
    """Contiguous runs of pipe lines."""
    blocks, cur = [], []
    for line in md.split("\n"):
        if line.lstrip().startswith("|"):
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def test_table_rows_joined():
    md = "Intro\n\n| A | B |\n\n| --- | --- |\n\n| 1 | 2 |\n\n| 3 | 4 |\n\nAfter"
    out = normalize_draft_markdown(md)
    blocks = _table_blocks(out)
    assert len(blocks) == 1
    assert blocks[0] == ["| A | B |", "| --- | --- |", "| 1 | 2 |", "| 3 | 4 |"]
    assert "Intro\n\n| A | B |" in out
    assert "| 3 | 4 |\n\nAfter" in out


def test_table_separator_and_padding():
    md = "| A | B | C |\n| 1 | 2 |\n| x | y | z | extra |"
    out = normalize_draft_markdown(md).split("\n")
    assert out[0] == "| A | B | C |"
    assert out[1] == "| --- | --- | --- |"
    assert out[2] == "| 1 | 2 |  |"
    assert out[3] == "| x | y | z extra |"


def test_alignment_row_kept():
    md = "| A | B |\n| :--- | ---: |\n| 1 | 2 |"
    out = normalize_draft_markdown(md).split("\n")
    assert out[1] == "| :--- | ---: |"


def test_escaped_pipe_inside_cell_not_split():
    md = "| A | B |\n| --- | --- |\n| a \\| b | c |"
    out = normalize_draft_markdown(md).split("\n")
    assert out[2] == "| a \\| b | c |"


def test_placeholders_unescaped():
    md = "Bank: \\<Bank Name\\>, IBAN \\<IBAN\\_Number\\> and <Day> and \\<Half>"
    out = normalize_draft_markdown(md)
    assert out == "Bank: <Bank Name>, IBAN <IBAN_Number> and <Day> and <Half>"


def test_placeholders_unescaped_inside_table():
    md = "| Field | Value |\n\n| :--- | :--- |\n\n| Bank | \\<Bank Name\\> |"
    out = normalize_draft_markdown(md)
    assert "| Bank | <Bank Name> |" in out
    assert "\\<" not in out


def test_headings_and_rules():
    md = "Text\n\n---\n\n#### Deep\n\n###### Deeper\n\n***\n\n## Kept"
    out = normalize_draft_markdown(md)
    assert "---" not in out
    assert "***" not in out
    assert "### Deep" in out
    assert "### Deeper" in out
    assert "## Kept" in out
    assert "####" not in out


def test_setext_underline_kept():
    # "Title\n---" is a level-2 heading in Markdown, not a rule.
    md = "Title\n---\n\nBody"
    assert normalize_draft_markdown(md) == md


def test_signature_underscores_kept():
    md = "Signed by  \n_____________________  \nWitness"
    assert "_____________________" in normalize_draft_markdown(md)


def test_nested_subclauses():
    md = "- The Company shall:\n\n    (a) pass a resolution;\n    (b) update the register."
    out = normalize_draft_markdown(md)
    assert "  - (a) pass a resolution;" in out
    assert "  - (b) update the register." in out


def test_subclause_not_touched_outside_list():
    md = "Clause 3:\n\n    (a) indented code-like line"
    assert normalize_draft_markdown(md) == md


def test_fenced_code_untouched():
    md = "```\n| a | b |\n\n| c | d |\n---\n\\<X\\>\n```"
    assert normalize_draft_markdown(md) == md


def test_blank_lines_collapsed():
    assert normalize_draft_markdown("A\n\n\n\nB\n\n") == "A\n\nB"


def test_single_pipe_line_left_alone():
    md = "Choose one | or the other"
    assert normalize_draft_markdown(md) == md
    md2 = "| just one row |\n\nThen prose."
    assert normalize_draft_markdown(md2) == md2


def test_idempotent():
    for md in (GOLDEN.read_text(encoding="utf-8"),
               "| A |\n\n| 1 |\n\n#### H\n\n- x\n    (a) y"):
        once = normalize_draft_markdown(md)
        assert normalize_draft_markdown(once) == once


def test_golden_tables_and_escapes():
    out = normalize_draft_markdown(GOLDEN.read_text(encoding="utf-8"))
    blocks = _table_blocks(out)
    assert len(blocks) == 3
    assert [len(b) for b in blocks] == [6, 5, 5]
    assert "\\<" not in out
    assert "####" not in out
    assert "\n---\n" not in out


def test_count_placeholders():
    md = "<Day> <Month> <day> <br> <Bank Name> <Bank Name>"
    assert count_placeholders(md) == 3  # day/Day dedupe, <br> excluded
    assert count_placeholders("") == 0


def test_setting_off(monkeypatch):
    from app.core.config import settings
    from app.processing.draft_markdown import normalize_if_enabled

    md = "#### Deep\n\n\\<X\\>"
    monkeypatch.setattr(settings, "draft_markdown_normalize", False)
    assert normalize_if_enabled(md) == md
    monkeypatch.setattr(settings, "draft_markdown_normalize", True)
    assert normalize_if_enabled(md) == "### Deep\n\n<X>"


def test_empty_input():
    assert normalize_draft_markdown("") == ""
    assert normalize_draft_markdown(None) == ""
