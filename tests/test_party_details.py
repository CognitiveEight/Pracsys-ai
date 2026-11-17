"""SIM-186: contact and identity details on Relevant Parties.

Masking, verification against the source text, merge precedence and the
shared-extractor prompt. Fictional data only.
"""

import re

import pytest

from app.prompts.extraction_prompts import (
    PARTY_CONTACT_DETAILS_BLOCK,
    extract_parties_subagent_prompt,
)
from app.socket.handlers.party_details import (
    first_mention_page,
    mask_identifier,
    mask_party_payload,
    merge_party_details,
    normalize_identifier_type,
    normalize_identifiers,
    verify_party_details,
)

SOURCE = (
    "Shri Arun Mehta, residing at 12 Lake View Road, Kothrud, Pune 411001, "
    "email arun.mehta@example.com, mobile +91 98765 43210. "
    "Aadhaar XXXX-XXXX-9012, PAN ABCXX1234X, GSTIN 27ABCDE1234F1Z5."
)


def _visible_digits(value: str) -> int:
    return sum(ch.isdigit() for ch in value)


# ------------------------------------------------------------------ masking

@pytest.mark.parametrize(
    "id_type, raw, expected",
    [
        ("aadhaar", "1234 5678 9012", "XXXX-XXXX-9012"),
        ("pan", "ABCDE1234F", "ABCXX1234X"),
        ("passport", "K1234567", "KXXX4567"),
        ("voter_id", "ABC1234567", "ABCXXX4567"),
        ("bank_account", "123456789012", "XXXXXXXX9012"),
        # The ingestion masker leaves the district digits visible (6 in all);
        # the panel's 4-digit rule masks them too.
        ("udyam", "UDYAM-MH-01-1234567", "XXXXXXXXXXXX4567"),
        ("gstin", "27ABCDE1234F1Z5", "XXXXXXXXXXXF1Z5"),
        ("other", "AB12345678", "XXXXXX5678"),
    ],
)
def test_mask_identifier_by_type(id_type, raw, expected):
    assert mask_identifier(id_type, raw) == expected


@pytest.mark.parametrize(
    "id_type, raw",
    [
        ("aadhaar", "1234 5678 9012"),
        ("pan", "ABCDE1234F"),
        ("passport", "K1234567"),
        ("voter_id", "ABC1234567"),
        ("driving_licence", "MH12 2015 1234567"),
        ("bank_account", "123456789012"),
        ("udyam", "UDYAM-MH-01-1234567"),
        ("gstin", "27ABCDE1234F1Z5"),
        ("cin", "U12345MH2010PTC123456"),
    ],
)
def test_mask_identifier_is_idempotent(id_type, raw):
    once = mask_identifier(id_type, raw)
    assert mask_identifier(id_type, once) == once


@pytest.mark.parametrize(
    "id_type, raw",
    [
        ("pan", "1234567890"),            # wrong shape for its type
        ("aadhaar", "123456789012345"),
        ("passport", "12345678"),
        ("other", "9876543210123"),
        ("unknown-kind", "5555 6666 7777 8888"),
    ],
)
def test_mask_identifier_never_shows_more_than_four_digits(id_type, raw):
    assert _visible_digits(mask_identifier(id_type, raw)) <= 4


def test_identifier_type_aliases():
    assert normalize_identifier_type("Aadhar") == "aadhaar"
    assert normalize_identifier_type("PAN Card") == "pan"
    assert normalize_identifier_type("EPIC") == "voter_id"
    assert normalize_identifier_type("driving license") == "driving_licence"
    assert normalize_identifier_type("something else") == "other"


def test_normalize_identifiers_masks_labels_and_dedups():
    ids = normalize_identifiers(
        [
            {"type": "aadhaar", "value": "1234 5678 9012"},
            {"type": "aadhaar", "value": "XXXX-XXXX-9012"},  # same number
            {"type": "pan", "value": ""},
            "not a dict",
        ],
        source_doc_id="doc_1",
    )
    assert ids == [
        {"type": "aadhaar", "label": "Aadhaar", "value": "XXXX-XXXX-9012", "sourceDocId": "doc_1"}
    ]


def test_mask_party_payload_masks_in_place():
    payload = {
        "parties": [
            {"name": "A", "identifiers": [{"type": "gstin", "value": "27ABCDE1234F1Z5"}]},
            {"name": "B"},
        ]
    }
    mask_party_payload(payload)
    assert payload["parties"][0]["identifiers"][0]["value"] == "XXXXXXXXXXXF1Z5"
    assert payload["parties"][1]["identifiers"] == []


