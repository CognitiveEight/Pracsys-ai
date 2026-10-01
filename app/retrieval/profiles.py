"""
Per-panel retrieval profiles — what each panel actually needs (task 3.9).

Section 5.2 makes the central claim of the retrieval design: a single similarity
search serves *none* of the panels well, because the panels ask structurally
different questions. A chronology is a global ordering across documents; a
statutory citation is exact-string work; "what does the agreement say about
termination" is genuinely semantic.

This module is the data behind that claim. Each panel declares the typed
sub-questions it needs answered and the modality each one should be routed to,
in the Phase 4 cutover order — most structurally determined first, most
synthetic last, so confidence in the graph is established before it carries the
harder reasoning.
"""

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

#: Retrieval modalities. Section 5.1 step 2.
MODALITY_GRAPH = "graph"
MODALITY_VECTOR = "vector"
MODALITY_LEXICAL = "lexical"
MODALITIES = (MODALITY_GRAPH, MODALITY_VECTOR, MODALITY_LEXICAL)


@dataclass(frozen=True)
class SubQuestionSpec:
    """
    One typed information need within a panel.

    Attributes:
        key: Stable identifier, used in coverage reporting.
        question: The sub-question in natural language, for the vector/lexical
            paths and for the coverage critic's explanation.
        modality: Which retrieval modality answers it.
        node_labels: Ontology labels this sub-question targets, when it is a
            graph question.
        required: When True, the coverage critic treats an unanswered
            sub-question as a coverage failure rather than a gap to note.
        graph_query: Name of the query in
            :mod:`app.retrieval.graph_queries` that answers it.
    """

    key: str
    question: str
    modality: str
    node_labels: Tuple[str, ...] = ()
    required: bool = True
    graph_query: Optional[str] = None


@dataclass(frozen=True)
class PanelProfile:
    """
    The retrieval plan for one panel.

    Attributes:
        panel: Panel identifier, matching the socket ``featureType`` and the
            eval harness's panel names.
        cutover_order: Position in the Phase 4 migration order.
        primary_modality: The modality this panel mostly depends on; what its
            quality gate is really testing.
        rationale: Why this panel sits where it does in the order.
        sub_questions: The decomposition.
        token_budget: Cap on assembled evidence for this panel.
    """

    panel: str
    cutover_order: int
    primary_modality: str
    rationale: str
    sub_questions: Tuple[SubQuestionSpec, ...]
    token_budget: int = 8000

    @property
    def required_keys(self) -> Tuple[str, ...]:
        return tuple(q.key for q in self.sub_questions if q.required)


def _q(key, question, modality, labels=(), required=True, graph_query=None) -> SubQuestionSpec:
    return SubQuestionSpec(key, question, modality, tuple(labels), required, graph_query)


