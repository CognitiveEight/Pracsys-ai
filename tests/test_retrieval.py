"""The agentic retrieval loop: plan → route → assemble → critique → verify."""

import pytest

from app.retrieval.assembler import Evidence, assemble, from_chunk, from_graph_row, render_graph_fact
from app.retrieval.critic import critique
from app.retrieval.graph_queries import (
    EVIDENCE_QUERIES,
    GRAPH_QUERIES,
    LOOKUP_QUERIES,
    build,
    withdraw_document_facts,
)
from app.retrieval.planner import RetrievalPlan, SubQuestion, plan, replan, route
from app.retrieval.profiles import (
    CUTOVER_ORDER,
    MODALITY_GRAPH,
    MODALITY_LEXICAL,
    MODALITY_VECTOR,
    PANEL_PROFILES,
    get_profile,
)
from app.retrieval.verifier import numbers_are_consistent, overlap_ratio, verify_claims


# --- profiles ---------------------------------------------------------------


def test_every_panel_in_the_cutover_order_has_a_profile():
    assert len(PANEL_PROFILES) == 8
    assert CUTOVER_ORDER[0] == "list_of_dates", "most structural first"
    assert CUTOVER_ORDER[-1] == "intelligence_chat", "per-turn planning last"
    assert CUTOVER_ORDER[-2] == "strategy_objective", "most synthetic last"


def test_unknown_panel_is_refused_not_defaulted():
    with pytest.raises(KeyError, match="No retrieval profile"):
        get_profile("made_up_panel")


def test_every_profile_graph_query_exists_and_is_evidence():
    """A profile must name an implemented query, and one that can be cited."""
    for profile in PANEL_PROFILES:
        for question in profile.sub_questions:
            if question.graph_query:
                assert question.graph_query in GRAPH_QUERIES, question.graph_query
                assert question.graph_query in EVIDENCE_QUERIES, question.graph_query


# --- router (task 3.2) — Section 5.2's table --------------------------------


@pytest.mark.parametrize("question,modality", [
    ("What is the chronology of this matter?", MODALITY_GRAPH),
    ("Who are the parties and how are they related?", MODALITY_GRAPH),
    ("What obligations exist and which are breached?", MODALITY_GRAPH),
    ("Do any documents contradict each other?", MODALITY_GRAPH),
    ("under BNS section 318", MODALITY_LEXICAL),
    ("AIR 1973 SC 1461", MODALITY_LEXICAL),
    ("case number for this petition", MODALITY_LEXICAL),
    ("What does the agreement say about termination?", MODALITY_VECTOR),
    ("Summarise the background", MODALITY_VECTOR),
])
def test_router_matches_the_design_table(question, modality):
    assert route(question)[0] == modality


def test_router_explains_itself():
    _, reason = route("under BNS section 318")
    assert "exact-match signal" in reason


def test_lexical_beats_graph_when_both_signal():
    """'all the dates in section 5' wants the exact section, not a chronology."""
    assert route("every reference in section 5")[0] == MODALITY_LEXICAL


# --- planner (task 3.1) -----------------------------------------------------


def test_panel_plan_comes_from_the_profile():
    plan_obj = plan("list_of_dates")
    assert plan_obj.panel == "list_of_dates"
    assert {q.key for q in plan_obj.sub_questions} >= {"events", "hearings", "filings"}
    assert plan_obj.by_modality[MODALITY_GRAPH]


def test_chat_is_planned_from_the_turn_not_a_profile():
    """
    The turn drives the plan: it is the first sub-question and the only required
    one. The standing graph traversals follow it (they carry the named queries
    the free-text turn cannot), so this asserts the turn's own routing rather
    than the whole list.
    """
    plan_obj = plan("intelligence_chat", "What does the contract say about notice?")
    turn = [q for q in plan_obj.sub_questions if q.key == "turn"]
    assert len(turn) == 1
    assert turn[0].modality == MODALITY_VECTOR
    assert [q.key for q in plan_obj.sub_questions if q.required] == ["turn"]


def test_structural_chat_question_also_pulls_narrative_context():
    plan_obj = plan("intelligence_chat", "Who are the parties?")
    leading = [q.modality for q in plan_obj.sub_questions[:2]]
    assert leading == [MODALITY_GRAPH, MODALITY_VECTOR]
    assert [q.key for q in plan_obj.sub_questions[:2]] == ["turn", "turn_context"]


