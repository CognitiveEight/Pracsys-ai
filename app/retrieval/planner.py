"""
The retrieval planner and modality router (work-plan tasks 3.1 and 3.2).

Section 5.1 step 1: decompose a panel's information need into typed
sub-questions using the ontology. Step 2: choose a modality per sub-question —
graph for structural/relational/chronological facts, vector for semantic
passages, lexical for exact citations and case numbers.

"Agentic" means the plan is *derived*, not fixed: :func:`plan` starts from the
panel's declared profile and then adapts it — a chat turn is planned from the
question itself, and :func:`replan` narrows the plan to what the coverage critic
found missing rather than re-running everything.

The router is deliberately rule-based rather than model-driven. Modality choice
is a structural property of the question ("is this an exact-string lookup?"),
it must be explainable when a panel regresses, and it must not cost a model call
on the critical path of every retrieval.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from app.retrieval.profiles import (
    MODALITY_GRAPH,
    MODALITY_LEXICAL,
    MODALITY_VECTOR,
    SubQuestionSpec,
    get_profile,
)

#: Signals that a question wants an exact string, not a similar passage.
_LEXICAL_SIGNALS = (
    r"\bsection\s+\d",
    r"\bs\.\s*\d",
    r"\bcitation\b",
    r"\bcited\s+as\b",
    r"\bcase\s+(?:no|number)\b",
    r"\bcrl\b",
    r"\bw\.?p\.?\b",
    r"\bo\.?s\.?\b",
    r"\bair\s+\d{4}\b",
    r"\bscc\b",
    r"\bfir\s+(?:no|number)\b",
    r"\bcheque\s+(?:no|number)\b",
    r"\bexactly\b",
    r"\bverbatim\b",
    r"\bact\b.*\bsection\b",
    r"\bunder\s+(?:bns|bnss|bsa|ipc|crpc)\b",
)

#: Signals that a question is structural or relational — a graph question.
_GRAPH_SIGNALS = (
    r"\bchronolog",
    r"\btimeline\b",
    r"\bsequence\s+of\b",
    r"\border\s+of\s+events\b",
    r"\bwho\s+(?:is|are|was|were)\b",
    r"\bparties\b",
    r"\bwhich\s+part(?:y|ies)\b",
    r"\brelationship\b",
    r"\brelated\s+to\b",
    r"\bobligation",
    r"\bbreach",
    r"\bcontradict",
    r"\bconflict\b",
    r"\binconsisten",
    r"\bhow\s+many\b",
    r"\bevery\b",
    r"\ball\s+(?:the\s+)?(?:dates|hearings|filings|orders|parties)\b",
    r"\bwhen\s+did\b",
    r"\bbefore\s+or\s+after\b",
)

_COMPILED_LEXICAL = [re.compile(p, re.IGNORECASE) for p in _LEXICAL_SIGNALS]
_COMPILED_GRAPH = [re.compile(p, re.IGNORECASE) for p in _GRAPH_SIGNALS]


@dataclass
class SubQuestion:
    """
    One planned sub-question, ready to execute.

    Attributes:
        key: Stable identifier; how coverage is reported.
        question: The natural-language need.
        modality: Where it will be sent.
        node_labels: Ontology labels targeted, for a graph question.
        graph_query: Named query in :mod:`app.retrieval.graph_queries`.
        required: Whether the coverage critic treats it as mandatory.
        reason: Why this modality was chosen. Kept so a regression can be
            explained rather than guessed at.
        attempt: Which planning round produced it; >0 means a reformulation.
    """

    key: str
    question: str
    modality: str
    node_labels: Tuple[str, ...] = ()
    graph_query: Optional[str] = None
    required: bool = True
    reason: str = ""
    attempt: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "question": self.question,
            "modality": self.modality,
            "nodeLabels": list(self.node_labels),
            "graphQuery": self.graph_query,
            "required": self.required,
            "reason": self.reason,
            "attempt": self.attempt,
        }


@dataclass
class RetrievalPlan:
    """
    A panel's decomposed information need.

    Attributes:
        panel: Panel identifier.
        sub_questions: The decomposition.
        token_budget: Evidence cap for the assembler.
        attempt: Planning round; >0 means this is a reformulation.
    """

    panel: str
    sub_questions: List[SubQuestion] = field(default_factory=list)
    token_budget: int = 8000
    attempt: int = 0

    @property
    def by_modality(self) -> Dict[str, List[SubQuestion]]:
        """Group the plan by modality, for parallel dispatch (Section 5.1 step 3)."""
        grouped: Dict[str, List[SubQuestion]] = {}
        for question in self.sub_questions:
            grouped.setdefault(question.modality, []).append(question)
        return grouped

    @property
    def required_keys(self) -> Tuple[str, ...]:
        return tuple(q.key for q in self.sub_questions if q.required)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "panel": self.panel,
            "attempt": self.attempt,
            "tokenBudget": self.token_budget,
            "subQuestions": [q.to_dict() for q in self.sub_questions],
            "modalityCounts": {m: len(q) for m, q in self.by_modality.items()},
        }


def route(question: str, default: str = MODALITY_VECTOR) -> Tuple[str, str]:
    """
    Choose a retrieval modality for one question (task 3.2).

    Lexical is tested first and wins outright: a question naming a section
    number or a citation wants that exact string, and a semantic search for it
    returns passages that merely discuss it. Graph is tested next — structural
    and relational questions. Vector is the default, because it is the right
    answer for genuinely semantic questions and the safe answer for
    unclassifiable ones.

    Args:
        question: The sub-question text.
        default: Modality when no signal matches.

    Returns:
        ``(modality, reason)`` — the reason is kept in the plan so a modality
        choice can be explained when a panel regresses.
    """
    text = str(question or "")
    for pattern in _COMPILED_LEXICAL:
        match = pattern.search(text)
        if match:
            return MODALITY_LEXICAL, f"exact-match signal {match.group(0)!r}"
    for pattern in _COMPILED_GRAPH:
        match = pattern.search(text)
        if match:
            return MODALITY_GRAPH, f"structural signal {match.group(0)!r}"
    return default, "no exact-match or structural signal; semantic by default"


def _from_spec(spec: SubQuestionSpec, attempt: int = 0) -> SubQuestion:
    """Build a planned sub-question from a profile's declaration."""
    return SubQuestion(
        key=spec.key,
        question=spec.question,
        modality=spec.modality,
        node_labels=spec.node_labels,
        graph_query=spec.graph_query,
        required=spec.required,
        reason="declared by the panel's retrieval profile",
        attempt=attempt,
    )