PANEL_PROFILES: Tuple[PanelProfile, ...] = (
    PanelProfile(
        panel="list_of_dates",
        cutover_order=1,
        primary_modality=MODALITY_GRAPH,
        rationale="Purely structural; the cleanest test of graph quality.",
        sub_questions=(
            _q(
                "events",
                "All dated events in this matter",
                MODALITY_GRAPH,
                ("Event",),
                True,
                "chronology",
            ),
            _q(
                "hearings",
                "All hearings, past and scheduled",
                MODALITY_GRAPH,
                ("Hearing",),
                True,
                "chronology",
            ),
            _q(
                "filings",
                "All filings and the dates they were filed",
                MODALITY_GRAPH,
                ("Filing",),
                True,
                "chronology",
            ),
            _q("limitation", "Events that start a limitation period", MODALITY_VECTOR, (), False),
            _q(
                "next_dates",
                "Orders fixing a next date",
                MODALITY_GRAPH,
                ("Order",),
                False,
                "orders",
            ),
        ),
    ),
    PanelProfile(
        panel="relevant_parties",
        cutover_order=2,
        primary_modality=MODALITY_GRAPH,
        rationale="Directly exercises entity resolution.",
        sub_questions=(
            _q(
                "parties",
                "Every party to this matter and their role",
                MODALITY_GRAPH,
                ("Party",),
                True,
                "party_network",
            ),
            _q(
                "advocates",
                "Counsel on record and who they appear for",
                MODALITY_GRAPH,
                ("Advocate",),
                True,
                "party_network",
            ),
            _q(
                "relationships",
                "How the parties relate to each other",
                MODALITY_GRAPH,
                ("Party", "Obligation"),
                False,
                "party_network",
            ),
        ),
    ),
    PanelProfile(
        panel="procedural_timeline",
        cutover_order=3,
        primary_modality=MODALITY_GRAPH,
        rationale="Structural, with forward-looking derivation.",
        sub_questions=(
            _q(
                "hearings",
                "Hearings and their outcomes",
                MODALITY_GRAPH,
                ("Hearing",),
                True,
                "chronology",
            ),
            _q(
                "orders",
                "Orders passed and their operative directions",
                MODALITY_GRAPH,
                ("Order",),
                True,
                "orders",
            ),
            _q(
                "filings",
                "Filings and who made them",
                MODALITY_GRAPH,
                ("Filing",),
                True,
                "chronology",
            ),
            _q("stage", "The current procedural stage", MODALITY_VECTOR, (), False),
        ),
    ),
    PanelProfile(
        panel="applicable_laws",
        cutover_order=4,
        primary_modality=MODALITY_GRAPH,
        rationale="Validates statutory normalisation end to end.",
        sub_questions=(
            _q(
                "provisions",
                "Statutory provisions engaged by this matter",
                MODALITY_GRAPH,
                ("Provision", "Statute"),
                True,
                "provisions",
            ),
            _q(
                "citations",
                "Exact statutory citations appearing in the documents",
                MODALITY_LEXICAL,
                (),
                True,
            ),
            _q("offences", "Conduct alleged, for mapping to offences", MODALITY_VECTOR, (), False),
        ),
    ),
    PanelProfile(
        panel="case_laws",
        cutover_order=5,
        primary_modality=MODALITY_GRAPH,
        rationale="Builds on validated provisions.",
        sub_questions=(
            _q(
                "precedents",
                "Precedents cited in the documents",
                MODALITY_GRAPH,
                ("Precedent",),
                True,
                "authority_chain",
            ),
            _q(
                "citations",
                "Exact case citations and neutral citations",
                MODALITY_LEXICAL,
                (),
                True,
            ),
            _q(
                "propositions",
                "Legal propositions the matter turns on",
                MODALITY_VECTOR,
                (),
                False,
            ),
        ),
    ),
    PanelProfile(
        panel="analysis_report",
        cutover_order=6,
        primary_modality=MODALITY_VECTOR,
        rationale="Synthetic; depends on everything above.",
        sub_questions=(
            _q(
                "obligations",
                "Obligations and which are breached",
                MODALITY_GRAPH,
                ("Obligation", "Breach"),
                True,
                "obligation_chain",
            ),
            _q(
                "contradictions",
                "Contradictions between the documents",
                MODALITY_GRAPH,
                (),
                True,
                "contradictions",
            ),
            _q(
                "chronology",
                "The chronology, for gaps",
                MODALITY_GRAPH,
                ("Event",),
                True,
                "chronology",
            ),
            _q("strengths", "Passages supporting the case", MODALITY_VECTOR, (), True),
            _q("risks", "Passages exposing risk or weakness", MODALITY_VECTOR, (), True),
            _q(
                "provisions",
                "Provisions engaged",
                MODALITY_GRAPH,
                ("Provision",),
                False,
                "provisions",
            ),
        ),
        token_budget=12000,
    ),
    PanelProfile(
        panel="strategy_objective",
        cutover_order=7,
        primary_modality=MODALITY_VECTOR,
        rationale="Most synthetic; migrate last.",
        sub_questions=(
            _q(
                "reliefs",
                "Reliefs sought and by whom",
                MODALITY_GRAPH,
                ("Relief",),
                True,
                "reliefs",
            ),
            _q(
                "obligations",
                "Obligation and breach structure",
                MODALITY_GRAPH,
                ("Obligation", "Breach"),
                True,
                "obligation_chain",
            ),
            _q("parties", "Party positions", MODALITY_GRAPH, ("Party",), True, "party_network"),
            _q("narrative", "The narrative account of the dispute", MODALITY_VECTOR, (), True),
            _q(
                "authority",
                "Authorities relied on",
                MODALITY_GRAPH,
                ("Precedent",),
                False,
                "authority_chain",
            ),
        ),
        token_budget=12000,
    ),
    PanelProfile(
        panel="intelligence_chat",
        cutover_order=8,
        primary_modality=MODALITY_VECTOR,
        rationale="Per-turn retrieval planning; migrate once all panels are proven.",
        # The turn's own question is the primary need and stays vector-routed.
        # The graph sub-questions were added because a vector-only profile made
        # this panel structurally unable to use the knowledge graph at all: with
        # Keel unconfigured its single sub-question had nowhere to go and chat
        # retrieved nothing, whatever the flag said. They are all
        # `required=False` — chat must still answer a question the graph holds no
        # facts for, and the coverage critic should not call a general legal
        # question "uncovered" merely because the matter has no Relief nodes.
        sub_questions=(
            _q("turn", "The user's question for this turn", MODALITY_VECTOR, (), True),
            _q("parties", "Who the parties are and their roles", MODALITY_GRAPH,
               ("Party",), False, "party_network"),
            _q("chronology", "The dated spine of the matter", MODALITY_GRAPH,
               ("Event", "Hearing", "Filing"), False, "chronology"),
            _q("reliefs", "Reliefs sought", MODALITY_GRAPH,
               ("Relief",), False, "reliefs"),
            _q("obligations", "Obligations and alleged breaches", MODALITY_GRAPH,
               ("Obligation", "Breach"), False, "obligation_chain"),
        ),
        token_budget=6000,
    ),
)

PROFILES_BY_PANEL: Mapping[str, PanelProfile] = {p.panel: p for p in PANEL_PROFILES}

#: Phase 4 cutover order.
CUTOVER_ORDER: Tuple[str, ...] = tuple(
    p.panel for p in sorted(PANEL_PROFILES, key=lambda p: p.cutover_order)
)


def get_profile(panel: str) -> PanelProfile:
    """
    Return the retrieval profile for a panel.

    Args:
        panel: Panel identifier.

    Returns:
        The profile.

    Raises:
        KeyError: If the panel has no profile — a new panel must declare what it
            needs rather than silently falling back to a generic search.
    """
    try:
        return PROFILES_BY_PANEL[panel]
    except KeyError:
        raise KeyError(
            f"No retrieval profile for panel {panel!r}; known panels: "
            f"{', '.join(sorted(PROFILES_BY_PANEL))}"
        ) from None
