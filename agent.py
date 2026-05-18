"""LangGraph workflow: parse input -> filter forbidden words (nested JSON) -> serialize output."""
from __future__ import annotations

import json
import os
import re
import time
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
_LAW_LLM_DISABLED = False
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# Per-phase wall-clock budgets for parallel Gemini fan-out. Each individual
# call still has its own inner timeout (6 s / 20 s); these caps just guarantee
# the overall request emits a response even when many calls stall.
_FORBIDDEN_FILTER_BUDGET_S = 25.0
_LAW_STRIP_BUDGET_S = 45.0


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


# Multi-word legal terms that contain a forbidden word (Legal / Case / …)
# but must survive intact. Order matters — longer first so "Medico-Legal
# Case" is protected before bare "Medico-Legal".
_PROTECTED_PHRASES_RE = re.compile(
    r"Medico[\s-]?[Ll]egal\s+[Cc]ase|Medico[\s-]?[Ll]egal",
)
_PROTECT_SENTINEL_FMT = "PROTECT{0}"
_PROTECT_TOKEN_RE = re.compile(r"PROTECT(\d+)")


def _protect_phrases(text: str):
    """Replace protected phrases with sentinel tokens before filtering.

    Returns (modified_text, mapping). The sentinel uses a Private-Use-Area
    character so neither the forbidden-words regex nor Gemini touches it."""
    if not isinstance(text, str) or not text:
        return text, {}
    mapping = {}

    def _swap(match: re.Match) -> str:
        token = _PROTECT_SENTINEL_FMT.format(len(mapping))
        mapping[token] = match.group(0)
        return token

    new_text = _PROTECTED_PHRASES_RE.sub(_swap, text)
    return new_text, mapping


def _restore_phrases(text: str, mapping: Dict[str, str]) -> str:
    """Swap sentinel tokens back to their original protected phrases."""
    if not mapping or not isinstance(text, str):
        return text
    for token, original in mapping.items():
        text = text.replace(token, original)
    # Belt-and-braces: any leftover sentinel (rare) drops out cleanly.
    return _PROTECT_TOKEN_RE.sub("", text)


def invoke_text_filter(text: str) -> str:
    protected, mapping = _protect_phrases(text)
    out = text_filter_graph.invoke({"text": protected, "output": ""})["output"]
    return _restore_phrases(out, mapping)


def _collect_strings(
    node: Any,
    predicate,
    into: Dict[str, None],
    skip_key=None,
) -> None:
    """Walk a JSON-like tree, recording every unique string for which
    `predicate(s)` is true. `into` is used as an insertion-ordered set.

    If `skip_key` is provided, any dict entry whose key matches it is
    skipped entirely — the subtree under that key is not visited."""
    if isinstance(node, str):
        if node and predicate(node):
            into[node] = None
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if skip_key is not None and skip_key(k):
                continue
            _collect_strings(v, predicate, into, skip_key)
        return
    if isinstance(node, list):
        for i in node:
            _collect_strings(i, predicate, into, skip_key)


def _rebuild_with_mapping(
    node: Any,
    mapping: Dict[str, str],
    skip_key=None,
) -> Any:
    """Rebuild a JSON-like tree, substituting strings present in `mapping`.
    Any dict entry whose key matches `skip_key` is preserved verbatim."""
    if isinstance(node, str):
        return mapping.get(node, node)
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if skip_key is not None and skip_key(k):
                out[k] = v
            else:
                out[k] = _rebuild_with_mapping(v, mapping, skip_key)
        return out
    if isinstance(node, list):
        return [_rebuild_with_mapping(i, mapping, skip_key) for i in node]
    return node


def _resolve_futures(
    futures: Dict[str, "Any"],
    deadline: float,
    on_timeout: str = "original",
) -> Dict[str, str]:
    """Collect parallel futures with a single wall-clock deadline.

    `on_timeout="original"` means: if the future doesn't beat the deadline,
    fall back to the original string (the dict key). The inner per-call
    timeout already lives in each task; this is the outer cap.
    """
    out: Dict[str, str] = {}
    for original, fut in futures.items():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            result = fut.result(timeout=remaining) if remaining > 0 else None
        except FuturesTimeoutError:
            result = None
        except Exception:
            result = None
        if isinstance(result, str) and result.strip():
            out[original] = result
        elif on_timeout == "original":
            out[original] = original
    return out


