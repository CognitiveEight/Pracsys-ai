"""Spreadsheet evidence end to end: parse, redact, render, store, compute, cite.

Each test pins one way a table used to lose accuracy:

* the prose PII redactor masked every phone number next to an "Account No"
  header, and ~1 in 10 transaction refs on checksum alone;
* chunks repeated rows (overlap), so adjacent chunks double-counted;
* retrieval showed a few blocks of a sheet and the model added rows by eye;
* ISO dates were checked by their year only, "15.5" matched "115.5", and
  chronology entries from a sheet could never be cited;
* the graph extractor merged "Player 1" into "Player 10".
"""

import asyncio
import datetime as dt
import os
import uuid
from decimal import Decimal
from io import BytesIO

import pytest

openpyxl = pytest.importorskip("openpyxl")

from app.processing import tables  # noqa: E402
from app.processing.tables import (  # noqa: E402
    KIND_DATE,
    KIND_IDENTIFIER,
    KIND_NUMBER,
    KIND_TEXT,
    format_indian,
    manifest,
    parse_workbook,
    redact_tableset,
    render_text,
    row_records,
)
from app.rag import table_query  # noqa: E402
from app.rag.table_query import SpecError, TableRef, execute, needs_calculation, validate  # noqa: E402


def _save(wb) -> bytes:
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _ledger(rows: int = 6) -> bytes:
    """Title row, header at row 3 starting in column B, a Total row, a blank row."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payments"
    ws["B1"] = "Payments ledger FY 2026"
    header = ["S.No", "Payee", "Mobile", "Account No", "Txn Ref", "Date", "Amount (INR)", "Status"]
    for j, h in enumerate(header):
        ws.cell(row=3, column=2 + j, value=h)
    statuses = ["Paid", "Pending", "Paid"]
    for i in range(1, rows + 1):
        r = 3 + i
        values = [
            i,
            f"Vendor {i:03d}",
            f"98765{43210 + i}",
            12345678900 + i,
            str(234567890120 + i),  # 12 digits: must never be masked as Aadhaar
            dt.datetime(2026, 3, 1) + dt.timedelta(days=i - 1),
            1000.5 * i,
            statuses[i % 3],
        ]
        for j, v in enumerate(values):
            ws.cell(row=r, column=2 + j, value=v)
    ws.cell(row=4 + rows, column=2)  # blank row
    ws.cell(row=5 + rows, column=2, value="Total")
    ws.cell(row=5 + rows, column=8, value=sum(1000.5 * i for i in range(1, rows + 1)))
    return _save(wb)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_header_found_below_title_rows_and_total_row_excluded():
    ts = parse_workbook(_ledger())
    sheet = ts.sheets[0]
    assert sheet.header_row == 3
    assert [r.number for r in sheet.preamble] == [1]
    assert [c.name for c in sheet.columns][:3] == ["S.No", "Payee", "Mobile"]
    assert [r.number for r in sheet.data_rows] == [4, 5, 6, 7, 8, 9]
    assert [r.number for r in sheet.total_rows] == [11]
    kinds = {c.name: c.kind for c in sheet.columns}
    assert kinds["S.No"] == KIND_IDENTIFIER  # never summed
    assert kinds["Mobile"] == KIND_IDENTIFIER
    assert kinds["Amount (INR)"] == KIND_NUMBER
    assert kinds["Date"] == KIND_DATE
    assert kinds["Status"] == KIND_TEXT


def test_text_numbers_text_dates_percentages_and_unsaved_formulas():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Amount", "Paid on", "Share", "Check"])
    ws.append(["a", "1,25,000", "05/04/2026", 0.155, "=B2*2"])
    ws.append(["b", "₹ 500", "6 Apr 2026", 0.5, "=B3*2"])
    ws.append(["c", "(1,200)", "2026-04-07", 0.25, None])
    for row in (2, 3, 4):
        ws.cell(row=row, column=4).number_format = "0.0%"
    ts = parse_workbook(_save(wb))
    sheet = ts.sheets[0]
    amounts = [r.cells[1].value for r in sheet.rows]
    assert amounts == [125000, 500, -1200]
    # Day-first: 05/04/2026 is 5 April, the Indian convention.
    assert [r.cells[2].value for r in sheet.rows] == ["2026-04-05", "2026-04-06", "2026-04-07"]
    assert sheet.day_first_dates
    assert [r.cells[3].display for r in sheet.rows] == ["15.5%", "50%", "25%"]
    # openpyxl wrote the formulas without values: kept as text, excluded, counted.
    assert sheet.uncomputed_formulas == 2
    assert sheet.rows[0].cells[4].display == "=B2*2" and sheet.rows[0].cells[4].value is None
    summary = tables.summary_text(ts)
    assert "Amount (number): 3 filled; sum 124300" in summary
    assert "2 formula cell(s) have no saved value" in summary
    assert "read as DD/MM/YYYY" in summary


def test_single_row_sheet_is_data_not_a_header():
    wb = openpyxl.Workbook()
    wb.active.append(["Remark", "Re-test ordered for Player 2"])
    ts = parse_workbook(_save(wb))
    text, _ = render_text(ts)
    assert ts.sheets[0].header_row is None
    assert "Remark | Re-test ordered for Player 2" in text


def test_summary_figures_are_exact_decimals():
    ts = parse_workbook(_ledger(6))
    summary = tables.summary_text(ts)
    # 1000.5 * (1+...+6) = 21010.5 exactly; the Total row (also 21010.5) is
    # excluded, so the figure is not doubled.
    assert "Amount (INR) (number): 6 filled; sum 21010.5; min 1000.5; max 6003" in summary
    assert "Status (text): 6 filled; Paid 4, Pending 2" in summary
    assert "1 total row(s) (row 11) excluded" in summary


def test_indian_grouping():
    assert format_indian(Decimal("216210974")) == "21,62,10,974"
    assert format_indian(Decimal("75432105.5"), 2) == "7,54,32,105.50"
    assert format_indian(Decimal("999")) == "999"
    assert format_indian(Decimal("-125000")) == "-1,25,000"


# ---------------------------------------------------------------------------
# Column-aware PII redaction
# ---------------------------------------------------------------------------


def test_redaction_follows_the_column_not_an_80_char_window():
    ts = parse_workbook(_ledger(6))
    red, scan = redact_tableset(ts)
    sheet = red.sheets[0]
    col = {c.name: i for i, c in enumerate(sheet.columns)}
    for row in sheet.data_rows:
        # Every account number masked: consistently, the whole column.
        assert row.cells[col["Account No"]].display.startswith("XXXXXXX")
        # Phones are kept (project policy), and 12-digit refs are not Aadhaar.
        assert not row.cells[col["Mobile"]].display.startswith("X")
        assert not row.cells[col["Txn Ref"]].display.startswith("X")
        # Amounts are untouched and still numbers.
        assert row.cells[col["Amount (INR)"]].kind == KIND_NUMBER
    assert len(scan.sensitive_fields()["bank_account_number"]) == 6
    # Raw values reach sensitive_fields only; the rendered text is masked.
    text, _ = render_text(red)
    assert "12345678901" not in text


def test_identifier_cue_inside_a_cell_still_masks():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Party", "Remarks", "Notes"])
    ws.append(["A", "Aadhaar 2345 6789 0124 submitted", "A/c 1234567890 frozen"])
    ws.append(["B", "nothing to note", "Roll 234567890124"])
    red, _ = redact_tableset(parse_workbook(_save(wb)))
    rows = red.sheets[0].rows
    assert "XXXX-XXXX-0124" in rows[0].cells[1].display
    assert "XXXXXX7890" in rows[0].cells[2].display
    assert rows[1].cells[2].display == "Roll 234567890124"


def test_strict_context_scan_does_not_mask_codes_without_their_cue():
    from app.processing.pii_detector import scan_text

    # Prose behaviour unchanged.
    assert scan_text("Passport A1234567 issued").matches
    # A part number in a table cell is not a passport.
    assert not scan_text("Part code: A1234567", strict_context=True).matches
    assert scan_text("Passport No: A1234567", strict_context=True).matches


# ---------------------------------------------------------------------------
# Rendering, chunking, row locations
# ---------------------------------------------------------------------------


def test_row_index_points_at_each_row_line():
    ts = parse_workbook(_ledger(6))
    text, index = render_text(ts)
    for entry in index:
        line = text[entry.offset:].split("\n", 1)[0]
        if entry.row == 1:
            assert line == "Payments ledger FY 2026"
        elif entry.row == 11:
            assert line.startswith("Total")
        else:
            assert line.startswith(f"{entry.row - 3} | Vendor {entry.row - 3:03d}")


def test_prose_chunks_keep_their_overlap():
    """Turning overlap off for spreadsheets must leave every other document's
    chunking exactly as it was (a shadowed flag once disabled it everywhere)."""
    from app.rag.chunker import chunk_text, reassemble

    prose = "\n\n".join(
        f"{i}. The respondent failed to pay the instalment due under clause {i} of the agreement."
        * 4
        for i in range(1, 40)
    )
    chunks = chunk_text(prose)
    assert len(chunks) > 3
    assert all(c.overlap_prev > 0 for c in chunks[1:])
    assert reassemble([{"content": c.text, "overlap_prev": c.overlap_prev} for c in chunks]) == prose


def test_spreadsheet_chunks_have_no_overlap_and_carry_rows():
    from app.rag.chunker import chunk_text
    from app.vectorstore.qdrant import _chunk_row_metadata

    ts = parse_workbook(_ledger(400))
    text, index = render_text(ts)
    chunks = chunk_text(text, with_overlap=False)
    assert len(chunks) > 5
    assert all(c.overlap_prev == 0 for c in chunks)
    assert "".join(c.text for c in chunks) == text
    seen_rows = []
    for c in chunks:
        meta = _chunk_row_metadata(c, index)
        if not meta:
            continue
        assert meta["sheet"] == "Payments"
        for rel, row, _sheet in meta["row_offsets"]:
            seen_rows.append(row)
            if 4 <= row <= 403:
                assert c.text[rel:].startswith(f"{row - 3} | Vendor")
    assert sorted(seen_rows) == sorted(e.row for e in index)  # every row exactly once


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------


def _ref_and_rows(data: bytes):
    red, _ = redact_tableset(parse_workbook(data))
    sheet = manifest(red)["sheets"][0]
    rows = [r for r in row_records(red) if r["sheet_index"] == sheet["sheet_index"]]
    ref = TableRef(ref="1", document_id="doc1", file_name="ledger.xlsx", annexure=3, sheet=sheet)
    return ref, rows


def test_count_sum_average_and_filters_over_every_row():
    ref, rows = _ref_and_rows(_ledger(30))
    res = execute(validate({"table": "1", "operation": "count",
                            "filters": [{"column": "Status", "op": "eq", "value": "pending"}]}, [ref]), rows)
    assert "Matching rows: 10." in res.text
    assert "Total (row 35)" in res.text and "excluded so nothing is counted twice" in res.text

    res = execute(validate({"table": "1", "operation": "sum", "column": "Amount (INR)"}, [ref]), rows)
    expected = sum(Decimal("1000.5") * i for i in range(1, 31))
    assert f"sum of \"Amount (INR)\" = {tables.format_number(expected)}" in res.text
    assert format_indian(expected, 2) in res.text  # both written forms

    res = execute(validate({"table": "1", "operation": "average", "column": "Amount (INR)",
                            "filters": [{"column": "Date", "op": "between",
                                         "value": ["2026-03-01", "2026-03-03"]}]}, [ref]), rows)
    assert "Matching rows: 3." in res.text and "average of \"Amount (INR)\" = 2001" in res.text


def test_group_by_distinct_list_percent_min_max():
    ref, rows = _ref_and_rows(_ledger(9))
    res = execute(validate({"table": "1", "operation": "count", "group_by": "Status"}, [ref]), rows)
    assert "Paid: 6 row(s); Pending: 3 row(s)" in res.text
    res = execute(validate({"table": "1", "operation": "percent",
                            "filters": [{"column": "Status", "op": "eq", "value": "Paid"}]}, [ref]), rows)
    assert "6 of 9 row(s) = 66.67%" in res.text
    res = execute(validate({"table": "1", "operation": "max", "column": "Amount (INR)"}, [ref]), rows)
    assert "maximum of \"Amount (INR)\" = 9004.5 (9,004.50) (row 12)" in res.text
    res = execute(validate({"table": "1", "operation": "list", "columns": ["Payee", "Status"],
                            "filters": [{"column": "Status", "op": "eq", "value": "Pending"}]}, [ref]), rows)
    assert "row 4: Vendor 001 | Pending" in res.text and "row 10: Vendor 007 | Pending" in res.text
    res = execute(validate({"table": "1", "operation": "distinct", "column": "Status"}, [ref]), rows)
    assert "Paid: 6 row(s); Pending: 3 row(s)" in res.text


def test_specs_that_cannot_be_run_are_refused_not_guessed():
    ref, _ = _ref_and_rows(_ledger(3))
    bad = [
        ({"table": "9", "operation": "count"}, "unknown table"),
        ({"table": "1", "operation": "sum", "column": "Amount"}, 'no column "Amount"'),
        ({"table": "1", "operation": "sum", "column": "Mobile"}, "needs a number column"),
        ({"table": "1", "operation": "percent"}, "needs filters"),
        ({"table": "1", "operation": "count",
          "filters": [{"column": "Date", "op": "gt", "value": "next week"}]}, "is not a date"),
        ({"table": "1", "operation": "count",
          "filters": [{"column": "Status", "op": "gt", "value": "Paid"}]}, "needs a number or date column"),
        ({"table": "1", "operation": "drop_table"}, "unknown operation"),
    ]
    for spec, why in bad:
        with pytest.raises(SpecError, match=why):
            validate(spec, [ref])


def test_blank_cells_never_satisfy_a_comparison():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Status", "Amount"])
    ws.append(["a", "Paid", 10])
    ws.append(["b", None, "N/A"])
    ws.append(["c", "Pending", 30])
    ref, rows = _ref_and_rows(_save(wb))
    res = execute(validate({"table": "1", "operation": "count",
                            "filters": [{"column": "Status", "op": "ne", "value": "Paid"}]}, [ref]), rows)
    assert "Matching rows: 1." in res.text  # the blank row is not "not Paid"
    res = execute(validate({"table": "1", "operation": "sum", "column": "Amount"}, [ref]), rows)
    assert "= 40" in res.text and "1 matching row(s) had no usable value" in res.text


def test_needs_calculation_gate():
    ref, _ = _ref_and_rows(_ledger(3))
    assert needs_calculation("How many payments are pending?", [ref])
    assert needs_calculation("What is in the Payee column?", [ref])  # names a column
    assert not needs_calculation("Summarise the legal notice.", [ref])
    assert not needs_calculation("How many payments are pending?", [])


def test_answer_table_questions_end_to_end(monkeypatch):
    from app.vectorstore.qdrant import vector_store

    red, _ = redact_tableset(parse_workbook(_ledger(12)))
    man, records = manifest(red), row_records(red)

    async def manifests(ws):
        return [{"document_id": "doc1", "filename": "ledger.xlsx", "row_count": len(records), "manifest": man}]

    async def get_rows(ws, did, sheet_index):
        return [r for r in records if r["sheet_index"] == sheet_index]

    async def plan(question, previous, refs):
        return [
            {"table": "1", "operation": "count", "filters": [{"column": "Status", "op": "eq", "value": "Pending"}]},
            {"table": "1", "operation": "sum", "column": "Payee"},  # invalid: reported, not run
        ]

    monkeypatch.setattr(vector_store, "get_table_manifests", manifests)
    monkeypatch.setattr(vector_store, "get_table_rows", get_rows)
    monkeypatch.setattr(table_query, "plan_queries", plan)
    answers = asyncio.run(
        table_query.answer_table_questions(
            workspace_id="ws", question="How many are pending?", previous_user_turn="",
            annexure_of={"doc1": 2}, names={"doc1": "ledger.xlsx"},
        )
    )
    assert len(answers.results) == 1 and "Matching rows: 4." in answers.results[0].text
    assert "Annexure 2 (ledger.xlsx)" in answers.results[0].text
    assert len(answers.notes) == 1 and "needs a number column" in answers.notes[0]


# ---------------------------------------------------------------------------
# Citations and attribution
# ---------------------------------------------------------------------------


def _sheet_pack():
    from app.rag.chunker import chunk_text
    from app.rag.evidence import EvidencePack
    from app.vectorstore.qdrant import _chunk_row_metadata

    red, _ = redact_tableset(parse_workbook(_ledger(6)))
    text, index = render_text(red)
    pack = EvidencePack()
    for c in chunk_text(text, with_overlap=False):
        meta = {**c.as_metadata(), **_chunk_row_metadata(c, index), "format": "spreadsheet"}
        pack.add_passage(
            {"content": c.text, "metadata": meta, "original_document_id": "doc1",
             "document_id": f"doc1_chunk_{c.index}"},
            annexure=3, file_name="ledger.xlsx",
        )
    return pack


def test_cell_citation_names_sheet_row_and_header():
    from app.rag.citations import verify_and_number

    pack = _sheet_pack()
    label = next(lbl for lbl, it in pack.items.items() if it.sheet and it.row_offsets)
    text, cites, report = verify_and_number(f"Vendor 004 was paid 4002 on 2026-03-04 [{label}].", pack)
    assert report.clean and cites
    cite = cites[0]
    assert cite["sheet"] == "Payments" and cite["rowStart"] == 7 and cite["rowEnd"] == 7
    assert cite["quote"].startswith("S.No | Payee | Mobile")
    assert "4 | Vendor 004" in cite["quote"]


def test_iso_dates_are_checked_whole_and_numbers_on_boundaries():
    from app.rag.citations import _figures, contains_figure, verify_and_number

    assert "2026-03-04" in _figures("paid on 2026-03-04")
    assert not contains_figure("total 115.5 units", "15.5")
    assert contains_figure("rs.600000/- paid", "600000")
    # Equal by value across trailing zeros, in both directions.
    assert contains_figure("total 10547271 due", "10547271.00")
    assert contains_figure("bone age 15.50 years", "15.5")
    assert contains_figure("paid 500.00 on", "500")
    assert not contains_figure("paid 500.05 on", "500")
    assert not contains_figure("dated 02.09.2026", "02.09.20")
    pack = _sheet_pack()
    label = next(lbl for lbl, it in pack.items.items() if it.sheet and it.row_offsets)
    _, _, report = verify_and_number(f"Vendor 004 was paid on 2026-03-29 [{label}].", pack)
    assert report.unsupported  # a wrong day in the right year is caught


def test_computed_result_is_citable_and_verified():
    from app.rag.citations import verify_and_number

    pack = _sheet_pack()
    t = pack.add_computed(
        "Computed by the system ... Result: sum of \"Amount (INR)\" = 21010.5 (21,010.50).",
        document_id="doc1", annexure=3, file_name="ledger.xlsx", sheet="Payments", row_start=4, row_end=9,
    )
    text, cites, report = verify_and_number(f"The total paid is ₹21,010.50 [{t.label}].", pack)
    assert report.clean
    assert cites[0]["computed"] is True and cites[0]["sheet"] == "Payments"
    _, _, report = verify_and_number(f"The total paid is ₹21,911.50 [{t.label}].", pack)
    assert report.unsupported


def test_chronology_entry_from_a_sheet_gets_a_citation():
    from app.rag.attribution import Attributor, _attribute_dated

    pack = _sheet_pack()
    chunks = [
        {"content": it.text, "original_document_id": "doc1", "document_id": it.chunk_id,
         "metadata": {"sheet": it.sheet, "row_start": it.row_start, "row_end": it.row_end,
                      "row_offsets": it.row_offsets}}
        for it in pack.items.values()
    ]
    attributor = Attributor(chunks, {"doc1": 3}, {"doc1": "ledger.xlsx"})
    entries = [{"date": "2026-03-05", "title": "Payment to Vendor 005", "description": "Paid 5002.5",
                "sourceDocId": "doc1", "pageNumber": 1}]
    assert _attribute_dated(entries, attributor) == 1
    cite = entries[0]["citation"]
    assert cite["sheet"] == "Payments" and cite["rowStart"] == 8


# ---------------------------------------------------------------------------
# Packing, readability, graph
# ---------------------------------------------------------------------------


def test_packing_names_the_rows_it_drops():
    from app.rag.packing import fit_to_budget

    text, _ = render_text(parse_workbook(_ledger(400)))
    packed = fit_to_budget(text, 8000, "general")
    assert packed.startswith("Workbook summary")  # whole-sheet figures always kept
    assert '[… omitted: sheet "Payments" rows' in packed
    assert "workbook summary at the top" in packed


def test_parsed_sheet_is_never_a_low_content_marker():
    from app.processing.readability import readability_scorer

    low = {"total_score": 41.0, "readable": False, "rating": "Poor"}
    out = readability_scorer.apply_lossless_floor(low)
    assert out["readable"] and out["total_score"] == 90.0 and out["details"]["lossless_parse"]


def test_graph_ingest_skips_spreadsheet_chunks():
    from app.ontology.pipeline import chunks_for_extraction

    stored = [
        {"document_id": "a_chunk_0", "original_document_id": "a", "content": "Row text",
         "metadata": {"format": "spreadsheet", "sheet": "Payments"}},
        {"document_id": "b_chunk_0", "original_document_id": "b", "content": "Prose text",
         "metadata": {"format": "pdf"}},
    ]
    assert [c["chunk_id"] for c in chunks_for_extraction(stored)] == ["b_chunk_0"]


def test_numbered_names_are_never_merged():
    from app.ontology.entity_resolution import name_similarity, resolve_entities

    assert name_similarity("Accused No. 1", "Accused No. 10") == 0.0
    assert name_similarity("Respondent No. II", "Respondent No. III") == 0.0
    assert name_similarity("Player 1", "Player 1") == 100.0
    entities = resolve_entities([{"name": f"Player {i}", "label": "Party"} for i in range(1, 41)])
    assert len(entities) == 40
    # Ordinary merges still happen.
    assert len(resolve_entities([{"name": "Meridian Traders Pvt Ltd", "label": "Party"},
                                 {"name": "M/s Meridian Traders", "label": "Party"}])) == 1


# ---------------------------------------------------------------------------
# Row store against a real Qdrant (skipped when none is reachable)
# ---------------------------------------------------------------------------


def _local_qdrant_url():
    url = os.getenv("TEST_QDRANT_URL", "http://localhost:6333")
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=url, timeout=2).get_collections()
        return url
    except Exception:
        return None


@pytest.mark.skipif(_local_qdrant_url() is None, reason="no local Qdrant")
def test_row_store_round_trip_and_both_deletion_paths(monkeypatch):
    from app.core.config import settings
    from app.vectorstore.qdrant import QdrantStore

    prefix = f"pytest_tables_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "qdrant_url", _local_qdrant_url())
    monkeypatch.setattr(settings, "qdrant_api_key", None)
    store = QdrantStore()
    store.collection_name = prefix
    store.chunks_collection = f"{prefix}_evidence"
    store.tables_collection = f"{prefix}_tables"

    async def run():
        await store.initialize()
        red, _ = redact_tableset(parse_workbook(_ledger(300)))
        try:
            for doc in ("d1", "d2"):
                n = await store.replace_document_tables("ws", doc, manifest(red), row_records(red), "l.xlsx")
                assert n == 301  # 300 data rows + the Total row
            # Re-writing a shorter version leaves no stale rows.
            short, _ = redact_tableset(parse_workbook(_ledger(5)))
            await store.replace_document_tables("ws", "d2", manifest(short), row_records(short), "l.xlsx")
            assert len(await store.get_table_rows("ws", "d2", 0)) == 6
            rows = await store.get_table_rows("ws", "d1", 0)
            assert [r["row"] for r in rows][:3] == [4, 5, 6] and len(rows) == 301
            assert {m["document_id"] for m in await store.get_table_manifests("ws")} == {"d1", "d2"}

            await store.delete_documents("ws", ["d1"])  # single-document delete
            assert await store.get_table_rows("ws", "d1", 0) == []
            assert {m["document_id"] for m in await store.get_table_manifests("ws")} == {"d2"}

            await store.delete_workspace("ws")  # compliance purge
            assert await store.get_table_manifests("ws") == []
            assert await store.get_table_rows("ws", "d2", 0) == []
        finally:
            for name in (store.collection_name, store.chunks_collection, store.tables_collection):
                if store.client.collection_exists(name):
                    store.client.delete_collection(name)

    asyncio.run(run())


@pytest.mark.skipif(_local_qdrant_url() is None, reason="no local Qdrant")
def test_ingest_to_cited_chat_answer_end_to_end(monkeypatch):
    """The real pipeline over a real Qdrant, with only Gemini stubbed:
    process_document -> chunks with row locations -> row store ->
    build_chat_evidence -> [T#] calculation -> citation check."""
    import importlib

    # app.processing re-exports a document_processor *instance* under the
    # module's name, so a plain "import ... as" yields the object.
    dp = importlib.import_module("app.processing.document_processor")
    qmod = importlib.import_module("app.vectorstore.qdrant")
    from app.ai.gemini_client import gemini_client
    from app.core.config import settings
    from app.rag.chat_context import build_chat_evidence
    from app.rag.citations import verify_and_number
    from app.rag.evidence import EvidencePack
    from app.vectorstore.qdrant import QdrantStore

    prefix = f"pytest_e2e_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "qdrant_url", _local_qdrant_url())
    monkeypatch.setattr(settings, "qdrant_api_key", None)
    store = QdrantStore()
    store.collection_name = prefix
    store.chunks_collection = f"{prefix}_evidence"
    store.tables_collection = f"{prefix}_tables"
    monkeypatch.setattr(qmod, "vector_store", store)
    monkeypatch.setattr(dp, "vector_store", store)

    async def embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
        return [[0.1] * store.vector_size for _ in texts]

    async def classify(self, text, filename):
        return {"summary": "Payments ledger", "evidentiary_strength": "Medium",
                "document_type": "other", "extracted_fields": {}}

    async def download(self, url, filename):
        return _ledger(250), 4096

    async def plan(question, previous, refs):
        return [{"table": "1", "operation": "sum", "column": "Amount (INR)",
                 "filters": [{"column": "Status", "op": "eq", "value": "Pending"}]}]

    monkeypatch.setattr(gemini_client, "embed_texts", embed_texts)
    monkeypatch.setattr(gemini_client, "is_available", lambda: True)
    monkeypatch.setattr(dp.DocumentProcessor, "_classify_document", classify)
    monkeypatch.setattr(dp.DocumentProcessor, "_download_file", download)
    monkeypatch.setattr(table_query, "plan_queries", plan)
    monkeypatch.setattr("app.ontology.pipeline.schedule_document_ingest", lambda *a, **k: None)

    async def run():
        await store.initialize()
        try:
            result = await dp.DocumentProcessor().process_document(
                file_url="https://example.invalid/x", filename="annexure-3-payments-ledger",
                workspace_id="ws", user_id="u", document_id="doc1", extension="xlsx",
            )
            assert result["success"] and result["readable"]
            assert result["sensitive_fields"]["bank_account_number"]  # raw IDs sealed here only

            chunks = await store.get_document_chunks("ws")
            assert chunks and all(c["metadata"]["overlap_prev"] == 0 for c in chunks)
            assert all("12345678901" not in c["content"] for c in chunks)  # masked at rest
            located = [c for c in chunks if c["metadata"].get("sheet") == "Payments"]
            assert located and all(c["metadata"]["row_offsets"] for c in located)

            pack, mode, _uploaded = await build_chat_evidence(
                workspace_id="ws", question="What is the total amount pending?",
                previous_user_turn="", annexure_of={"doc1": 3}, names={"doc1": "ledger.xlsx"},
                forced_ids=[], max_total_chars=400_000, full_mode_max_docs=15,
            )
            computed = [i for i in pack.items.values() if i.kind == "computed"]
            assert len(computed) == 1 and pack.table_notes[0].startswith(f"[{computed[0].label}]")
            pending = [i for i in range(1, 251) if i % 3 == 1]
            expected = sum(Decimal("1000.5") * i for i in pending)
            figure = tables.format_number(expected)
            assert f"= {figure}" in computed[0].text
            assert f"Matching rows: {len(pending)}." in computed[0].text

            # The answer quoting the computed figure verifies; a wrong one does not.
            restored = EvidencePack.from_state(pack.to_state())
            label = computed[0].label
            _, cites, report = verify_and_number(f"Pending payments total ₹{format_indian(expected, 2)} [{label}].", restored)
            assert report.clean and cites[0]["computed"]
            _, _, report = verify_and_number(f"Pending payments total ₹{figure}1 [{label}].", restored)
            assert report.unsupported

            # Deleting the document leaves nothing computable.
            await store.delete_documents("ws", ["doc1"])
            assert await store.get_table_manifests("ws") == []
        finally:
            for name in (store.collection_name, store.chunks_collection, store.tables_collection):
                if store.client.collection_exists(name):
                    store.client.delete_collection(name)

    asyncio.run(run())
