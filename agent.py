"""LangGraph workflow: parse input -> filter forbidden words (nested JSON) -> serialize output."""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Dict, TypedDict

import google.generativeai as genai
from langgraph.graph import END, START, StateGraph
from app.ai.gemini_client import gemini_client
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

Forbidden words (whole words only; case-sensitive; never match inside longer words, e.g. keep "clientele" unchanged):
["Petitioner", "petitioner", "Client", "client", "Case", "case", "Case's", "case's", "Respondent", "respondent", "Legal", "legal"]

Replacement rules - use the replacement that corresponds to the exact spelling you matched:

"Petitioner" -> "Individual"
"petitioner" -> "individual"
"Respondent" -> "Individual"
"respondent" -> "individual"
"Case" -> "Matter"
"case" -> "matter"
"Case's" -> "Matter's"
"case's" -> "matter's"
"Client" -> "Individual"
"client" -> "individual"
"Legal" -> (remove word only)
"legal" -> (remove word only)

Mandatory casing for replacements:
- If the matched forbidden word is "Petitioner", "Client", "Respondent", "Case", or "Case's", you MUST output "Individual", "Individual", "Individual", "Matter", or "Matter's" respectively - never use the lowercase forms for those matches.
- If the matched word is "petitioner", "client", "respondent", "case", or "case's", you MUST output "individual", "individual", "individual", "matter", or "matter's" respectively - unless the sentence-initial rule below requires capitalizing the first letter of that word.

Sentence starts after replacement or removal (English):
- After all forbidden-word edits, if a word is the very first word of the entire text, OR the first word after a sentence boundary (. ! ? followed by space or newline), capitalize its first letter when normal English prose would (so you do not leave a sentence starting with a lowercase letter because a forbidden word was removed or replaced).
- Examples: removing leading "Legal " so the next word begins the sentence -> capitalize that word. A leading "case " that becomes "matter " at text start -> use "Matter " for that token only at that position.
- Apply this only for sentence starts; do not randomly retitle-case words mid-sentence.

Spacing when removing "Legal"/"legal":
- Do not leave double spaces; use a single space between the surrounding words.

Do not add, remove, or rephrase anything except what these rules require for forbidden words and the sentence-start/spacing fixes above.
Do not explain. Return only the processed text.

