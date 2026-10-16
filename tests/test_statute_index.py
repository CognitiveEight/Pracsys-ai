"""check_provision against the India Code section index (app/statutes/data/sections.json)."""

import json

import pytest

from app.statutes.register import (
    check_provision,
    get_section_index,
    section_key,
)


@pytest.fixture(scope="module")
def index():
    idx = get_section_index()
    assert idx is not None, "app/statutes/data/sections.json must ship with the code"
    return idx


def test_index_is_complete_for_the_core_criminal_codes(index):
    assert len(index.acts["BNS"]["sections"]) == 358
    assert len(index.acts["BNSS"]["sections"]) == 531
    assert len(index.acts["BSA"]["sections"]) == 170


def test_index_holds_no_ipc(index):
    names = [a["name"] for a in index.acts.values()]
    assert not [n for n in names if "penal code" in n.lower()]


@pytest.mark.parametrize("raw, key", [
    ("138", "138"), ("Section 138(1)", "138"), ("66-C", "66C"), ("sec. 66c", "66C"),
    ("45-IA", "45IA"), ("378Z-I", "378ZI"), ("", ""), ("Schedule", ""),
])
def test_section_key(raw, key):
    assert section_key(raw) == key


def test_bns_headings_are_the_bns_ones_not_ipc():
    assert check_provision("Bharatiya Nyaya Sanhita, 2023", "303")["heading"] == "Theft"
    assert check_provision("BNS", "318")["registerStatus"] == "verified"


def test_ipc_numbering_under_bns_is_invalid():
    for section in ("354A", "376D", "999"):
        assert check_provision("BNS", section)["registerStatus"] == "invalid"


def test_legacy_act_stays_unverified():
    result = check_provision("Indian Penal Code", "420")
    assert result["registerStatus"] == "unverified" and result["statute"] == "IPC"


def test_full_name_and_abbreviation_both_resolve():
    for act in ("Negotiable Instruments Act, 1881", "NI Act", "The Negotiable Instruments Act"):
        assert check_provision(act, "138")["registerStatus"] == "verified", act


def test_order_and_rule_are_never_read_as_sections():
    assert check_provision("Code of Civil Procedure, 1908", "Order 39 Rule 1")["registerStatus"] == "unverified"
    assert check_provision("Code of Civil Procedure, 1908", "Section 9")["registerStatus"] == "verified"


def test_repealed_act_is_flagged_but_not_invalid():
    result = check_provision("Income-tax Act, 1961", "10")
    assert result["registerStatus"] == "unverified"
    assert result["repealed"] is True and "2025" in result["repealedBy"]


def test_ambiguous_name_without_year_is_not_guessed():
    # Merchant Shipping Act 1958 (repealed) and 2025 both exist.
    assert check_provision("Merchant Shipping Act", "3") == {"registerStatus": "unverified"}
    assert check_provision("Merchant Shipping Act, 2025", "3")["registerStatus"] == "verified"


def test_omitted_section_is_invalid_with_flag():
    result = check_provision("Patents Act, 1970", "95")
    assert result["registerStatus"] == "invalid" and result.get("omitted") is True


def test_unknown_act_and_missing_number_are_unverified():
    assert check_provision("Some Unknown Act", "5") == {"registerStatus": "unverified"}
    assert check_provision("BNS", "")["registerStatus"] == "unverified"


def test_missing_index_falls_back_to_seed(tmp_path, monkeypatch):
    import app.statutes.register as reg

    monkeypatch.setattr(reg, "DEFAULT_SECTION_INDEX_PATH", tmp_path / "absent.json")
    reg.get_section_index.cache_clear()
    try:
        assert reg.get_section_index() is None
        # Seed still answers for its own acts and never raises.
        assert reg.check_provision("BNS", "999")["registerStatus"] == "invalid"
        assert reg.check_provision("NI Act", "138") == {"registerStatus": "unverified"}
    finally:
        reg.get_section_index.cache_clear()


def test_index_file_is_compact_json():
    from app.statutes.register import DEFAULT_SECTION_INDEX_PATH

    raw = json.loads(DEFAULT_SECTION_INDEX_PATH.read_text(encoding="utf-8"))
    assert set(raw) >= {"index_version", "acts", "repealed"}
    assert DEFAULT_SECTION_INDEX_PATH.stat().st_size < 2_000_000


def test_law_rows_get_the_official_section_title():
    from app.statutes.register import annotate_law_rows

    payload = {"laws": [
        {"act": "Copyright Act, 1957", "section": "Section 19", "description": "assignment"},
        {"act": "Bharatiya Nyaya Sanhita, 2023", "section": "Section 318", "description": "cheating"},
        {"act": "Some Unknown Act", "section": "Section 4", "description": "x"},
    ]}
    annotate_law_rows(payload)
    rows = payload["laws"]
    assert rows[0]["sectionTitle"] == "Mode of assignment" and rows[0]["section"] == "Section 19"
    assert rows[1]["sectionTitle"] == "Cheating"
    assert "sectionTitle" not in rows[2] and rows[2]["registerStatus"] == "unverified"


def test_edited_section_gets_a_fresh_title():
    from app.statutes.register import annotate_law_rows

    payload = {"laws": [{"act": "Copyright Act, 1957", "section": "Section 19"}]}
    annotate_law_rows(payload)
    payload["laws"][0]["section"] = "Section 14"
    annotate_law_rows(payload)
    assert payload["laws"][0]["sectionTitle"] == "Meaning of copyright"