def test_chat_without_a_question_is_an_error():
    with pytest.raises(ValueError, match="requires the turn's question"):
        plan("intelligence_chat")


def test_replan_escalates_modality_rather_than_rewording():
    plan_obj = plan("applicable_laws")
    retry = replan(plan_obj, ["provisions"])
    assert retry.attempt == 1
    assert retry.sub_questions[0].modality == MODALITY_VECTOR, "graph -> vector"
    assert "escalated from graph" in retry.sub_questions[0].reason


def test_replan_is_bounded():
    """Section 5.1 step 5: the loop is bounded, max N."""
    first = plan("applicable_laws")
    second = replan(first, ["provisions"])
    assert replan(second, ["provisions"]) is None


def test_replan_with_nothing_uncovered_does_nothing():
    assert replan(plan("applicable_laws"), []) is None


# --- graph queries (task 3.3) -----------------------------------------------


@pytest.mark.parametrize("name", sorted(GRAPH_QUERIES))
def test_queries_are_parameterised_and_workspace_scoped(name):
    """String-built Cypher over extracted text is an injection; every value is a parameter."""
    cypher, params = build(name, "ws-secret-id")
    assert "ws-secret-id" not in cypher, "a value was interpolated into the query"
    assert params["workspace_id"] == "ws-secret-id"
    assert "$workspace_id" in cypher


@pytest.mark.parametrize("name", sorted(EVIDENCE_QUERIES))
def test_evidence_queries_return_provenance(name):
    """Anything that can be cited must carry where it came from."""
    cypher, _ = build(name, "ws1")
    assert "source_document_id" in cypher and "chunk_id" in cypher


@pytest.mark.parametrize("name", sorted(LOOKUP_QUERIES))
def test_lookup_queries_are_not_evidence(name):
    """An internal lookup is never rendered into a prompt, so it needs no provenance."""
    assert name not in EVIDENCE_QUERIES


def test_unknown_query_is_refused():
    with pytest.raises(KeyError, match="Unknown graph query"):
        build("no_such_query", "ws1")


def test_withdrawal_detaches_so_no_dangling_edges_remain():
    cypher, params = withdraw_document_facts("ws1", "d1")
    assert "DETACH DELETE" in cypher
    assert params == {"workspace_id": "ws1", "document_id": "d1"}


# --- assembler (task 3.6) ---------------------------------------------------


def test_evidence_without_provenance_is_dropped():
    """An uncitable passage makes an unverifiable claim look supported."""
    result = assemble([Evidence("text", "", "", score=0.9)], token_budget=1000)
    assert result["evidence"] == [] and result["droppedNoProvenance"] == 1


def test_from_chunk_rejects_a_chunk_with_no_ids():
    assert from_chunk({"text": "x"}, "vector") is None
    assert from_chunk({"text": "x", "document_id": "d", "chunk_id": "c"}, "vector") is not None


def test_from_graph_row_renders_a_fact_and_skips_provenance_keys():
    row = {"party": "X Ltd", "role": "complainant",
           "source_document_id": "d1", "chunk_id": "c1", "workspace_id": "ws1"}
    assert "workspace_id" not in render_graph_fact(row)
    evidence = from_graph_row(row, "parties")
    assert evidence.modality == "graph" and evidence.score == 1.0


def test_budget_is_a_hard_cap():
    candidates = [Evidence("x" * 400, "d1", f"c{i}", score=1.0, sub_question_key="k") for i in range(10)]
    result = assemble(candidates, token_budget=200)
    assert result["tokensUsed"] <= 200
    assert result["droppedForBudget"] == 8


def test_duplicate_chunks_are_paid_for_once_but_credited_to_each_question():
    result = assemble([
        Evidence("shared", "d1", "c1", score=0.9, sub_question_key="k1"),
        Evidence("shared", "d1", "c1", score=0.5, sub_question_key="k2"),
    ], token_budget=1000)
    assert len(result["evidence"]) == 1
    assert result["deduplicated"] == 1
    assert result["coverageByKey"] == {"k1": 1, "k2": 1}


