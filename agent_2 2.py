"""LangGraph workflow: parse input → filter forbidden words (nested JSON) → serialize output."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, TypedDict

import httpx
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
_INPUT_PAYLOAD: dict[str, Any] = {"requestId":"047837af-8f9f-44e4-a72a-5e27095dbb0a","workspaceId":"69d7552642e9be0e706410fc","userId":"6996c47c5ece710aeed68c8b","trigger":"manual_export","contentHash":"sha256:fe62e2bea7f1b4c8ad3cea80ab4743c845a4a978c8f398ddb181a0a66a35f083","documentSource":{"caseSummary":"This case is about the unauthorized hacking of a social media account belonging to Shubam Malik, the son of Umesh Malik, who is a Member of the Legislative Assembly (MLA) from Budhana. According to Annexure 1, the incident was reported on May 24, 2020, alleging that the account was compromised to edit and circulate videos and posts maliciously. The primary legal issues involve cybercrime under the Information Technology Act, 2000, and criminal defamation aimed at tarnishing the political and social reputation of Umesh Malik. The complainant has requested a high-level investigation by senior police officials to identify the perpetrators and initiate strict legal action. The matter highlights a targeted digital attack intended to cause public and political harm through identity theft and content manipulation.","reportCaseSummary":"This matter is about the alleged hacking of a digital profile belonging to Shubam Malik, a student at Virginia University and son of Umesh Malik, an elected representative from Budhana. On May 24, 2020, a formal communication was sent to the Station House Officer of Civil Lines, Muzaffarnagar, detailing the unauthorized manipulation of social media content. The narrative indicates that specific videos and posts were edited and disseminated across digital platforms with the intent to harm the public and political reputation of Umesh Malik. Legal considerations include violations under the Information Technology Act and provisions related to forgery and defamation. The request emphasizes the need for a specialized inquiry by high-ranking authorities to ensure the apprehension of those responsible for the cyber-attack. This situation underscores the risks of digital impersonation and its impact on the social integrity of public figures.","qualityScoreExplanation":"The current case resolution score is 69%. The score reflects 5 critical information gap(s) noted in the annexures, 2 factual weakness(es), and 4 procedural or legal risk(s), which reduces readiness relative to a fully complete case file. The analysis still identifies 4 strength(s) that support the file.  Lawyer-provided updates resolved 1 missing information item(s), 0 weakness(es), and 0 risk(s).","reportQualityScoreExplanation":"The current case resolution score is 69%. The score reflects 5 critical information gap(s) noted in the annexures, 2 factual weakness(es), and 4 procedural or risk(s), which reduces readiness relative to a fully complete case file. The analysis still identifies 4 strength(s) that support the file. Lawyer-provided updates resolved 1 missing information item(s), 0 weakness(es), and 0 risk(s).","possibleNextSteps":["Verify if an FIR was registered based on the complaint in Annexure 1 and obtain a certified copy of the same under the Bharatiya Nagarik Suraksha Sanhita, 2023.","Engage a digital forensic expert to preserve the current state of the hacked account and annexure the edited content as evidence under the Bharatiya Sakshya Adhiniyam, 2023.","Draft and send legal notices to the relevant social media intermediaries to take down the defamatory content and provide user access logs for the period of the breach.","Submit a follow-up representation to the Superintendent of Police (Cyber Cell) Muzaffarnagar to expedite the technical investigation and identify the source of the viral posts.","Collect character evidence and testimonials to quantify the damage caused to the political and social reputation for a potential civil defamation suit under the Bharatiya Nyaya Sanhita, 2023."],"listOfDates":[{"id":"date_1775719777114_0.48885817221811356","date":"2020-05-24","title":"Complaint Filed","description":"Umesh Malik filed a complaint regarding hacking of son's social media account.","sourceDocId":"69d7552742e9be0e706410fe","sourceDocName":"annexure-1-Handwritten_Complaint.png","pageNumber":1,"isIncident":True,"eventType":"Filing","relevance":"High","_id":"69d7556042e9be0e70641142"}],"laws":[{"id":"law_1","act":"Information Technology Act, 2000","section":"Section 66","description":"Applicable to the hacking of Shubham Malik's social media account as described in the document snippet where the account was compromised to edit and viralize content.","relevance":"High"},{"id":"law_2","act":"Information Technology Act, 2000","section":"Section 66C","description":"Relates to identity theft; the perpetrators used the victim's unique identification features (social media account) to post edited videos, as stated in the complaint.","relevance":"High"},{"id":"law_3","act":"Bharatiya Nyaya Sanhita, 2023","section":"Section 356","description":"Addresses defamation; the document explicitly states that the edited videos were viralized to tarnish the political and social image of Umesh Malik.","relevance":"High"},{"id":"law_4","act":"Bharatiya Nyaya Sanhita, 2023","section":"Section 336(4)","description":"Relates to forgery for the purpose of harming reputation, corresponding to the 'wrongly edited' videos used to damage the petitioner's public standing.","relevance":"Medium"},{"id":"law_5","act":"Information Technology Act, 2000","section":"Section 66D","description":"Cheating by personation using computer resources, applicable since the perpetrators used the son's account to spread misinformation and damage the family's reputation.","relevance":"Medium"}],"missingInfo":["Technical logs or login alerts indicating unauthorized access","Direct statement or affidavit from Shubam Malik (the victim)","Copies or screenshots of the edited videos and posts mentioned","Details of any previous communication with the social media intermediary","Identity or handles of the accounts actively circulating the content"],"strengths":[{"point":"The complaint is filed by a sitting MLA, which necessitates immediate administrative attention and prioritizes the registration of an FIR","laws":["BNSS Section 173"]},{"point":"Clear factual allegation of unauthorized access to a computer resource and identity theft","laws":["Information Technology Act 2000 Section 66","Information Technology Act 2000 Section 66C"]},{"point":"The annexure explicitly identifies the intent to cause reputational harm through the circulation of manipulated media","laws":["BNS Section 356"]},{"point":"The prompt reporting of the incident on 24-05-2020 demonstrates due diligence in seeking a remedy","laws":["BNSS"]}],"weaknesses":["The primary annexure is a third-party complaint (father) rather than a direct statement from the victim","Lack of primary digital evidence or metadata attached to the initial complaint to substantiate the hacking claim"]},"timestamp":"2026-04-09T07:35:48.333Z"}
 

INPUT_RAW = json.dumps(_INPUT_PAYLOAD, ensure_ascii=False, indent=2)

SYSTEM = """You are a strict text processor.

