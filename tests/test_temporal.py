"""Temporal normalisation — day-first, precision-aware, and honest about gaps."""

from datetime import date

import pytest

from app.ontology.temporal import (
    PRECISION_APPROXIMATE,
    PRECISION_DAY,
    PRECISION_MONTH,
    PRECISION_YEAR,
    build_chronology,
    derive_precedes_edges,
    parse_date,
    resolve_relative,
    sort_key,
)


@pytest.mark.parametrize("raw,iso,precision", [
    ("14.03.2025", "2025-03-14", PRECISION_DAY),
    ("14/03/2025", "2025-03-14", PRECISION_DAY),
    ("14-03-2025", "2025-03-14", PRECISION_DAY),
    ("2025-03-14", "2025-03-14", PRECISION_DAY),
    ("14th March 2025", "2025-03-14", PRECISION_DAY),
    ("14 Mar 2025", "2025-03-14", PRECISION_DAY),
    ("March 14, 2025", "2025-03-14", PRECISION_DAY),
    ("March 2025", "2025-03-01", PRECISION_MONTH),
    ("in the year 2019", "2019-01-01", PRECISION_YEAR),
])
def test_parses_the_forms_legal_documents_actually_use(raw, iso, precision):
    parsed = parse_date(raw)
    assert parsed is not None and parsed.iso == iso and parsed.precision == precision


def test_day_first_is_the_default():
    """03/04/2025 is 3 April in Indian practice. Getting this wrong corrupts every chronology."""
    assert parse_date("03/04/2025").iso == "2025-04-03"
    assert parse_date("03/04/2025", day_first=False).iso == "2025-03-04"


def test_impossible_day_first_falls_back_rather_than_dropping():
    """03/14/2025 cannot be day-first; read it month-first rather than lose it."""
    assert parse_date("03/14/2025").iso == "2025-03-14"


def test_hedged_dates_are_marked_approximate():
    parsed = parse_date("on or about 14 March 2025")
    assert parsed.iso == "2025-03-14"
    assert parsed.precision == PRECISION_APPROXIMATE, "the hedge must not be laundered away"


def test_no_date_returns_none_rather_than_guessing():
    assert parse_date("no date at all here") is None
    assert parse_date("") is None


def test_impossible_calendar_date_is_rejected():
    assert parse_date("31.02.2025") is None


def test_two_digit_year_does_not_land_in_the_future():
    assert parse_date("14.03.98").value.year == 1998


def test_precision_defines_the_range_it_covers():
    assert parse_date("March 2025").range_end.isoformat() == "2025-03-31"
    assert parse_date("in the year 2019").range_end.isoformat() == "2019-12-31"
    exact = parse_date("14.03.2025")
    assert exact.range_start == exact.range_end


def test_february_month_range_handles_leap_years():
    assert parse_date("February 2024").range_end.isoformat() == "2024-02-29"
    assert parse_date("February 2025").range_end.isoformat() == "2025-02-28"


# --- relative expressions ---------------------------------------------------


def test_relative_resolves_against_an_anchor():
    parsed = parse_date("within 15 days", anchor=date(2025, 3, 14))
    assert parsed.iso == "2025-03-29" and parsed.is_relative


def test_relative_without_an_anchor_is_unresolved_not_today():
    """Anchoring to 'now' would fabricate a date that changes every pipeline run."""
    assert parse_date("within 15 days") is None
    assert resolve_relative("within 15 days", None) is None


def test_month_relative_is_marked_approximate():
    parsed = resolve_relative("after 3 months", date(2025, 1, 1))
    assert parsed.precision == PRECISION_APPROXIMATE, "30-day months are an approximation"


# --- chronology -------------------------------------------------------------


def test_chronology_orders_and_reports_what_it_could_not_read():
    result = build_chronology([
        {"date": "19.04.2025", "description": "FIR registered"},
        {"date": "14th March 2025", "description": "Cheque returned"},
        {"date": "gibberish", "description": "Unknown"},
    ])
    assert [e["date"] for e in result["events"]] == ["2025-03-14", "2025-04-19"]
    assert len(result["unparsed"]) == 1, "nothing may be silently dropped"
    assert result["unparsed"][0]["description"] == "Unknown"


def test_less_precise_dates_sort_to_the_start_of_their_range():
    result = build_chronology([
        {"date": "14.03.2025", "description": "exact"},
        {"date": "March 2025", "description": "vague"},
    ])
    assert [e["description"] for e in result["events"]] == ["vague", "exact"]


def test_chronology_preserves_extra_fields():
    result = build_chronology([
        {"date": "14.03.2025", "description": "x", "source_document_id": "d1"},
    ])
    assert result["events"][0]["source_document_id"] == "d1"


def test_precedes_edges_only_between_certain_dates():
    """A month-precision date overlapping a day one establishes no ordering."""
    events = build_chronology([
        {"date": "March 2025", "description": "vague"},
        {"date": "14.03.2025", "description": "exact"},
        {"date": "19.04.2025", "description": "later"},
    ])["events"]
    edges = derive_precedes_edges(events)
    assert [(e["from"], e["to"]) for e in edges] == [("exact", "later")]


def test_sort_key_is_range_start_then_precision():
    assert sort_key(parse_date("March 2025")) < sort_key(parse_date("14.03.2025"))
