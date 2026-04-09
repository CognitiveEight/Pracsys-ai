"""LangGraph workflow: parse input -> filter forbidden words (nested JSON) -> serialize output."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from app.core.config import settings

# Must match SYSTEM "Forbidden words" list (case-sensitive fast path = LLM path).
FORBIDDEN = (
    "Petitioner",
    "petitioner",
    "Client",
    "client",
    "Case",
    "case",
    "Respondent",
    "respondent",
    "Legal",
    "legal",
    "Case's",
    "case's",
)

SYSTEM = """You are a strict text processor.

Your task is to ONLY process specific forbidden words from the input text.

Forbidden words:
["Petitioner", "petitioner", "Client", "client", "Case", "case", "Case's", "case's", "Respondent", "respondent", "Legal", "legal"]

Replacement rules:

"Petitioner" -> "Individual"
"petitioner" -> "individual"
"Respondent" -> "Individual"
"respondent" -> "individual"
"Case" -> "Matter"
"case" -> "matter"
"case's" -> "matter's"
"Client" -> "Individual"
"client" -> "individual"
"Legal" -> ""
"legal" -> ""

Processing rules:

Replace ONLY exact matches of the forbidden words listed above.
For forbidden words without a defined replacement, remove them completely.
Do NOT modify any other words, punctuation, spacing, or sentence structure.
Do NOT fix grammar or improve readability beyond the defined replacements.
Do NOT change capitalization of any remaining text.
Do NOT remove partial matches (e.g., "clientele" must remain unchanged).
Match words exactly as written (case-sensitive).

Output requirements:

The output must be IDENTICAL to the input except for the replacements/removals defined above.
Do not add or remove anything else.
Do not explain anything.
Return only the processed text.

If no forbidden words are present, return the input exactly as is."""

_FORBIDDEN_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(FORBIDDEN, key=len, reverse=True))
    + r")\b",
)

_DIRECT_REPLACEMENTS = {
    "Petitioner": "Individual",
    "petitioner": "individual",
    "Respondent": "Individual",
    "respondent": "individual",
    "Case's": "Matter's",
    "case's": "matter's",
    "Case": "Matter",
    "case": "matter",
    "Client": "Individual",
    "client": "individual",
    "Legal": "",
    "legal": "",
}


class TextFilterState(TypedDict):
    text: str
    output: str


def has_forbidden(text: str) -> bool:
    return _FORBIDDEN_RE.search(text) is not None


def _ai_output_text(msg) -> str:
    body = getattr(msg, "text", None) or ""
    if body:
        return body
    raw = getattr(msg, "content", None)
    if isinstance(raw, list):
        return "".join(
            (b.get("text") or "")
            for b in raw
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return raw if isinstance(raw, str) else ""


def _direct_filter(text: str) -> str:
    """Deterministic fallback when GEMINI_MODEL is unavailable."""
    pattern = re.compile(
        r"\b(?:"
        + "|".join(re.escape(k) for k in sorted(_DIRECT_REPLACEMENTS, key=len, reverse=True))
        + r")\b"
    )
    return pattern.sub(lambda m: _DIRECT_REPLACEMENTS.get(m.group(0), m.group(0)), text)


def node_text_filter(state: TextFilterState) -> dict[str, str]:
    text = state["text"]
    if not has_forbidden(text):
        return {"output": text}

    model = os.environ.get("GEMINI_MODEL") or settings.gemini_model
    if not model:
        return {"output": _direct_filter(text)}

    m = model.lower()
    kwargs: dict = {"model": model, "timeout": 120}
    if "gemini-3" in m:
        kwargs["thinking_level"] = "minimal"
        kwargs["temperature"] = 1.0
    else:
        kwargs["temperature"] = 0

    try:
        llm = ChatGoogleGenerativeAI(**kwargs)
        msg = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=text)])
        out = _ai_output_text(msg)
        if not isinstance(out, str) or not out:
            return {"output": _direct_filter(text)}
        # Hard safety: enforce deterministic forbidden-word replacement
        # even when Gemini output drifts from instruction.
        return {"output": _direct_filter(out)}
    except Exception:
        return {"output": _direct_filter(text)}


def build_text_filter_graph():
    g = StateGraph(TextFilterState)
    g.add_node("filter", node_text_filter)
    g.add_edge(START, "filter")
    g.add_edge("filter", END)
    return g.compile()


text_filter_graph = build_text_filter_graph()


def invoke_text_filter(text: str) -> str:
    return text_filter_graph.invoke({"text": text, "output": ""})["output"]


def filter_structure(x: Any) -> Any:
    if isinstance(x, str):
        return invoke_text_filter(x)
    if isinstance(x, dict):
        return {k: filter_structure(v) for k, v in x.items()}
    if isinstance(x, list):
        return [filter_structure(i) for i in x]
    return x


class WorkflowState(TypedDict, total=False):
    raw_input: str
    data: Any
    is_json: bool
    json_indent: int | None
    result: Any
    final_text: str


def sanitize_json_newlines_in_strings(text: str) -> str:
    """Escape literal newlines inside JSON double-quoted strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False

    while i < n:
        ch = text[i]
        if escape:
            out.append(ch)
            escape = False
            i += 1
            continue
        if in_string:
            if ch == "\\":
                out.append(ch)
                escape = True
                i += 1
                continue
            if ch == '"':
                bs = 0
                j = i - 1
                while j >= 0 and text[j] == "\\":
                    bs += 1
                    j -= 1
                if bs % 2 == 0:
                    in_string = False
                out.append(ch)
                i += 1
                continue
            if ch == "\r":
                if i + 1 < n and text[i + 1] == "\n":
                    i += 2
                else:
                    i += 1
                out.append("\\n")
                continue
            if ch == "\n":
                i += 1
                out.append("\\n")
                continue
            out.append(ch)
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1

    return "".join(out)