If no forbidden words are present, return the input exactly as is."""

_FORBIDDEN_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(FORBIDDEN, key=len, reverse=True))
    + r")\b",
)

_DIRECT_REPLACEMENTS = {
    "Petitioner": "Individual",
    "petitioner": "individual",
    "Petitoner": "Individual",
    "petitoner": "individual",
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

_LLM_FILTER_DISABLED = False
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=2)


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
    """Deterministic fallback with sentence-safe casing/spacing fixes."""
    marker = "__LEGAL_SENTENCE_START__"

    def _is_sentence_start(s: str, idx: int) -> bool:
        j = idx - 1
        while j >= 0 and s[j].isspace():
            j -= 1
        if j < 0:
            return True
        return s[j] in ".!?\n"

    def _previous_word(s: str, idx: int) -> str:
        prefix = s[:idx]
        m = re.search(r"([A-Za-z]+)\s*$", prefix)
        return m.group(1) if m else ""

    def _replace(m: re.Match) -> str:
        tok = m.group(0)
        rep = _DIRECT_REPLACEMENTS.get(tok, tok)
        if rep:
            prev = _previous_word(text, m.start())
            # Natural article+noun style: "The Petitioner" -> "The individual".
            if prev.lower() == "the" and rep in ("Individual", "Matter", "Matter's"):
                rep = rep[0].lower() + rep[1:]
            # Keep normal grammar when lowercase token starts a sentence.
            if rep and rep[0].islower() and _is_sentence_start(text, m.start()):
                rep = rep[0].upper() + rep[1:]
            return rep

        # Removing Legal/legal: if it starts a sentence, keep natural opener.
        if _is_sentence_start(text, m.start()):
            return marker
        return ""

    pattern = re.compile(
        r"\b(?:"
        + "|".join(re.escape(k) for k in sorted(_DIRECT_REPLACEMENTS, key=len, reverse=True))
        + r")\b"
    )
    out = pattern.sub(_replace, text)

    # "Legal issues ..." -> "The issues ..."
    out = re.sub(rf"{marker}\s+([A-Za-z])", r"The \1", out)
    out = out.replace(marker, "")

    # Spacing cleanup around removals.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([,.;:!?])", r"\1", out)
    out = re.sub(r"\n +", "\n", out)
    return out


def node_text_filter(state: TextFilterState) -> dict[str, str]:
    global _LLM_FILTER_DISABLED
    text = state["text"]
    if not has_forbidden(text):
        return {"output": text}

    if _LLM_FILTER_DISABLED:
        return {"output": _direct_filter(text)}

    try:
        # Use same Gemini wiring as app/ai/gemini_client.py
        gemini_client.initialize()
        if not gemini_client.model:
            return {"output": _direct_filter(text)}

        model_name = os.environ.get("GEMINI_MODEL") or settings.gemini_model
        if model_name and model_name != gemini_client.model_name:
            gemini_client.model_name = model_name
            gemini_client.model = genai.GenerativeModel(model_name)

        full_prompt = f"{SYSTEM}\n\n{text}"
        future = _LLM_EXECUTOR.submit(
            lambda: gemini_client.model.generate_content(
                full_prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.0, max_output_tokens=8000
                ),
            )
        )
        try:
            response = future.result(timeout=6)
        except FuturesTimeoutError:
            _LLM_FILTER_DISABLED = True
            return {"output": _direct_filter(text)}
        out = getattr(response, "text", "") or ""
        if not isinstance(out, str) or not out:
            return {"output": _direct_filter(text)}
        # Hard safety: enforce deterministic forbidden-word replacement
        # even when Gemini output drifts from instruction.
        return {"output": _direct_filter(out)}
    except Exception as e:
        # Staging/prod often uses ADC tokens without GenAI scopes.
        # Disable LLM path for this process after first scope/auth failure
        # to avoid repeated retry noise and latency on every string.
        emsg = str(e)
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in emsg or "PermissionDenied" in emsg:
            _LLM_FILTER_DISABLED = True
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


# ---------------------------------------------------------------------------
# Law-citation stripping (analysis_docx_generate_request post-processing)
# ---------------------------------------------------------------------------
# When the backend asks for a DOCX export, narrative fields (caseSummary,
# strategicObjective, etc.) often contain inline law citations like
# "In accordance with Section 27 of the Protection of Women from Domestic
# Violence Act, 2005,". The exported report should keep those citations only
# inside the dedicated "Applicable Laws" section. This pass surgically removes
# law citations from every other field, preserving the rest of the wording
# verbatim (no rephrasing, no recapitalisation).

LAW_STRIP_SYSTEM = """You are a strict text editor.

Your ONLY job is to REMOVE law citations from the text. Do NOT rephrase, restructure, summarize, paraphrase, or rewrite any sentence. Do NOT change the capitalization of words that survive.

REMOVE these patterns:
1. Statute / section citations (with or without year):
   - "Section X of the <Act Name>, <Year>"
   - "Section X of the <Act Name>"
   - "Section X of <Act Name>"
   - "under the <Act Name>, <Year>"
   - "under <Act Name>"
   - "<Act Name>, <Year>" used as a citation (e.g. "Bharatiya Nyaya Sanhita, 2023")
   - "Section X" / "Sections X and Y" used as a legal citation
   - Acronym citations: "BNS", "BNSS", "BSA", "IPC", "CrPC", "CPC", "IT Act"
   - Named acts: "Bharatiya Nyaya Sanhita", "Bharatiya Nagarik Suraksha Sanhita", "Bharatiya Sakshya Adhiniyam", "Indian Penal Code", "Code of Criminal Procedure", "Information Technology Act", "Protection of Women from Domestic Violence Act", "Indian Contract Act", "Immoral Traffic (Prevention) Act", etc.
2. Connective introducer phrases that lead INTO a citation being removed — strip them with the citation:
   - "In accordance with …,"
   - "under …,"
   - "as per …,"
   - "pursuant to …,"
   - "according to …,"