def test_distinct_graph_facts_from_one_chunk_all_survive():
    """
    Keying dedupe on chunk_id alone collapsed every graph fact from a passage
    into one item. On a real matter that turned 11 retrieved rows into 3 and
    handed the panel 645 chars against a 12,000-token budget — the graph's whole
    contribution, discarded as "duplicates". Distinct facts share a chunk; they
    are not duplicates.
    """
    facts = [
        Evidence("party=KSCA; role=Respondent", "d1", "d1:0", score=0.9,
                 sub_question_key="parties"),
        Evidence("party=BCCI; role=Authority", "d1", "d1:0", score=0.8,
                 sub_question_key="parties"),
        Evidence("relief=Independent Audit", "d1", "d1:0", score=0.7,
                 sub_question_key="reliefs"),
    ]
    result = assemble(facts, token_budget=10_000)
    assert len(result["evidence"]) == 3
    assert result["deduplicated"] == 0
    assert result["coverageByKey"] == {"parties": 2, "reliefs": 1}
    # One chunk, reported once — this is a "was it retrieved?" lookup.
    assert result["availableChunkIds"] == ["d1:0"]


def test_an_identical_graph_fact_from_two_traversals_still_dedupes():
    result = assemble([
        Evidence("party=KSCA; role=Respondent", "d1", "d1:0", score=0.9,
                 sub_question_key="parties"),
        Evidence("party=KSCA; role=Respondent", "d1", "d1:0", score=0.4,
                 sub_question_key="obligations"),
    ], token_budget=10_000)
    assert len(result["evidence"]) == 1
    assert result["deduplicated"] == 1
    assert result["coverageByKey"] == {"parties": 1, "obligations": 1}


def test_interleaving_stops_one_question_starving_another():
    candidates = [
        *[Evidence("a" * 400, "d1", f"a{i}", score=1.0, sub_question_key="loud") for i in range(5)],
        Evidence("b" * 400, "d1", "b1", score=0.2, sub_question_key="quiet"),
    ]
    result = assemble(candidates, token_budget=200)
    assert set(result["coverageByKey"]) == {"loud", "quiet"}


# --- critic (task 3.7) ------------------------------------------------------


def _plan_with(keys, required=True):
    return RetrievalPlan(
        panel="test",
        sub_questions=[SubQuestion(k, k, MODALITY_VECTOR, required=required) for k in keys],
    )


def test_uncovered_required_question_triggers_a_replan():
    verdict = critique(_plan_with(["a"]), {"coverageByKey": {}, "evidence": []})
    assert verdict.uncovered == ["a"] and verdict.should_replan
    assert not verdict.is_adequate


def test_uncovered_optional_question_does_not_trigger_a_replan():
    verdict = critique(_plan_with(["a"], required=False), {"coverageByKey": {}, "evidence": []})
    assert verdict.optional_uncovered == ["a"] and not verdict.should_replan


def test_thin_support_is_distinguished_from_absent():
    assembly = {"coverageByKey": {"a": 1},
                "evidence": [{"subQuestionKey": "a", "score": 0.9}]}
    verdict = critique(_plan_with(["a"]), assembly)
    assert verdict.thin == ["a"] and verdict.uncovered == []


def test_adequate_coverage_needs_no_loop():
    assembly = {"coverageByKey": {"a": 3},
                "evidence": [{"subQuestionKey": "a", "score": 0.9} for _ in range(3)]}
    verdict = critique(_plan_with(["a"]), assembly)
    assert verdict.is_adequate and not verdict.should_replan


def test_budget_limited_shortfall_does_not_trigger_a_pointless_loop():
    """Re-fetching evidence the budget already discarded is wasted work."""
    assembly = {"coverageByKey": {}, "evidence": [], "droppedForBudget": 5, "tokenBudget": 100}
    verdict = critique(_plan_with(["a"]), assembly)
    assert verdict.uncovered == ["a"]
    assert verdict.budget_limited and not verdict.should_replan
    assert any("budget constraint" in note for note in verdict.notes)


# --- verifier (task 3.8) ----------------------------------------------------


EVIDENCE = [{"chunkId": "c1", "documentId": "d1", "page": 3,
             "text": "BNS section 318 cheating applies to the accused; cheque for Rs 4,50,000"}]


