"""JSON recovery for JSON-mode responses, shared by every provider."""

from __future__ import annotations

import json
import re
from typing import Optional

_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>\s*", flags=re.I)


def strip_reasoning(text: str) -> str:
    """Drop inline ``<think>…</think>`` blocks (DeepSeek-R1, QwQ, Qwen3 thinking
    mode served through OpenAI-compatible servers). An unterminated block —
    output cut off mid-thought — leaves nothing usable."""
    if not text or "<think>" not in text.lower():
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    lowered = cleaned.lower()
    if "<think>" in lowered:
        cleaned = cleaned[: lowered.index("<think>")]
    return cleaned.strip()


def extract_balanced_json_fragment(text: str) -> Optional[str]:
    if not text:
        return None
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [x for x in (start_obj, start_arr) if x >= 0]
    if not starts:
        return None
    start = min(starts)
    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    quote = '"'
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            i += 1
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start : i + 1].strip()
        i += 1
    return text[start:].strip()


def coerce_valid_json_text(raw: str) -> Optional[str]:
    """Return ``raw`` reduced to one parseable JSON value, or None."""
    text = strip_reasoning((raw or "").strip())
    if not text:
        return None
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
        if match:
            text = match.group(1).strip()
        else:
            text = re.sub(r"^```\w*\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text).strip()
    candidate = extract_balanced_json_fragment(text) or text
    candidate = re.sub(r",\s*([\]}])", r"\1", candidate)
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        return None


JSON_ONLY_INSTRUCTION = (
    "Respond with ONLY one valid JSON value — no markdown fences, no prose "
    "before or after it."
)