def filter_structure(x: Any) -> Any:
    """Apply the forbidden-words filter to every string in a JSON-like tree.

    Uses a 3-phase parallel pattern: collect unique strings that need the
    filter, submit them all to the executor at once, then rebuild the tree
    with the results. Strings without forbidden words are left untouched
    (no Gemini call) — same fast-path as `node_text_filter`.
    """
    if isinstance(x, str):
        return invoke_text_filter(x) if has_forbidden(x) else x
    if not isinstance(x, (dict, list)):
        return x

    unique: Dict[str, None] = {}
    _collect_strings(x, has_forbidden, unique, skip_key=_is_applicable_laws_key)
    if not unique:
        return x

    deadline = time.monotonic() + _FORBIDDEN_FILTER_BUDGET_S
    futures = {s: _LLM_EXECUTOR.submit(invoke_text_filter, s) for s in unique}
    mapping = _resolve_futures(futures, deadline)
    return _rebuild_with_mapping(x, mapping, skip_key=_is_applicable_laws_key)


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

=== PRIMARY DIRECTIVE — apply this above every other rule below ===

Any phrase that functions as a law, act, statute, section, article, code, sanhita, adhiniyam, ordinance, rule, regulation, or constitutional citation — KNOWN OR UNKNOWN to you — MUST be REMOVED from the text. The act lists, connective lists, and worked examples further down are ILLUSTRATIONS, not an exhaustive enumeration. If a phrase looks like a legal reference (any of the shapes below, or anything similar), treat it as a citation and strip it — do not skip it just because the act name is unfamiliar:

- "Section N of the <Act Name>, <Year>" / "Section N of <Act Name>" / "Section N of the (said|aforesaid|same) Act/Code/Constitution"
- "Article N of <Act Name or Constitution>"
- "Rule N of the <Rules / Regulations>"
- "<Capitalised Name> Act, <Year>" / "<Capitalised Name> Act" used as a citation
- "<Capitalised Name> Adhiniyam / Sanhita / Code, <Year>"
- "the relevant / applicable / appropriate / various / specific / pertinent (sections | provisions | clauses | articles | chapters) of <Act>"
- Acronyms used as citations (BNS, BNSS, BSA, IPC, CrPC, CPC, IT Act, NI Act, PMLA, …) and any acronym that resolves to an act name in context
- "Order N of the <Code/Regulations>", "Schedule N of the <Act>", "Chapter N of the <Code>"

REMOVE SMARTLY — not just the citation, but the surrounding grammar that attaches to it:

1. Also remove the connective phrase that introduces the citation: "under", "pursuant to", "as per", "in accordance with", "according to", "as permitted under", "as contemplated under", "as defined in", "as set out in", "as enshrined in", "as recognised under", "under the provisions of", "by virtue of", "in terms of", etc.
2. When the citation is in the middle of a sentence, ALSO consume the comma that immediately follows it. Example: "under the X Act, 2005, will be filed" → "will be filed" (NEVER ", will be filed").
3. Remove trailing qualifiers that hang off the citation: ", where permissible", ", as applicable", ", subject to law", ", as the case may be".
4. Keep the rest of the sentence grammatical. Do NOT leave orphan commas, dangling "of", or half-finished prefixes like "Section 27(1)(a) of,". If the citation was the entire grammatical subject or object of a clause, you MAY gently adjust the minimum surrounding words so the sentence still reads naturally — but do NOT paraphrase content unrelated to the citation.
5. Preserve surviving words' original casing exactly. Lowercase "the initiation" stays lowercase. Preserve non-citation references verbatim: "Annexure 1", "MLC", "FIR", party names, place names ("Patna", "Delhi"), dates, and the standalone word "legal" when it is not part of a citation.

WHEN IN DOUBT, REMOVE THE CITATION. The rules below refine HOW to remove; this directive establishes WHAT to remove.

=== END PRIMARY DIRECTIVE ===

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
- If no law citations are present, return the input EXACTLY unchanged.

CRITICAL — when removing a citation in the middle of a sentence, ALSO consume the comma that immediately follows the citation. The pattern "<connective> <citation>, <rest>" must become "<rest>" — never ", <rest>".

CRITICAL — pronoun references like "Section N of the Act", "Section N of the said Act", "Article N of that Code" ARE citations. Remove them even when the act name is not repeated.

CRITICAL — a bare "Section N" / "Article N" that appears AFTER a connective ("under Section 18", "pursuant to Article 226") IS a citation and must be removed. A bare "Section N" used in a non-legal context ("Section A of this report") is NOT — keep it.