3. Trailing qualifiers that hang off the removed citation: ", where permissible", ", as applicable", ", subject to law", etc. — remove with the citation.

STRICT RULES:
- Preserve the original wording and capitalization of every surviving word. If removing a prefix leaves a sentence starting with a lowercase letter, KEEP IT LOWERCASE — do not retitle-case.
- KEEP document references like "Annexure 1", "Annexure 2", and similar — they are not laws.
- KEEP party names, place names, dates that are not part of a citation.
- Collapse double spaces left behind into a single space. Remove leftover ", ," sequences (turn into ","). Preserve sentence-ending punctuation (".", "?", "!").
- If a sentence becomes empty after removal, drop the sentence and its trailing space.
- Return ONLY the cleaned text. No commentary, no quotes, no explanation.
- If no law citations are present, return the input EXACTLY unchanged."""


# Quick pre-filter so we don't pay for an LLM call on text that obviously has
# no law citations. Matches the broad shape of an Indian-law reference.
_LAW_KEYWORD_RE = re.compile(
    r"(?:\bSection\s+\d+|\bSections\s+\d+|"
    r"Act,?\s*\d{4}|Adhiniyam|Sanhita|"
    r"Bharatiya|Nyaya|Suraksha|Sakshya|"
    r"\bIPC\b|\bCrPC\b|\bCPC\b|\bBNS\b|\bBNSS\b|\bBSA\b|"
    r"Indian Penal Code|Code of Criminal Procedure|"
    r"Code of Civil Procedure|Evidence Act|IT Act|"
    r"Information Technology Act|Protection of Women|"
    r"Domestic Violence Act|Indian Contract Act|"
    r"Immoral Traffic|"
    r"In accordance with Section|under the.*Act|pursuant to)",
    re.IGNORECASE,
)


def has_potential_law_citation(text: str) -> bool:
    return bool(text) and _LAW_KEYWORD_RE.search(text) is not None


def _law_strip_via_llm(text: str) -> str:
    """Surgical removal of law citations via Gemini. Returns input unchanged on any failure."""
    global _LLM_FILTER_DISABLED
    if not isinstance(text, str) or not text.strip():
        return text
    if not has_potential_law_citation(text):
        return text
    if _LLM_FILTER_DISABLED:
        return text

    try:
        gemini_client.initialize()
        if not gemini_client.model:
            return text

        model_name = os.environ.get("GEMINI_MODEL") or settings.gemini_model
        if model_name and model_name != gemini_client.model_name:
            gemini_client.model_name = model_name
            gemini_client.model = genai.GenerativeModel(model_name)

        full_prompt = f"{LAW_STRIP_SYSTEM}\n\nINPUT:\n{text}\n\nOUTPUT:"
        future = _LLM_EXECUTOR.submit(
            lambda: gemini_client.model.generate_content(
                full_prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.0, max_output_tokens=8000
                ),
            )
        )
        try:
            response = future.result(timeout=15)
        except FuturesTimeoutError:
            return text
        out = getattr(response, "text", "") or ""
        if not isinstance(out, str) or not out.strip():
            return text
        return out.strip()
    except Exception as e:
        emsg = str(e)
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in emsg or "PermissionDenied" in emsg:
            _LLM_FILTER_DISABLED = True
        return text


def _is_applicable_laws_key(key: Any) -> bool:
    """True when the dict key names the 'Applicable Laws' section (any casing/separator)."""
    if not isinstance(key, str):
        return False
    k = key.lower()
    return "applicable" in k and "law" in k


def strip_laws_in_structure(data: Any) -> Any:
    """Recursively strip law citations from every string field, except inside
    any subtree keyed by an 'applicable laws' key (which stays untouched)."""
    if isinstance(data, str):
        return _law_strip_via_llm(data)
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if _is_applicable_laws_key(k):
                out[k] = v
            else:
                out[k] = strip_laws_in_structure(v)
        return out
    if isinstance(data, list):
        return [strip_laws_in_structure(i) for i in data]
    return data


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

    # Strip law citations from every narrative field; keep them ONLY inside the
    # dedicated "Applicable Laws" subtree (which is skipped by key).
    document_payload = strip_laws_in_structure(document_payload)

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