def plan(panel: str, question: Optional[str] = None) -> RetrievalPlan:
    """
    Decompose a panel's information need into typed sub-questions (task 3.1).

    For every panel except chat, the profile *is* the plan: the decomposition is
    a property of the panel, agreed once and reviewable, not re-derived per
    request. For ``intelligence_chat`` there is no fixed need — the turn's own
    question is routed, which is what Section 6.5 means by "per-turn retrieval
    planning".

    Args:
        panel: Panel identifier.
        question: The user's question. Required for ``intelligence_chat``;
            ignored by the other panels.

    Returns:
        The plan.

    Raises:
        KeyError: On an unknown panel.
        ValueError: If ``intelligence_chat`` is planned with no question.
    """
    profile = get_profile(panel)

    if panel == "intelligence_chat":
        if not question or not question.strip():
            raise ValueError("intelligence_chat requires the turn's question to plan retrieval")
        modality, reason = route(question)
        sub_questions = [
            SubQuestion(
                key="turn",
                question=question.strip(),
                modality=modality,
                required=True,
                reason=reason,
            )
        ]
        # A structural chat question still wants the passage that supports the
        # answer, so pair the graph hop with a semantic one rather than choosing.
        if modality == MODALITY_GRAPH:
            sub_questions.append(
                SubQuestion(
                    key="turn_context",
                    question=question.strip(),
                    modality=MODALITY_VECTOR,
                    required=False,
                    reason="narrative context supporting a structural answer",
                )
            )
        # The turn's own question is routed but carries no `graph_query` — it is
        # free text, not a named traversal — so on its own it can never reach the
        # graph: `retrieve_for_panel` skips a graph sub-question with no query.
        # Chat was therefore structurally incapable of using the knowledge graph,
        # whatever the flag said. The profile's standing traversals supply the
        # named queries, so a chat turn grounds on the same facts the panels do.
        # All optional: chat must still answer a question the graph cannot.
        sub_questions.extend(
            _from_spec(spec)
            for spec in profile.sub_questions
            if spec.modality == MODALITY_GRAPH and spec.graph_query
        )
        return RetrievalPlan(
            panel=panel, sub_questions=sub_questions, token_budget=profile.token_budget
        )

    plan_obj = RetrievalPlan(
        panel=panel,
        sub_questions=[_from_spec(spec) for spec in profile.sub_questions],
        token_budget=profile.token_budget,
    )
    logger.debug(
        "Planned {}: {} sub-questions {}",
        panel,
        len(plan_obj.sub_questions),
        plan_obj.to_dict()["modalityCounts"],
    )
    return plan_obj


def replan(
    previous: RetrievalPlan,
    uncovered_keys: Sequence[str],
    max_attempts: int = 2,
) -> Optional[RetrievalPlan]:
    """
    Reformulate the plan for the sub-questions coverage did not answer
    (Section 5.1 step 5's bounded loop).

    Reformulation is a **modality escalation**, not a reworded question: if the
    graph had no answer, the fact may be in prose the graph never captured, so
    the retry goes to vector. If vector found nothing, an exact identifier may
    be what is needed, so it escalates to lexical. Rewording the same question
    into the same modality would mostly re-fetch the same nothing.

    Args:
        previous: The plan that under-covered.
        uncovered_keys: Sub-question keys with no or thin support.
        max_attempts: Hard bound on planning rounds. The loop must terminate.

    Returns:
        A narrowed plan for the next attempt, or None when there is nothing left
        to escalate or the bound is reached.
    """
    if previous.attempt + 1 >= max_attempts:
        logger.info(
            "Retrieval replanning stopped at the {}-attempt bound for {}",
            max_attempts,
            previous.panel,
        )
        return None
    if not uncovered_keys:
        return None

    escalation = {
        MODALITY_GRAPH: MODALITY_VECTOR,
        MODALITY_VECTOR: MODALITY_LEXICAL,
        MODALITY_LEXICAL: MODALITY_VECTOR,
    }
    uncovered = set(uncovered_keys)
    retries: List[SubQuestion] = []
    for question in previous.sub_questions:
        if question.key not in uncovered:
            continue
        next_modality = escalation[question.modality]
        if next_modality == question.modality:
            continue
        retries.append(
            SubQuestion(
                key=question.key,
                question=question.question,
                modality=next_modality,
                node_labels=question.node_labels,
                graph_query=None,  # escalating away from the graph
                required=question.required,
                reason=f"escalated from {question.modality} after no coverage",
                attempt=previous.attempt + 1,
            )
        )

    if not retries:
        return None
    logger.info(
        "Replanning {} attempt {}: escalating {} sub-questions",
        previous.panel,
        previous.attempt + 1,
        len(retries),
    )
    return RetrievalPlan(
        panel=previous.panel,
        sub_questions=retries,
        token_budget=previous.token_budget,
        attempt=previous.attempt + 1,
    )