WORKED EXAMPLES — each input/output below shows the exact behavior required.

EXAMPLE 1 (sentence-leading connective + statute citation + comma):
INPUT:
In accordance with Section 27 of the Protection of Women from Domestic Violence Act, 2005, the initiation of legal proceedings in Patna is prioritized to ensure the convenience and protection of the wife and children.
OUTPUT:
the initiation of legal proceedings in Patna is prioritized to ensure the convenience and protection of the wife and children.

EXAMPLE 2 (mid-sentence connective with trailing qualifier):
INPUT:
The criminal implications of the hairline fracture documented in Annexure 1 are to be managed within the mediation framework to explore the possibility of compounding offences under the Bharatiya Nagarik Suraksha Sanhita, 2023, where permissible.
OUTPUT:
The criminal implications of the hairline fracture documented in Annexure 1 are to be managed within the mediation framework to explore the possibility of compounding offences.

EXAMPLE 3 (mid-sentence citation; trailing comma after the citation MUST disappear too):
INPUT:
An application under Section 12 of the Protection of Women from Domestic Violence Act, 2005, will be filed to obtain protection orders under Section 18 and residence orders under Section 19.
OUTPUT:
An application will be filed to obtain protection orders and residence orders.

EXAMPLE 4 (pronoun reference to a previously-named act):
INPUT:
The relocation of the sister to her parental home in Patna provides a significant procedural advantage for filing a petition under the Protection of Women from Domestic Violence Act, 2005. Section 27 of the Act permits the initiation of legal proceedings in the jurisdiction where the aggrieved person resides, ensuring a more accessible forum.
OUTPUT:
The relocation of the sister to her parental home in Patna provides a significant procedural advantage for filing a petition. The initiation of legal proceedings in the jurisdiction where the aggrieved person resides is permitted, ensuring a more accessible forum.

EXAMPLE 5 (section with sub-clauses + standalone act + acronym):
INPUT:
Jurisdiction will be invoked in Patna under Section 27(1)(a) of the Protection of Women from Domestic Violence Act, 2005, based on her permanent relocation. Application for a protection order under the BNSS 2023 will be prioritized.
OUTPUT:
Jurisdiction will be invoked in Patna based on her permanent relocation. Application for a protection order will be prioritized.

EXAMPLE 6 (vague "the relevant/applicable sections of <Act>" + sentence-ending citation):
INPUT:
Ensure the conversion of the written complaint into a formal First Information Report (FIR) under the relevant sections of the Bharatiya Nyaya Sanhita, 2023. Financial annexures are essential for quantifying maintenance under the Protection of Women from Domestic Violence Act, 2005.
OUTPUT:
Ensure the conversion of the written complaint into a formal First Information Report (FIR). Financial annexures are essential for quantifying maintenance.