Your task is to ONLY process specific forbidden words from the input text.

Forbidden words (whole words only; case-sensitive; never match inside longer words, e.g. keep "clientele" unchanged):
["Petitioner", "petitioner", "Client", "client", "Case", "case", "Case's", "case's", "Respondent", "respondent", "Legal", "legal"]

Replacement rules — use the replacement that corresponds to the exact spelling you matched:

"Petitioner" → "Individual"
"petitioner" → "individual"
"Respondent" → "Individual"
"respondent" → "individual"
"Case" → "Matter"
"case" → "matter"
"Case's" → "Matter's"
"case's" → "matter's"
"Client" → "Individual"
"client" → "individual"
"Legal" → (remove word only)
"legal" → (remove word only)

Mandatory casing for replacements:
- If the matched forbidden word is "Petitioner", "Client", "Respondent", "Case", or "Case's", you MUST output "Individual", "Individual", "Individual", "Matter", or "Matter's" respectively — never use the lowercase forms for those matches.
- If the matched word is "petitioner", "client", "respondent", "case", or "case's", you MUST output "individual", "individual", "individual", "matter", or "matter's" respectively — unless the sentence-initial rule below requires capitalizing the first letter of that word.

Sentence starts after replacement or removal (English):
- After all forbidden-word edits, if a word is the very first word of the entire text, OR the first word after a sentence boundary (. ! ? followed by space or newline), capitalize its first letter when normal English prose would (so you do not leave a sentence starting with a lowercase letter because a forbidden word was removed or replaced).
- Examples: removing leading "Legal " so the next word begins the sentence → capitalize that word. A leading "case " that becomes "matter " at text start → use "Matter " for that token only at that position.
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


def _gemini_invoke_retries() -> int:
    return max(1, int(os.environ.get("GEMINI_INVOKE_RETRIES", "4")))


def _gemini_invoke_backoff_sec() -> float:
    return max(0.5, float(os.environ.get("GEMINI_INVOKE_BACKOFF_SEC", "1.5")))


def _invoke_llm_with_retries(llm: ChatGoogleGenerativeAI, messages: list) -> Any:
    """Retry llm.invoke on HTTP transport failures (e.g. connection reset, timeouts)."""
    retries = _gemini_invoke_retries()
    backoff = _gemini_invoke_backoff_sec()
    last: BaseException | None = None
    for attempt in range(retries):
        try:
            return llm.invoke(messages)
        except httpx.RequestError as e:
            last = e
            if attempt + 1 >= retries:
                break
            delay = backoff * (2**attempt)
            print(
                f"Gemini API transport error ({type(e).__name__}: {e}); "
                f"retry {attempt + 2}/{retries} in {delay:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    assert last is not None
    msg = (
        "Could not reach the Gemini API after retries (network/TLS). "
        "Check outbound HTTPS, VPN/proxy/firewall, and HTTP(S)_PROXY if set. "
        "Tune GEMINI_INVOKE_RETRIES and GEMINI_INVOKE_BACKOFF_SEC."
    )
    raise RuntimeError(msg) from last


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
    msg = _invoke_llm_with_retries(
        llm,
        [SystemMessage(content=SYSTEM), HumanMessage(content=text)],
    )
    out = _ai_output_text(msg)
    print("Answer came from LLM.", flush=True)
    return {"output": out}


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