def test_mask_party_payload_leaves_out_unknown_fields():
    # Backend contract (SIM-184): leave the key out when unknown, never "".
    party = {
        "name": "A", "address": "", "email": "  ", "contactNumber": "9876543210",
        "sourceDocId": "", "sourceDocName": "", "pageNumber": 0,
    }
    mask_party_payload({"parties": [party]})
    for field in ("address", "email", "sourceDocId", "sourceDocName", "pageNumber"):
        assert field not in party, field
    assert party["contactNumber"] == "9876543210"
    kept = {"name": "B", "pageNumber": 3}
    mask_party_payload({"parties": [kept]})
    assert kept["pageNumber"] == 3


def test_first_mention_page():
    text = "--- Page 1 ---\nintro\n--- Page 2 ---\nShri Arun Mehta said\n--- Page 3 ---\nArun Mehta"
    assert first_mention_page(text, ["Shri Arun Mehta", "Arun Mehta"]) == 2
    assert first_mention_page(text, ["Nobody Here"]) is None
    assert first_mention_page("no page markers, Arun Mehta", ["Arun Mehta"]) is None


# ------------------------------------------------------------------ verification

def test_verify_keeps_present_values_and_drops_invented_ones():
    raw = {
        "address": "12 Lake View Road, Kothrud, Pune 411001",
        "emails": ["arun.mehta@example.com", "invented@nowhere.in"],
        "phones": ["9876543210", "9999999999"],
        "identifiers": [
            {"type": "aadhaar", "value": "XXXX-XXXX-9012"},
            {"type": "pan", "value": "ABCXX9999X"},
            {"type": "gstin", "value": "27ABCDE1234F1Z5"},
        ],
    }
    details, dropped = verify_party_details(raw, SOURCE, source_doc_id="doc_1")
    assert details["address"] == "12 Lake View Road, Kothrud, Pune 411001"
    assert details["email"] == "arun.mehta@example.com"
    assert details["contactNumber"] == "9876543210"
    assert [i["value"] for i in details["identifiers"]] == ["XXXX-XXXX-9012", "XXXXXXXXXXXF1Z5"]
    assert all(i["sourceDocId"] == "doc_1" for i in details["identifiers"])
    assert dropped == 3


def test_verify_drops_address_with_wrong_pin_or_mostly_invented():
    wrong_pin, _ = verify_party_details(
        {"address": "12 Lake View Road, Kothrud, Pune 411999"}, SOURCE
    )
    invented, _ = verify_party_details(
        {"address": "45 Hill Crest Avenue, Baner, Nashik"}, SOURCE
    )
    too_short, _ = verify_party_details({"address": "Pune"}, SOURCE)
    assert wrong_pin["address"] == ""
    assert invented["address"] == ""
    assert too_short["address"] == ""


def test_verify_phone_inside_a_merged_run():
    text = "Contact: 98765 43210 91234 56789 for both offices."
    details, dropped = verify_party_details({"phones": ["98765 43210", "9123456789"]}, text)
    assert details["contactNumber"] == "98765 43210, 9123456789"
    assert dropped == 0


def test_verify_accepts_single_string_fields():
    details, _ = verify_party_details(
        {"email": "arun.mehta@example.com", "contactNumber": "+91 98765 43210"}, SOURCE
    )
    assert details["email"] == "arun.mehta@example.com"
    assert details["contactNumber"] == "+91 98765 43210"


# ------------------------------------------------------------------ merge

def test_merge_primary_wins_and_secondary_fills_blanks():
    primary = {
        "address": "",
        "email": "typed.by.lawyer@example.com",
        "contactNumber": "",
        "identifiers": [{"type": "pan", "value": "ABCXX1234X"}],
    }
    secondary = {
        "address": "12 Lake View Road, Pune 411001",
        "email": "arun.mehta@example.com",
        "contactNumber": "9876543210",
        "identifiers": [
            {"type": "pan", "value": "ABCDE1234F"},  # same PAN, unmasked
            {"type": "aadhaar", "value": "XXXX-XXXX-9012"},
        ],
    }
    merged = merge_party_details(primary, secondary)
    assert merged["email"] == "typed.by.lawyer@example.com"
    assert merged["address"] == "12 Lake View Road, Pune 411001"
    assert merged["contactNumber"] == "9876543210"
    assert [i["value"] for i in merged["identifiers"]] == ["ABCXX1234X", "XXXX-XXXX-9012"]


# ------------------------------------------------------------------ shared extractor

def test_shared_extractor_prompt_unchanged():
    plain = extract_parties_subagent_prompt(combined_document_content="DOC")
    flagged = extract_parties_subagent_prompt(
        combined_document_content="DOC", with_contact_details=True
    )
    assert "CONTACT AND IDENTITY DETAILS" not in plain
    assert plain.endswith("DOCUMENTS:\nDOC")
    # The flag adds exactly the block, before the documents, and nothing else.
    assert flagged == plain.replace(
        "DOCUMENTS:\nDOC", PARTY_CONTACT_DETAILS_BLOCK + "\nDOCUMENTS:\nDOC"
    )
    assert re.search(r"Annexure N", plain)
