"""Upload wording (Annexure N) and dates taken from file names."""

import pytest

from app.socket.event_utils import _annexure_wording_for_next_steps, _upload_wording
from app.utils.document_dates import date_from_document_name


@pytest.mark.parametrize("text, expected", [
    ("Document 2 records the notice.", "Annexure 2 records the notice."),
    ("Documents 3 and 4 show receipts.", "Annexures 3 and 4 show receipts."),
    ("The document states the cheque bounced.", "The annexure states the cheque bounced."),
    ("Collect the uploaded documents.", "Collect the uploaded annexures."),
    # Not uploads — must be left alone (the blanket replace corrupted these):
    ("Annexure 1 documents an official representation.", "Annexure 1 documents an official representation."),
    ("overrule identity documents including passports.", "overrule identity documents including passports."),
    ("the document dated 05/03/2026 was served.", "the document dated 05/03/2026 was served."),
])
def test_upload_wording(text, expected):
    assert _upload_wording(text) == expected


def test_next_steps_wrapper_uses_the_same_rule():
    assert _annexure_wording_for_next_steps("  Rely on Document 1 and identity documents. ") == \
        "Rely on Annexure 1 and identity documents."


@pytest.mark.parametrize("name, expected", [
    ("annexure-2-25_11_29_Letter_KCA.docx.pdf", ("2025-11-29", True)),   # stored upload name
    ("25 11 29 Letter KCA.docx.pdf", ("2025-11-29", True)),
    ("26 03 13 Legal Notice.pdf", ("2026-03-13", True)),
    ("2026-03-13 Order.pdf", ("2026-03-13", True)),
    ("260313 scan.pdf", ("2026-03-13", True)),
    ("annexure-1-30_Nov_BCCI_Letter.docx.pdf", ("2025-11-30", False)),   # no year: latest past
    ("Annexure-30_Nov_BCCI_Letter.docx.pdf", ("2025-11-30", False)),     # prefix word only: 30 is the day
    ("Annexure-25_11_29_Letter_KCA.docx.pdf", ("2025-11-29", True)),
    ("5 September 2025 notice.pdf", ("2025-09-05", True)),
    ("complaint.pdf", None),
    ("12 Novel draft.pdf", None),                                        # not a month
    ("30 Feb.pdf", None),                                                # not a date
])
def test_date_from_document_name(name, expected):
    assert date_from_document_name(name, "2026-10-01") == expected


def test_year_less_name_never_resolves_to_the_future():
    date_iso, has_year = date_from_document_name("31 Dec letter.pdf", "2026-10-01")
    assert date_iso == "2025-12-31" and has_year is False