General notes: "Annexure 1", place names ("Patna", "Delhi"), party words ("wife", "children", "husband"), and the standalone word "legal" (when not part of a citation) all stay verbatim. The rule applies to ANY Indian act, including ones not listed above — if a phrase looks like a statute citation ("Section N of <Capitalized Phrase> Act[, YYYY]", "<Capitalized Phrase> Act[, YYYY]", or "the <Capitalized Phrase> Adhiniyam/Sanhita[, YYYY]") treat it as one and remove it even if the act name is unfamiliar."""


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


def _gemini_law_strip_call(text: str, extra_instruction: str = "") -> str | None:
    """Single Gemini round-trip for law-citation stripping. Returns the cleaned
    text, or None on any hard failure (timeout, empty response, exception)."""
    global _LAW_LLM_DISABLED
    if _LAW_LLM_DISABLED:
        return None
    try:
        gemini_client.initialize()
        if not gemini_client.model:
            return None

        model_name = os.environ.get("GEMINI_MODEL") or settings.gemini_model
        if model_name and model_name != gemini_client.model_name:
            gemini_client.model_name = model_name
            gemini_client.model = genai.GenerativeModel(model_name)

        prompt_body = LAW_STRIP_SYSTEM
        if extra_instruction:
            prompt_body = f"{LAW_STRIP_SYSTEM}\n\n{extra_instruction}"
        full_prompt = f"{prompt_body}\n\nINPUT:\n{text}\n\nOUTPUT:"

        future = _LLM_EXECUTOR.submit(
            lambda: gemini_client.model.generate_content(
                full_prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.0, max_output_tokens=8000
                ),
            )
        )
        try:
            response = future.result(timeout=20)
        except FuturesTimeoutError:
            return None
        out = getattr(response, "text", "") or ""
        if not isinstance(out, str) or not out.strip():
            return None
        return out.strip()
    except Exception as e:
        emsg = str(e)
        if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in emsg or "PermissionDenied" in emsg:
            _LAW_LLM_DISABLED = True
        return None


# ---------------------------------------------------------------------------
# Regex safety net for known Indian acts.
# ---------------------------------------------------------------------------
# Runs AFTER Gemini. Gemini still handles unknown acts; this layer guarantees
# the well-known Indian acts are stripped even when the LLM silently misses
# them. Patterns are surgical — they remove only citation phrases, not the
# surrounding narrative.

_KNOWN_ACT_NAMES = (
    r"Bharatiya\s+Nagarik\s+Suraksha\s+Sanhita",
    r"Bharatiya\s+Nyaya\s+Sanhita",
    r"Bharatiya\s+Sakshya\s+Adhiniyam",
    r"Protection\s+of\s+Women\s+from\s+Domestic\s+Violence\s+Act",
    r"Domestic\s+Violence\s+Act",
    r"Indian\s+Penal\s+Code",
    r"Code\s+of\s+Criminal\s+Procedure",
    r"Code\s+of\s+Civil\s+Procedure",
    r"Indian\s+Evidence\s+Act",
    r"Information\s+Technology\s+Act",
    r"IT\s+Act",
    r"Hindu\s+Marriage\s+Act",
    r"Indian\s+Contract\s+Act",
    r"Immoral\s+Traffic\s*\(Prevention\)\s+Act",
    r"Negotiable\s+Instruments\s+Act",
    r"Right\s+to\s+Information\s+Act",
    r"Specific\s+Relief\s+Act",
    r"Constitution\s+of\s+India",
)
_KNOWN_ACT_ACRONYMS = (r"BNSS", r"BNS", r"BSA", r"IPC", r"CrPC", r"CPC")

_ACT_BODY = (
    r"(?:the\s+)?(?:"
    + "|".join(_KNOWN_ACT_NAMES + _KNOWN_ACT_ACRONYMS)
    + r")"
)
# "the X Act, 2005" or bare "BNSS 2023"
_ATOM = rf"{_ACT_BODY}(?:,\s*\d{{4}}|\s+\d{{4}})?"
# Section/Article number with optional letter and ANY number of (sub)(sub) groups.
_SECTION_NUM = (
    r"\d+[A-Z]?(?:\([^)]+\))*"
    r"(?:\s*(?:,|and|to)\s*\d+[A-Z]?(?:\([^)]+\))*)*"
)
_SECTION_OF = rf"Section[s]?\s+{_SECTION_NUM}\s+of\s+{_ATOM}"
_ARTICLE_OF = rf"Articles?\s+{_SECTION_NUM}\s+of\s+{_ATOM}"
# "Section/Article N of the (said) Act/Constitution/Code/provision" — pronoun
# reference to an act named earlier in the surrounding text.
_SECTION_OF_GENERIC = (
    rf"(?:Section[s]?|Articles?)\s+{_SECTION_NUM}\s+of\s+"
    r"(?:that\s+|the\s+(?:said\s+|aforesaid\s+|same\s+)?)?"
    r"(?:Act|Code|Constitution|provision|Sanhita|Adhiniyam)"
)
# Bare "Section N" / "Article N" with no "of …" — only counts as a citation
# inside a connective ("under Section 18", "pursuant to Article 226").
_BARE_SECTION = rf"(?:Section[s]?|Articles?)\s+{_SECTION_NUM}"

# "the relevant/applicable/various sections of <Act>" — vague reference to
# an act with no specific section number called out. Often appears as
# "under the relevant sections of the BNS, 2023".
_VAGUE_QUALIFIER = (
    r"(?:relevant|applicable|appropriate|prevailing|various|specific|"
    r"certain|other|said|same|aforesaid|necessary|pertinent)"
)
_VAGUE_UNIT = r"(?:sections?|provisions?|clauses?|articles?|chapters?)"
_VAGUE_TARGET = (
    rf"(?:{_ATOM}|"
    r"(?:the\s+)?(?:said\s+|aforesaid\s+|same\s+)?"
    r"(?:Act|Code|Constitution|provision)s?(?:,\s*\d{4})?)"
)
_VAGUE_SECTIONS_OF = (
    rf"(?:the\s+)?{_VAGUE_QUALIFIER}\s+{_VAGUE_UNIT}\s+of\s+{_VAGUE_TARGET}"
)

_TAIL = r"(?:,\s*(?:where\s+permissible|as\s+applicable|subject\s+to\s+law))?"

# Citation as it appears WITHOUT a leading connective (must include an act).
# Order matters: longer-prefix patterns first so they aren't eaten by _ATOM.
_CITATION = (
    rf"(?:{_SECTION_OF}|{_ARTICLE_OF}|{_SECTION_OF_GENERIC}|"
    rf"{_VAGUE_SECTIONS_OF}|{_ATOM})"
)
_CITATION_LIST = rf"{_CITATION}(?:\s*(?:,|and)\s*{_CITATION})*{_TAIL}"

# Citation as it appears AFTER a connective — also accepts bare "Section N".
_CITATION_CONN = (
    rf"(?:{_SECTION_OF}|{_ARTICLE_OF}|{_SECTION_OF_GENERIC}|"
    rf"{_VAGUE_SECTIONS_OF}|{_ATOM}|{_BARE_SECTION})"
)
_CITATION_CONN_LIST = (
    rf"{_CITATION_CONN}(?:\s*(?:,|and)\s*{_CITATION_CONN})*{_TAIL}"
)

# P1: sentence-leading connective + citation + comma.
_P1 = re.compile(
    rf"(^|[.!?]\s)"
    rf"(?:In\s+accordance\s+with|Pursuant\s+to|As\s+per|According\s+to|Under)\s+"
    rf"{_CITATION_CONN_LIST}\s*,?\s*",
    re.IGNORECASE,
)
# P2: mid-sentence connective + citation (eats its leading space AND
# optionally the comma that usually follows the citation).
_P2 = re.compile(
    rf"\s+(?:under|pursuant\s+to|as\s+per|in\s+accordance\s+with|according\s+to|"
    rf"as\s+permitted\s+under|as\s+provided\s+under|as\s+envisaged\s+under|"
    rf"as\s+recognized\s+under|as\s+contemplated\s+under|as\s+defined\s+in|"
    rf"as\s+set\s+out\s+in|as\s+stipulated\s+in|as\s+enshrined\s+in)\s+"
    rf"{_CITATION_CONN_LIST},?",
    re.IGNORECASE,
)
# P3: "Section/Article N of <Act>[, year]" or "Section N of the (said) Act"
# without a connective.
_P3 = re.compile(
    rf"\b(?:{_SECTION_OF}|{_ARTICLE_OF}|{_SECTION_OF_GENERIC})"
    rf"(?:\s*(?:,|and)\s*{_ATOM})*{_TAIL}\s*,?",
    re.IGNORECASE,
)
# P4: orphan act mention, optionally preceded by "and " or ", ".
_P4 = re.compile(
    rf"(?:(?:,|and)\s+)?\b{_ATOM}\b{_TAIL}",
    re.IGNORECASE,
)


def _regex_law_safety_net(text: str) -> str:
    """Deterministic post-pass over Gemini's output (or the original input
    when Gemini failed). Strips citations referencing a curated list of
    known Indian acts. Unknown acts are intentionally left to the LLM."""
    if not isinstance(text, str) or not text.strip():
        return text

    out = _P1.sub(lambda m: m.group(1), text)
    out = _P2.sub("", out)
    out = _P3.sub("", out)
    out = _P4.sub("", out)

    # Cleanup leftover punctuation/whitespace artifacts.
    out = re.sub(r",\s*,+", ",", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"\band\s*,", ",", out)
    out = re.sub(r",\s*and\s*,", ",", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\.\s*\.", ".", out)
    # Sentence-start orphan punctuation left after a citation removal.
    out = re.sub(r"([.!?])\s*,\s*", r"\1 ", out)
    out = re.sub(r"^\s*[,;:]\s*", "", out)
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r"[ \t]+,", ",", out)
    return out.strip()


def _build_retry_hint(attempt: int) -> str:
    """Sharpened instruction appended after `LAW_STRIP_SYSTEM` for retry passes."""
    if attempt == 0:
        return (
            "RETRY: Your previous output still contains a law citation. "
            "Scan it again and identify EVERY statute reference, every "
            "'Section N' / 'Article N', every 'Section N of <Act>' or "
            "'Section N of the (said) Act', every act name (including "
            "BNS, BNSS, BSA, IPC, CrPC, CPC, and any '... Act, YYYY' / "
            "'... Adhiniyam' / '... Sanhita') — then remove ALL of them. "
            "Also remove the comma that immediately follows a stripped "
            "mid-sentence citation. Preserve every other word and its "
            "original case. Output only the cleaned text."
        )
    return (
        "RETRY (final): Use the following chain-of-thought silently, then "
        "output ONLY the final cleaned text on the last line.\n"
        "STEP 1 — Mentally list every law citation still present "
        "(act names, section numbers, article numbers, acronyms, "
        "'Section N of the Act' pronoun references).\n"
        "STEP 2 — Remove each one. After removing a mid-sentence "
        "citation, also remove the comma that immediately follows it.\n"
        "STEP 3 — Output the cleaned text. Surviving words keep their "
        "exact original case (lowercase 'the initiation' stays lowercase). "
        "No commentary, no quotes, no STEP labels in the output."
    )


def _law_strip_via_llm(text: str) -> str:
    """Remove law citations primarily via Gemini. If the first pass still
    leaves citation keywords detectable, retry up to twice with sharpened
    self-critique prompts. Only when Gemini is fully unavailable do we fall
    back to the deterministic `_regex_law_safety_net` so exports never ship
    raw citations during a Gemini outage."""
    if not isinstance(text, str) or not text.strip():
        return text
    if not has_potential_law_citation(text):
        return text

    candidate = _gemini_law_strip_call(text)
    if candidate is None:
        return _regex_law_safety_net(text)

    for attempt in range(2):
        if not has_potential_law_citation(candidate):
            return candidate
        retry_out = _gemini_law_strip_call(
            text, extra_instruction=_build_retry_hint(attempt)
        )
        if retry_out is None:
            break
        candidate = retry_out

    if has_potential_law_citation(candidate):
        regex_alt = _regex_law_safety_net(text)
        return regex_alt if len(regex_alt) <= len(candidate) else candidate
    return candidate


def _is_applicable_laws_key(key: Any) -> bool:
    """True when the dict key names a laws section that must be preserved
    verbatim — both the 'Applicable Laws' subtree and the structured `laws`
    array. Any key matching here is skipped by both the forbidden-words
    filter and the law-citation stripper."""
    if not isinstance(key, str):
        return False
    k = key.lower()
    if k == "laws":
        return True
    return "applicable" in k and "law" in k


def _collect_law_strings(node: Any, into: Dict[str, None]) -> None:
    """Walk a tree collecting unique strings that need law-citation stripping,
    skipping any subtree keyed by an 'applicable laws' key."""
    if isinstance(node, str):
        if node and has_potential_law_citation(node):
            into[node] = None
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if _is_applicable_laws_key(k):
                continue
            _collect_law_strings(v, into)
        return
    if isinstance(node, list):
        for i in node:
            _collect_law_strings(i, into)


def _rebuild_law_mapping(node: Any, mapping: Dict[str, str]) -> Any:
    """Rebuild the tree substituting law-stripped strings; preserve any
    'applicable laws' subtree exactly as-is."""
    if isinstance(node, str):
        return mapping.get(node, node)
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if _is_applicable_laws_key(k):
                out[k] = v
            else:
                out[k] = _rebuild_law_mapping(v, mapping)
        return out
    if isinstance(node, list):
        return [_rebuild_law_mapping(i, mapping) for i in node]
    return node


def strip_laws_in_structure(data: Any) -> Any:
    """Strip law citations from every string in the tree, except inside any
    subtree keyed by an 'applicable laws' key.

    Uses the 3-phase parallel pattern (collect / submit-all / rebuild) so all
    Gemini calls for one request fan out concurrently through `_LLM_EXECUTOR`.
    """
    if isinstance(data, str):
        return _law_strip_via_llm(data)
    if not isinstance(data, (dict, list)):
        return data

    unique: Dict[str, None] = {}
    _collect_law_strings(data, unique)
    if not unique:
        return data

    deadline = time.monotonic() + _LAW_STRIP_BUDGET_S
    futures = {s: _LLM_EXECUTOR.submit(_law_strip_via_llm, s) for s in unique}
    mapping = _resolve_futures(futures, deadline)
    return _rebuild_law_mapping(data, mapping)


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
