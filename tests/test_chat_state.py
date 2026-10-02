"""The intelligence-chat graph must declare every key its nodes hand on.

LangGraph silently drops any key a node returns that is not in the graph's
state schema. That is how chat citations were lost: ``explain_why_node``
produced them, ``ChatState`` did not declare them, and every reply went out
with none. These tests fail the moment a node writes or reads an undeclared key.
"""

import ast
from pathlib import Path
from typing import Any, Dict

from langgraph.graph import END, StateGraph

from app.langgraph.chat_state import ChatState

ROOT = Path(__file__).resolve().parent.parent
_SOURCES = [ROOT / "app" / "langgraph" / "chat_nodes.py", ROOT / "app" / "langgraph" / "intelligence_chat_agent.py"]
_STATE_NAMES = {"state", "final_state", "result"}


def _state_keys():
    written, read = set(), set()
    for path in _SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                if any(k is None for k in node.value.keys):  # a {**state, ...} node result
                    written |= {k.value for k in node.value.keys
                                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in _STATE_NAMES and node.args
                  and isinstance(node.args[0], ast.Constant)):
                read.add(node.args[0].value)
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                  and node.value.id in _STATE_NAMES and isinstance(node.slice, ast.Constant)):
                read.add(node.slice.value)
    return written, read


def test_every_key_a_node_returns_is_declared():
    written, _ = _state_keys()
    assert written, "parser found no node results; the test is not looking at the graph"
    assert not written - set(ChatState.__annotations__)


def test_every_key_read_from_state_is_declared():
    _, read = _state_keys()
    assert not read - set(ChatState.__annotations__)


def test_citations_survive_a_graph_hop():
    """Behavioural check of the mechanism itself, on the real schema."""

    def produce(state: Dict[str, Any]) -> Dict[str, Any]:
        return {**state, "evidence_index": {"E1": {"label": "E1"}},
                "citations": [{"n": 1}], "citation_report": {"unsupported": 0}}

    def consume(state: Dict[str, Any]) -> Dict[str, Any]:
        return {**state, "response_text": f"{len(state.get('citations') or [])} citation(s)"}

    graph = StateGraph(ChatState)
    graph.add_node("produce", produce)
    graph.add_node("consume", consume)
    graph.set_entry_point("produce")
    graph.add_edge("produce", "consume")
    graph.add_edge("consume", END)
    out = graph.compile().invoke({"user_message": "q"})
    assert out["citations"] == [{"n": 1}]
    assert out["evidence_index"] == {"E1": {"label": "E1"}}
    assert out["response_text"] == "1 citation(s)"