def node_parse(state: WorkflowState) -> dict[str, Any]:
    raw = state["raw_input"]
    s = raw.strip()
    if not s or s[0] not in "{[":
        return {"data": raw, "is_json": False, "json_indent": None}

    fixed = sanitize_json_newlines_in_strings(s)
    return {
        "data": json.loads(fixed),
        "is_json": True,
        "json_indent": 2 if "\n" in raw else None,
    }


def node_deep_filter(state: WorkflowState) -> dict[str, Any]:
    data = state["data"]
    if isinstance(data, (dict, list)):
        return {"result": filter_structure(data)}
    if isinstance(data, str):
        return {"result": invoke_text_filter(data)}
    return {"result": data}


def node_format(state: WorkflowState) -> dict[str, str]:
    if state.get("is_json"):
        body = json.dumps(
            state["result"],
            ensure_ascii=False,
            indent=state.get("json_indent"),
        )
    else:
        r = state["result"]
        body = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    return {"final_text": body}


def build_workflow():
    g = StateGraph(WorkflowState)
    g.add_node("parse", node_parse)
    g.add_node("deep_filter", node_deep_filter)
    g.add_node("format", node_format)
    g.add_edge(START, "parse")
    g.add_edge("parse", "deep_filter")
    g.add_edge("deep_filter", "format")
    g.add_edge("format", END)
    return g.compile()


workflow = build_workflow()


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def process_analysis_docx_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform analysis_docx_generate_request into analysis_docx_generate_response
    using the same filtering logic defined in this file.
    """
    request_id = payload.get("requestId")
    workspace_id = payload.get("workspaceId")
    source = payload.get("documentSource")

    if not request_id or not workspace_id or not isinstance(source, dict):
        return {
            "requestId": request_id,
            "workspaceId": workspace_id,
            "success": False,
            "documentPayload": None,
            "error": "Invalid payload: requestId, workspaceId, and documentSource are required",
            "timestamp": _now_iso(),
        }

    # Process the full source so every provided key is normalized consistently.
    document_payload = filter_structure(source)

    # Normalize summary keys expected by DOCX pipeline.
    report_case_summary = source.get("reportCaseSummary")
    if report_case_summary is None:
        report_case_summary = source.get("caseSummary", "")
    document_payload["reportCaseSummary"] = filter_structure(str(report_case_summary))

    report_quality = source.get("reportQualityScoreExplanation")
    if report_quality is None:
        report_quality = source.get("qualityScoreExplanation", "")
    document_payload["reportQualityScoreExplanation"] = filter_structure(str(report_quality))

    # Ensure new backend key is always present for DOCX consumers.
    if "risks" not in document_payload:
        document_payload["risks"] = []
    document_payload["risks"] = filter_structure(document_payload.get("risks") or [])

    return {
        "requestId": request_id,
        "workspaceId": workspace_id,
        "success": True,
        "documentPayload": document_payload,
        "error": None,
        "timestamp": _now_iso(),
    }


if __name__ == "__main__":
    raw = input().strip()
    incoming = json.loads(raw)
    print(json.dumps(process_analysis_docx_request(incoming), ensure_ascii=False))
