"""LangGraph workflow: parse input → filter forbidden words (nested JSON) → serialize output."""
from __future__ import annotations

import json
import os
import re
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

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

# Option A: edit _INPUT_PAYLOAD, then INPUT_RAW is built below.
# Option B: set INPUT_RAW = r''' ... '''.strip() with pasted JSON (line breaks inside strings OK).
_INPUT_PAYLOAD: dict[str, Any] = {"requestId":"201bd169-8b90-4c86-ad6c-c4f966097bd4","workspaceId":"69d4dad1a7f57aed9e1a4827","userId":"6995927e3123f80f3756c6f2","trigger":"manual_export","contentHash":"sha256:4ee6434cede9abd69bfb7702f96a3031ad6af87829b67ca2a2c4c52cc606033a","documentSource":{"caseSummary":"This case is about the unauthorized hacking of a social media account belonging to Shubham Malik, the son of Umesh Malik, who is a Member of the Legislative Assembly (MLA) from Budhana. The petitioner alleges that after the account was compromised, videos and posts were edited in a malicious manner and circulated on social media platforms. These actions were reportedly intended to tarnish the political and social reputation of the petitioner through the use of his son's digital identity case . The matter involves potential violations of cyber laws and criminal provisions related to forgery and defamation. The petitioner has requested a high-level investigation by senior police officials to identify and prosecute the individuals responsible for this digital attack.","qualityScoreExplanation":"The score is impacted by the lack of specific digital identifiers (URLs/IP logs), the unidentified status of the perpetrators, and the procedural complexity of the victim being located in a foreign jurisdiction. Strengths include the prompt reporting legal and clear intent to cause reputational harm. User rejected 1 item from missingInfo: \"Contact details or statement from the primary victim (Shubham Malik)\". Score adjusted by +4 (from 68% to 72%). The primary victim's statement case  is a critical evidentiary component, and its exclusion as a missing requirement significantly reduces the case's procedural complexity. User rejected 1 item from missingInfo: \"Current status of the investigation by the high-level officer\". Score adjusted by +2 (from 72% to 74%). client The status of a high-level investigation is a procedural detail that does not directly impact the core evidentiary requirements of the case.","missingInfo":["Specific social media platform name and account handle","URLs or links to the edited viral content","IP logs or login alerts indicating unauthorized access","Copies of the edited videos or posts mentioned in the complaint"],"strengths":[{"point":"The complaint clearly alleges unauthorized access and hacking of a digital account","laws":["Information Technology Act 2000 Section 66","BNS Section 308"]},{"point":"The annexure identifies a specific legal intent to tarnish the political and social reputation of a public figure through manipulated media","laws":["BNS Section 356","BNS Section 336"]},{"point":"The complaint was filed promptly following the discovery of the viral content, establishing a clear timeline of grievance","laws":["BNSS Section 173"]},{"point":"The allegation involves the forgery of electronic records by editing and viral distribution of content","laws":["BNS Section 336","BSA Section 63"]}],"weaknesses":["The client identity of the perpetrators is currently unknown, hindering immediate prosecution","The primary victim is located in Virginia, USA, which may complicate the collection of direct testimony and evidence","Lack of technical metadata or digital footprints attached to the primary complaint"]},"timestamp":"2026-04-08T11:42:00.308Z"}

INPUT_RAW = json.dumps(_INPUT_PAYLOAD, ensure_ascii=False, indent=2)

SYSTEM = """You are a strict text processor.
 
Your task is to ONLY process specific forbidden words from the input text.
 
Forbidden words:
["Petitioner", "petitioner", "Client", "client", "Case", "case", "Case's", "case's", "Respondent", "respondent", "Legal", "legal"]
 
Replacement rules:
 
"Petitioner" → "Individual"
"petitioner" → "individual"
"Respondent" → "Individual"
"respondent" → "individual"
"Case" → "Matter"
"case" → "matter"
"case's" → "matter's"
"Client" → "Individual"
"client" → "individual"
"Legal" → ""
"legal" → ""
 
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


# --- Single-string LLM subgraph -------------------------------------------------


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


def node_text_filter(state: TextFilterState) -> dict[str, str]:
    text = state["text"]
    if not has_forbidden(text):
        return {"output": text}

    model = os.environ["GEMINI_MODEL"]
    m = model.lower()
    kwargs: dict = {"model": model, "timeout": 120}
    if "gemini-3" in m:
        kwargs["thinking_level"] = "minimal"
        kwargs["temperature"] = 1.0
    else:
        kwargs["temperature"] = 0

    llm = ChatGoogleGenerativeAI(**kwargs)
    msg = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=text)]
    )
    return {"output": _ai_output_text(msg)}


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


# --- End-to-end workflow (parse → deep filter → format) -------------------------


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


def load_dotenv_file(path: str | None = None) -> None:
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val


if __name__ == "__main__":
    load_dotenv_file()
    final = workflow.invoke({"raw_input": INPUT_RAW})["final_text"]
    print(final)
