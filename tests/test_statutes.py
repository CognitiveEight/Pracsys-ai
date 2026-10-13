"""Statutory register and resolver — Section 4.3's three rules."""

import json

import pytest

from app.ontology.neo4j_ddl import generate_schema_statements, statement_counts
from app.statutes.register import (
    RegisterError,
    get_register,
    load_register,
    normalize_section_number,
)
from app.statutes.resolver import (
    Resolution,
    resolution_report,
    resolve_many,
    resolve_provision,
    unresolvable_rate,
)


# --------------------------------------------------------------------------
# Register
# --------------------------------------------------------------------------


def test_shipped_register_loads_and_self_validates():
    """Loading runs the internal consistency checks, including legacy targets."""
    register = get_register()
    assert register.statutes.keys() == {"BNS", "BNSS", "BSA", "IT_ACT"}
    assert register.legacy.keys() == {"IPC", "CRPC", "IEA"}


def test_register_is_honestly_marked_incomplete():
    """A partial register must never present as authoritative."""
    register = get_register()
    assert register.status == "seed"
    assert not register.is_complete
    assert register.coverage == "partial"


def test_every_legacy_mapping_targets_a_registered_section():
    """The check that catches an error in the mapping table itself."""
    register = get_register()
    for legacy in register.legacy.values():
        successor = register.statutes[legacy.succeeded_by]
        for old, new in legacy.section_map.items():
            assert successor.has_section(new), f"{legacy.key} {old} -> {new} missing"


