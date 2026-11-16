"""SIM-209: table totals re-added in code. Fictional data only."""

from app.processing.draft_totals import check_table_totals, format_number, parse_number

CAP_TABLE = """Intro.

| Shareholder | Class | Shares | Investment (USD) | Stake (%) |
| --- | --- | ---: | ---: | ---: |
| Founder Holder | B | 621,166 | USD 1,200,000 | 60.25% |
| Partner One | A | 50,000 | — | 4.85% |
| Partner Two | A | 48,334 | USD 425,000 | 4.69% |
| Partner Three | A | 30,000 | — | 2.91% |
| Investor One | A | 13,333 | USD 200,000 | 2.79% |
| Investor Two | A | — | — | 1.46% |
| Partner Four | A | 11,667 | USD 175,000 | 1.13% |
| Partner Five | A | 10,000 | — | 0.97% |
| Reserve | A | 215,500 | — | 20.95% |
| **TOTAL** | — | **990,000** | USD 2,000,000 | 100.00% |
"""


def test_wrong_total_is_flagged_right_ones_left():
    out, issues = check_table_totals(CAP_TABLE)
    assert [(i.column, i.stated, i.computed) for i in issues] == [(2, "990,000", "1,000,000")]
    total_row = next(ln for ln in out.split("\n") if "TOTAL" in ln)
    assert "<check total: 1,000,000>" in total_row
    assert "USD 2,000,000" in total_row and "100.00%" in total_row  # correct ones untouched


def test_fix_mode_replaces_the_figure():
    out, issues = check_table_totals(CAP_TABLE, fix=True)
    assert len(issues) == 1
    total_row = next(ln for ln in out.split("\n") if "TOTAL" in ln)
    assert "**1,000,000**" in total_row and "check total" not in total_row


def test_indian_grouping_and_currency_kept():
    md = ("| Item | Amount |\n| --- | --- |\n| A | INR 1,20,00,000 |\n| B | INR 45,00,000 |\n"
          "| Total | INR 1,60,00,000 |\n")
    out, issues = check_table_totals(md, fix=True)
    assert issues[0].computed == "INR 1,65,00,000"
    assert "INR 1,65,00,000" in out


def test_placeholder_or_text_makes_column_unverifiable():
    md = ("| Item | Shares |\n| --- | --- |\n| A | <Allotted Shares> |\n| B | 100 |\n"
          "| Total | 999 |\n")
    assert check_table_totals(md)[1] == []


def test_percent_rounding_tolerated():
    md = ("| Name | Stake |\n| --- | --- |\n| A | 33.33% |\n| B | 33.33% |\n| C | 33.33% |\n"
          "| Total | 100.00% |\n")
    assert check_table_totals(md)[1] == []


def test_subtotals_each_cover_their_own_block():
    md = ("| Item | Amount |\n| --- | --- |\n| A | 10 |\n| B | 20 |\n| Sub-total | 30 |\n"
          "| C | 5 |\n| D | 5 |\n| Sub-total | 11 |\n")
    _out, issues = check_table_totals(md)
    assert [(i.row, i.computed) for i in issues] == [(5, "10")]


def test_tables_without_totals_and_prose_untouched():
    md = "No table here.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n"
    assert check_table_totals(md) == (md, [])
    assert check_table_totals("") == ("", [])


def test_number_parsing_and_formatting():
    n = parse_number("**Rs. 1,50,000/-**")
    assert n is not None and n.value == 150000 and n.indian and n.post == "/-"
    assert format_number(n.value + 1000, n) == "Rs. 1,51,000/-"
    assert parse_number("<Day>") is None and parse_number("Clause 3") is None
