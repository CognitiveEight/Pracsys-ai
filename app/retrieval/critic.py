"""
The coverage critic — does the assembled evidence actually answer the plan?
(work-plan task 3.7)

Section 5.1 step 5: *"is any sub-question unanswered or thinly supported? — yes
→ reformulate and loop (bounded, max N)"*.

The critic is what makes the loop agentic rather than a fixed pipeline: it runs
between assembly and generation, and it can send the planner back round. Two
distinctions it draws that a simple "did we get any hits" check cannot:

* **Unanswered vs thin.** Zero evidence for a required sub-question is a
  coverage failure. One weak passage is *thin* — the panel can proceed, but the
  gap belongs in the report.
* **Retrieval-thin vs budget-thin.** If the assembler dropped items for the
  token budget, thin coverage is a budgeting problem and re-running retrieval
  will not fix it. The critic says which, because escalating modality to chase
  evidence the budget already threw away is wasted work.

The verdict is advisory data, not a decision: the caller decides whether to loop
or proceed. What the critic guarantees is that proceeding with thin evidence is
a recorded choice rather than an unnoticed one.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from loguru import logger

#: Evidence count at or below which a sub-question counts as thinly supported.
DEFAULT_THIN_THRESHOLD = 1

#: Mean score below which support counts as weak even if plentiful.
DEFAULT_WEAK_SCORE = 0.3


@dataclass
class CoverageVerdict:
    """
    The critic's assessment of one retrieval round.

    Attributes:
        covered: Sub-question keys with adequate support.
        thin: Keys supported, but barely.
        uncovered: Required keys with no support at all.
        optional_uncovered: Non-required keys with no support. Reported, never
            a reason to loop.
        should_replan: True when looping is likely to help.
        budget_limited: True when the assembler dropped items for budget, so
            thin coverage is a budget problem rather than a retrieval one.
        notes: Human-readable findings for the report.
    """

    covered: List[str] = field(default_factory=list)
    thin: List[str] = field(default_factory=list)
    uncovered: List[str] = field(default_factory=list)
    optional_uncovered: List[str] = field(default_factory=list)
    should_replan: bool = False
    budget_limited: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def is_adequate(self) -> bool:
        """True when every required sub-question has at least adequate support."""
        return not self.uncovered and not self.thin

    @property
    def keys_to_retry(self) -> List[str]:
        """Keys worth another retrieval round: uncovered first, then thin."""
        return [*self.uncovered, *self.thin]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "covered": self.covered,
            "thin": self.thin,
            "uncovered": self.uncovered,
            "optionalUncovered": self.optional_uncovered,
            "shouldReplan": self.should_replan,
            "budgetLimited": self.budget_limited,
            "isAdequate": self.is_adequate,
            "notes": self.notes,
        }


def critique(
    plan: Any,
    assembly: Mapping[str, Any],
    thin_threshold: int = DEFAULT_THIN_THRESHOLD,
    weak_score: float = DEFAULT_WEAK_SCORE,
) -> CoverageVerdict:
    """
    Assess whether the assembled evidence answers the plan.

    Args:
        plan: A :class:`app.retrieval.planner.RetrievalPlan`.
        assembly: Output of :func:`app.retrieval.assembler.assemble`.
        thin_threshold: Evidence count at or below which support is thin.
        weak_score: Mean score below which support is weak despite its count.

    Returns:
        The verdict.
    """
    coverage: Mapping[str, int] = assembly.get("coverageByKey", {}) or {}
    evidence: Sequence[Mapping[str, Any]] = assembly.get("evidence", []) or []
    budget_limited = bool(assembly.get("droppedForBudget"))

    scores_by_key: Dict[str, List[float]] = {}
    for item in evidence:
        scores_by_key.setdefault(str(item.get("subQuestionKey", "")), []).append(
            float(item.get("score") or 0.0)
        )

    verdict = CoverageVerdict(budget_limited=budget_limited)

    for question in plan.sub_questions:
        key = question.key
        count = coverage.get(key, 0)
        if count == 0:
            if question.required:
                verdict.uncovered.append(key)
                verdict.notes.append(f"'{question.question}' returned no evidence")
            else:
                verdict.optional_uncovered.append(key)
            continue

        scores = scores_by_key.get(key, [])
        mean_score = sum(scores) / len(scores) if scores else 0.0
        if count <= thin_threshold or (scores and mean_score < weak_score):
            verdict.thin.append(key)
            verdict.notes.append(
                f"'{question.question}' has thin support "
                f"({count} item{'s' if count != 1 else ''}, mean score {mean_score:.2f})"
            )
        else:
            verdict.covered.append(key)

    # Looping helps only when there is something to chase AND the shortfall is a
    # retrieval problem. If the budget already discarded evidence, fetching more
    # of it will just be discarded again.
    verdict.should_replan = bool(verdict.uncovered) and not budget_limited
    if budget_limited and (verdict.thin or verdict.uncovered):
        verdict.notes.append(
            f"{assembly.get('droppedForBudget')} items were dropped for the "
            f"{assembly.get('tokenBudget')}-token budget — thin coverage here is a "
            "budget constraint, not a retrieval failure; raise the budget rather "
            "than re-running retrieval"
        )

    if verdict.uncovered:
        logger.warning(
            "Coverage critic: {} required sub-question(s) unanswered for {}: {}",
            len(verdict.uncovered),
            plan.panel,
            verdict.uncovered,
        )
    return verdict