def test_malformed_register_fails_closed(tmp_path):
    """An empty register would mark every real citation unverified — so it raises."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "register_version": "x", "status": "seed",
        "statutes": {"BNS": {"canonical_name": "BNS", "sections": {}}},
        "legacy_statutes": {"IPC": {"canonical_name": "IPC", "succeeded_by": "BNS",
                                    "section_map": {"302": "103"}}},
    }), encoding="utf-8")
    with pytest.raises(RegisterError, match="not in the register"):
        load_register(bad)


def test_missing_register_raises_rather_than_returning_empty(tmp_path):
    with pytest.raises(RegisterError, match="not found"):
        load_register(tmp_path / "nope.json")


def test_duplicate_alias_is_rejected(tmp_path):
    bad = tmp_path / "dupe.json"
    bad.write_text(json.dumps({
        "register_version": "x", "status": "seed",
        "statutes": {
            "A": {"canonical_name": "Act A", "aliases": ["shared"], "sections": {}},
            "B": {"canonical_name": "Act B", "aliases": ["shared"], "sections": {}},
        },
        "legacy_statutes": {},
    }), encoding="utf-8")
    with pytest.raises(RegisterError, match="claimed by"):
        load_register(bad)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Section 302", "302"), ("sec. 66c", "66C"), ("S. 41A", "41A"),
        ("66-C", "66C"), ("  Sections 173 ", "173"), ("316(4)", "316(4)"),
        ("", ""), ("Sec 498a", "498A"),
    ],
)
def test_section_normalisation(raw, expected):
    assert normalize_section_number(raw) == expected


def test_act_alias_lookup_handles_surface_forms():
    register = get_register()
    for surface in ["IPC", "i.p.c.", "Indian Penal Code, 1860", "the indian penal code"]:
        assert register.lookup_act(surface) == ("IPC", True), surface
    for surface in ["BNS", "Bharatiya Nyaya Sanhita, 2023", "bns 2023"]:
        assert register.lookup_act(surface) == ("BNS", False), surface
    assert register.lookup_act("Companies Act 2013") == (None, False)


def test_longest_alias_wins_on_containment():
    """'indian evidence act' must not lose to a shorter contained alias."""
    register = get_register()
    assert register.lookup_act("under the Indian Evidence Act, 1872") == ("IEA", True)


# --------------------------------------------------------------------------
# Resolver — Section 4.3 rules 1, 2 and 3
# --------------------------------------------------------------------------


def test_rule_1_known_section_resolves():
    result = resolve_provision("BNS", "103")
    assert result.resolution is Resolution.RESOLVED
    assert result.citation == "BNS 103"
    assert result.heading == "Punishment for murder"
    assert result.emittable


def test_rule_2_legacy_citation_carries_forward():
    """IPC 420 becomes BNS 318 — the substance survives, the IPC label does not."""
    result = resolve_provision("IPC", "420")
    assert result.resolution is Resolution.MAPPED
    assert result.citation == "BNS 318"
    assert result.original_act == "IPC"
    assert result.original_section == "420"
    assert result.is_legacy_source
    assert result.emittable


@pytest.mark.parametrize(
    "act,section,expected_citation",
    [("IPC", "302", "BNS 103"), ("CrPC", "154", "BNSS 173"),
     ("Indian Evidence Act", "65B", "BSA 63"), ("IPC", "498A", "BNS 85")],
)
def test_legacy_mappings_across_all_three_frameworks(act, section, expected_citation):
    assert resolve_provision(act, section).citation == expected_citation


def test_rule_3_unknown_section_is_unverified_not_invalid():
    """The register is partial: absent means unknown, never 'does not exist'."""
    result = resolve_provision("BNSS", "41")
    assert result.resolution is Resolution.UNVERIFIED
    assert not result.emittable
    assert "unknown, not invalid" in result.note


def test_unknown_act_is_unverified():
    result = resolve_provision("Companies Act 2013", "138")
    assert result.resolution is Resolution.UNVERIFIED
    assert not result.emittable


def test_section_past_the_acts_length_is_invalid():
    """This one IS a claim of impossibility — BNS has 358 sections."""
    result = resolve_provision("BNS", "9999")
    assert result.resolution is Resolution.INVALID
    assert not result.emittable


def test_malformed_section_is_invalid():
    assert resolve_provision("BNS", "abc").resolution is Resolution.INVALID
    assert resolve_provision("BNS", "").resolution is Resolution.INVALID


def test_unmapped_legacy_section_is_not_emittable():
    """IPC 511 has no seed mapping: it must not be emitted as a current provision."""
    result = resolve_provision("IPC", "511")
    assert result.resolution is Resolution.UNMAPPED_LEGACY
    assert not result.emittable
    assert result.citation is None


def test_no_ipc_ever_appears_in_an_emittable_result():
    """The standing product rule: IPC must not reach applicable_laws output."""
    citations = [{"act": "IPC", "section": s} for s in
                 ["302", "420", "376", "511", "34", "120B", "9999"]]
    for resolved in resolve_many(citations):
        if resolved.emittable:
            assert resolved.act_key in {"BNS", "BNSS", "BSA", "IT_ACT"}
            assert "penal code" not in (resolved.act_name or "").lower()


def test_resolution_report_and_metric_10_1():
    resolutions = resolve_many([
        {"act": "BNS", "section": "103"},
        {"act": "IPC", "section": "420"},
        {"act": "BNSS", "section": "41"},
    ])
    report = resolution_report(resolutions)
    assert report["total"] == 3
    assert report["emittable"] == 2
    assert report["unresolvableRate"] == pytest.approx(1 / 3, abs=1e-4)
    assert unresolvable_rate([]) == 0.0


def test_resolver_never_raises_on_junk():
    for act, section in [("", ""), (None, None), ("BNS", None), ("!!", "??")]:
        assert resolve_provision(act, section) is not None


# --------------------------------------------------------------------------
# Generated Neo4j DDL
# --------------------------------------------------------------------------


def test_ddl_generates_for_both_editions():
    for edition in ("enterprise", "community"):
        statements = generate_schema_statements(edition)
        assert any(s.startswith("CREATE CONSTRAINT") for s in statements)
        assert any(s.startswith("CREATE INDEX") for s in statements)
        assert all(s.endswith(";") for s in statements if s.startswith("CREATE"))


def test_unknown_edition_is_rejected():
    with pytest.raises(ValueError, match="Unknown edition"):
        generate_schema_statements("aura")


def test_every_node_type_gets_a_workspace_index():
    """Matter scoping is the first predicate of essentially every query."""
    statements = "\n".join(generate_schema_statements("enterprise"))
    from app.ontology.schema import NODE_TYPES

    for spec in NODE_TYPES:
        assert f"FOR (n:{spec.label}) ON (n.workspace_id);" in statements, spec.label


def test_community_edition_reports_what_it_cannot_enforce():
    statements = generate_schema_statements("community")
    assert any("NOT ENFORCED BY THIS EDITION" in s for s in statements)
    assert statement_counts("community")["constraints"] < statement_counts("enterprise")["constraints"]


def test_property_anchored_edges_get_no_relationship_ddl():
    statements = "\n".join(generate_schema_statements("enterprise"))
    assert ":OCCURRED_ON]" not in statements
