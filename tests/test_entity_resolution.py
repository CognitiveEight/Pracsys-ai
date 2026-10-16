"""Entity resolution — merges are the dangerous error, so they must be earned."""

import pytest

from app.ontology.entity_resolution import (
    canonical_index,
    canonicalise,
    initials_compatible,
    is_initialised_form,
    is_role_referent,
    name_similarity,
    normalize_name,
    resolve_entities,
    resolve_parties,
)


@pytest.mark.parametrize("raw,expected", [
    ("M/s Meridian Traders Private Limited.", "meridian traders"),
    ("Bharat Steel and Sons", "bharat steel"),
    ("MERIDIAN TRADERS PVT LTD", "meridian traders"),
    ("Meridian Traders & Co", "meridian traders"),
    ("Shri Ravindran Nair", "ravindran nair"),
    ("The Bharat Steel Corporation", "bharat steel"),
    ("", ""),
])
def test_normalisation_strips_noise_not_identity(raw, expected):
    assert normalize_name(raw) == expected


def test_similarity_is_order_insensitive():
    assert name_similarity("Nair Ravindran", "Ravindran Nair") == 100.0


def test_corporate_spellings_collapse_to_one_entity():
    entities = resolve_entities([
        {"name": "M/s Meridian Traders"},
        {"name": "Meridian Traders Private Limited"},
        {"name": "MERIDIAN TRADERS PVT LTD"},
    ])
    assert len(entities) == 1
    assert entities[0].canonical_name == "Meridian Traders Private Limited"
    assert entities[0].mentions == 3


def test_canonical_is_the_fullest_form():
    entities = resolve_entities([
        {"name": "Meridian Traders"}, {"name": "Meridian Traders Private Limited"},
    ])
    assert len(entities) == 1
    assert entities[0].canonical_name == "Meridian Traders Private Limited"


def test_single_token_subset_does_not_absorb():
    """A bare surname must not swallow everyone who shares it."""
    assert len(resolve_entities([{"name": "Nair"}, {"name": "Ravindran Nair"}])) == 2
    assert len(resolve_entities([{"name": "Meridian"}, {"name": "Meridian Traders Ltd"}])) == 2


def test_multi_token_subset_does_merge_and_is_audited():
    """A dropped corporate suffix is a real match — but still a recorded inference."""
    result = resolve_parties([
        {"name": "Meridian Traders"}, {"name": "Meridian Traders Kochi"},
    ])
    assert len(result["entities"]) == 1
    assert result["mergeCount"] == 1, "a subset match is an inference, not an exact repeat"
    assert result["mergesForReview"][0]["score"] == 100.0


def test_distinct_parties_are_not_merged():
    entities = resolve_entities([{"name": "Bharat Steel Ltd"}, {"name": "Bharat Cement Ltd"}])
    assert len(entities) == 2


def test_role_referent_never_creates_a_merge():
    """'the accused' in two documents may be two different people."""
    entities = resolve_entities([
        {"name": "Ravindran Nair", "source_document_id": "d1"},
        {"name": "the accused", "source_document_id": "d2"},
    ])
    assert len(entities) == 2


@pytest.mark.parametrize("name", ["the accused", "The Complainant", "petitioner"])
def test_role_referents_are_recognised(name):
    assert is_role_referent(name)


def test_role_referent_attaches_on_exact_repeat():
    entities = resolve_entities([
        {"name": "the accused", "source_document_id": "d1"},
        {"name": "the accused", "source_document_id": "d2"},
    ])
    assert len(entities) == 1 and entities[0].mentions == 2


# --- initialised names ------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("R. Nair", True), ("K. S. Iyer", True), ("Ravindran Nair", False), ("Nair", False),
])
def test_initialised_form_detection(name, expected):
    assert is_initialised_form(name) is expected


@pytest.mark.parametrize("full,abbrev,expected", [
    ("Ravindran Nair", "R. Nair", True),
    ("Rajesh Nair", "R. Nair", True),
    ("Ravindran Nair", "S. Nair", False),
    ("Ravindran Nair", "R. Menon", False),
    ("Ravindran Kumar Nair", "R. K. Nair", True),
])
def test_initials_compatibility(full, abbrev, expected):
    assert initials_compatible(full, abbrev) is expected


def test_unambiguous_initial_merges():
    entities = resolve_entities([{"name": "Ravindran Nair"}, {"name": "R. Nair"}])
    assert len(entities) == 1
    assert entities[0].canonical_name == "Ravindran Nair"
    assert "R. Nair" in entities[0].aliases