def test_supported_claim_keeps_its_citation():
    result = verify_claims(
        [{"text": "BNS section 318 on cheating applies to the accused", "citations": ["c1"]}],
        EVIDENCE,
    )
    assert result["supported"] == 1
    assert result["citationPayload"][0]["citations"][0]["documentId"] == "d1"


def test_unsupported_claim_is_dropped_when_the_contract_has_no_place_for_it():
    result = verify_claims([{"text": "The accused absconded to Dubai"}], EVIDENCE)
    assert result["dropped"] == 1 and result["survivingClaims"] == []


def test_unsupported_claim_is_demoted_when_the_contract_allows():
    result = verify_claims(
        [{"text": "The accused absconded to Dubai"}], EVIDENCE, allow_unverified=True
    )
    assert result["unverified"] == 1 and len(result["survivingClaims"]) == 1


def test_a_citation_to_a_chunk_never_retrieved_confers_no_support():
    """Otherwise the metric measures willingness to emit chunk ids, not grounding."""
    result = verify_claims(
        [{"text": "Something entirely unrelated to cheques", "citations": ["never-retrieved"]}],
        EVIDENCE, allow_unverified=True,
    )
    claim = result["claims"][0]
    assert claim["status"] == "unverified"
    assert "never returned" in claim["reason"]


@pytest.mark.parametrize("claim,consistent", [
    ("section 318 applies", True),
    ("section 420 applies", False),
    ("the cheque was for Rs 4,50,000", True),
    ("the cheque was for Rs 9,00,000", False),
    ("cheating is alleged", True),
])
def test_numeric_consistency_guard(claim, consistent):
    assert numbers_are_consistent(claim, EVIDENCE[0]["text"]) is consistent


def test_a_wrong_section_number_is_not_supported_by_similar_prose():
    """The exact failure this programme exists to prevent."""
    result = verify_claims([{"text": "Section 420 applies"}], EVIDENCE, allow_unverified=True)
    assert result["claims"][0]["status"] == "unverified"
    assert result["supported"] == 0


def test_number_mismatch_zeroes_the_overlap():
    assert overlap_ratio("section 420 applies", EVIDENCE[0]["text"]) == 0.0
    assert overlap_ratio("section 318 applies", EVIDENCE[0]["text"]) > 0.0


def test_traceability_is_the_metric_101_figure():
    result = verify_claims([
        {"text": "BNS section 318 cheating applies to the accused", "citations": ["c1"]},
        {"text": "Unrelated invented claim about Dubai"},
    ], EVIDENCE, allow_unverified=True)
    assert result["traceability"] == 0.5


def test_no_claims_is_perfect_traceability_not_a_crash():
    assert verify_claims([], EVIDENCE)["traceability"] == 1.0


# --- chat could not reach the graph at all (found on the BCCI matter) --------


def test_chat_plan_carries_named_traversals_not_just_the_turn():
    """
    The turn's question is free text and carries no ``graph_query``, and
    ``retrieve_for_panel`` skips a graph sub-question without one — so a chat
    plan built from the question alone could never reach the graph, whatever
    RETRIEVAL_PANELS said. The profile's standing traversals supply the queries.
    """
    from app.retrieval.planner import plan

    p = plan("intelligence_chat", "What reliefs are being sought?")
    keys = [q.key for q in p.sub_questions]
    assert "turn" in keys
    graph_qs = [q for q in p.sub_questions if q.modality == "graph"]
    assert graph_qs, "chat plan has no graph sub-question"
    assert all(q.graph_query for q in graph_qs), "a graph sub-question has no query"
    assert {"party_network", "chronology", "reliefs", "obligation_chain"} <= {
        q.graph_query for q in graph_qs
    }


def test_chat_graph_traversals_are_optional():
    """A general legal question must not be called 'uncovered' because the
    matter happens to have no Relief nodes."""
    from app.retrieval.planner import plan

    p = plan("intelligence_chat", "What is the limitation period for this claim?")
    for q in p.sub_questions:
        if q.graph_query:
            assert not q.required, f"{q.key} is required; chat must answer without the graph"


def test_chat_still_requires_a_question():
    from app.retrieval.planner import plan
    import pytest as _pytest

    with _pytest.raises(ValueError, match="question"):
        plan("intelligence_chat", "")
