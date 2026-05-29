"""LangGraph subgraph that matches a drafting suggestion to a lawyer's
template using parallel "deep-agent" understanding.

This formalizes the three phases of template matching as a LangGraph
``StateGraph`` (mirroring the established pattern in ``strategy_agent.py`` /
``intelligence_chat_agent.py`` / ``role_agent.py``):

    gather  → list ready candidate templates from the in-memory store
    understand → classify the suggestion + lazily classify candidates,
                 running focused sub-agents in parallel via asyncio.gather
                 ("deep agents in spirit" — no extra dependency)
    decide  → type-first selection + anchor-overlap fallback + LLM tiebreak

The node bodies delegate to the proven helper methods on ``DraftingAgent``
(`_tm_gather`, `_tm_understand`, `_tm_decide`) so this is an orchestration
layer, not a reimplementation. A conditional edge skips the LLM-heavy
``understand``/``decide`` nodes entirely when the library is empty.

Public entrypoint: :func:`run_template_match`, returning the same
``(payload, ready_library_size)`` tuple the old monolithic matcher did, so
callers (`_attach_template_matches`, `_handle_kickoff`) are unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, StateGraph
from loguru import logger


class TemplateMatchState(TypedDict, total=False):
    """State threaded through the template-match graph.

    ``agent`` and ``ready`` are passed by reference (LangGraph does not deep
    copy arbitrary objects), which is intentional: the ``understand`` node
    backfills classifications onto the ``ready`` payloads in place so
    ``decide`` sees them.
    """

    agent: Any
    user_id: str
    suggestion_title: str
    suggestion_description: str
    fallback_text: str
    suggestion_text: str
    ready: List[Dict[str, Any]]
    ready_size: int
    suggestion_class: Optional[Dict[str, Any]]
    result: Optional[Dict[str, Any]]


async def _gather_node(state: TemplateMatchState) -> Dict[str, Any]:
    agent = state["agent"]
    ready, ready_size = await agent._tm_gather(
        user_id=state["user_id"],
        suggestion_text=state["suggestion_text"],
    )
    return {"ready": ready, "ready_size": ready_size}


async def _understand_node(state: TemplateMatchState) -> Dict[str, Any]:
    agent = state["agent"]
    suggestion_class = await agent._tm_understand(
        user_id=state["user_id"],
        suggestion_title=state["suggestion_title"],
        suggestion_description=state["suggestion_description"],
        fallback_text=state["fallback_text"],
        ready=state["ready"],
    )
    return {"suggestion_class": suggestion_class}


async def _decide_node(state: TemplateMatchState) -> Dict[str, Any]:
    agent = state["agent"]
    result = await agent._tm_decide(
        user_id=state["user_id"],
        suggestion_title=state["suggestion_title"],
        suggestion_description=state["suggestion_description"],
        suggestion_text=state["suggestion_text"],
        ready=state["ready"],
        ready_size=state["ready_size"],
        suggestion_class=state.get("suggestion_class"),
    )
    return {"result": result}


def _route_after_gather(state: TemplateMatchState) -> str:
    """Skip the LLM-heavy understand/decide nodes when there is nothing to
    match against — an empty library is a deterministic no-match."""
    return "understand" if state.get("ready") else "end"


def _build_graph():
    graph = StateGraph(TemplateMatchState)
    graph.add_node("gather", _gather_node)
    graph.add_node("understand", _understand_node)
    graph.add_node("decide", _decide_node)
    graph.set_entry_point("gather")
    graph.add_conditional_edges(
        "gather",
        _route_after_gather,
        {"understand": "understand", "end": END},
    )
    graph.add_edge("understand", "decide")
    graph.add_edge("decide", END)
    return graph.compile()


# Compiled once at import — the graph is stateless; per-call state is passed
# to ainvoke, so a single compiled instance is safe to share.
_TEMPLATE_MATCH_GRAPH = _build_graph()


async def run_template_match(
    agent: Any,
    *,
    user_id: str,
    suggestion_title: str,
    suggestion_description: str,
    fallback_text: str = "",
) -> Tuple[Optional[Dict[str, Any]], int]:
    """Run the template-match graph and return ``(payload, ready_size)``.

    Returns ``(None, 0)`` on empty input or any graph error so callers can
    degrade gracefully to AI generation.
    """
    suggestion_text = " ".join(
        s for s in (suggestion_title, suggestion_description, fallback_text) if s
    ).strip()
    if not user_id or not suggestion_text:
        if user_id:
            logger.info(
                "Template matcher: no suggestion / fallback text — skipping "
                "match (user={})",
                (user_id or "")[:12],
            )
        return None, 0

    initial: TemplateMatchState = {
        "agent": agent,
        "user_id": user_id,
        "suggestion_title": suggestion_title,
        "suggestion_description": suggestion_description,
        "fallback_text": fallback_text,
        "suggestion_text": suggestion_text,
        "ready": [],
        "ready_size": 0,
        "suggestion_class": None,
        "result": None,
    }
    try:
        final = await _TEMPLATE_MATCH_GRAPH.ainvoke(initial)
    except Exception as e:
        logger.warning("Template match graph failed (user={}): {}", (user_id or "")[:12], e)
        return None, 0
    return final.get("result"), int(final.get("ready_size") or 0)