def test_ambiguous_initial_is_flagged_not_guessed():
    """The dangerous case: 'R. Nair' fits both Ravindran and Rajesh."""
    result = resolve_parties([
        {"name": "Ravindran Nair"}, {"name": "Rajesh Nair"}, {"name": "R. Nair"},
    ])
    assert len(result["entities"]) == 3, "must not merge into either"
    flagged = result["ambiguousAbbreviations"]
    assert len(flagged) == 1
    assert flagged[0]["name"] == "R. Nair"
    assert set(flagged[0]["couldBe"]) == {"Ravindran Nair", "Rajesh Nair"}


def test_similarity_merges_are_reported_with_their_score():
    """An SME must be able to audit every merge that was not an exact match."""
    result = resolve_parties([
        {"name": "Bharat Steel Corporation Limited"},
        {"name": "Bharat Steel Corporaton Limited"},  # typo in the source document
    ])
    assert result["mergeCount"] == 1
    assert result["mergesForReview"][0]["score"] >= 92.0


def test_threshold_controls_merge_aggression():
    mentions = [{"name": "Bharat Steel Ltd"}, {"name": "Bharat Steels Ltd"}]
    assert len(resolve_entities(mentions, threshold=50.0)) == 1
    assert len(resolve_entities(mentions, threshold=99.9)) == 2


def test_canonical_index_maps_every_surface_form():
    entities = resolve_entities([
        {"name": "M/s Meridian Traders"}, {"name": "Meridian Traders Private Limited"},
    ])
    index = canonical_index(entities)
    assert canonicalise("MERIDIAN TRADERS PVT LTD", index) == "Meridian Traders Private Limited"
    assert canonicalise("Totally Unknown Ltd", index) == "Totally Unknown Ltd"


def test_source_documents_are_tracked():
    entities = resolve_entities([
        {"name": "Meridian Traders", "source_document_id": "d1"},
        {"name": "M/s Meridian Traders", "source_document_id": "d2"},
    ])
    assert entities[0].to_dict()["source_document_ids"] == ["d1", "d2"]


def test_empty_and_blank_mentions_are_ignored():
    assert resolve_entities([{"name": ""}, {"name": "   "}, {}]) == []


# --- cross-ingest reconciliation --------------------------------------------
#
# The failure that only appears across separate ingests: resolution inside one
# extraction pass cannot see the entity created by last week's upload.


def test_a_new_spelling_resolves_onto_an_existing_entity():
    from app.ontology.entity_resolution import reconcile_with_existing

    mapping = reconcile_with_existing(
        ["M/s Meridian Traders"], ["Meridian Traders Private Limited"]
    )
    assert mapping == {"M/s Meridian Traders": "Meridian Traders Private Limited"}


def test_an_existing_name_is_never_recanonicalised():
    """Renaming an entity would orphan every fact already attached to it."""
    from app.ontology.entity_resolution import reconcile_with_existing

    mapping = reconcile_with_existing(
        ["Meridian Traders Private Limited"], ["M/s Meridian Traders"]
    )
    assert mapping == {"Meridian Traders Private Limited": "M/s Meridian Traders"}


def test_an_unrelated_new_entity_is_left_alone():
    from app.ontology.entity_resolution import reconcile_with_existing

    assert reconcile_with_existing(["Bharat Steel Ltd"], ["Meridian Traders Ltd"]) == {}


def test_an_initial_resolves_onto_an_existing_full_name():
    from app.ontology.entity_resolution import reconcile_with_existing

    assert reconcile_with_existing(["R. Nair"], ["Ravindran Nair"]) == {
        "R. Nair": "Ravindran Nair"
    }


def test_an_ambiguous_initial_is_refused_across_ingests_too():
    from app.ontology.entity_resolution import reconcile_with_existing

    assert reconcile_with_existing(["R. Nair"], ["Ravindran Nair", "Rajesh Nair"]) == {}


def test_an_equally_good_match_against_two_entities_is_refused():
    from app.ontology.entity_resolution import reconcile_with_existing

    mapping = reconcile_with_existing(["Acme"], ["Acme Alpha Ltd", "Acme Beta Ltd"])
    assert mapping == {}, "guessing between two equal matches would be a merge error"


def test_a_role_referent_never_reconciles_onto_a_named_party():
    from app.ontology.entity_resolution import reconcile_with_existing

    assert reconcile_with_existing(["the accused"], ["Ravindran Nair"]) == {}


def test_an_empty_graph_reconciles_nothing():
    from app.ontology.entity_resolution import reconcile_with_existing

    assert reconcile_with_existing(["Anyone At All"], []) == {}
