"""
LangGraph agent for temporal inference in list-of-dates generation.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger


class TemporalInferenceState(TypedDict, total=False):
    document_text: str
    source_doc_id: str
    source_doc_name: str
    anchor_year: Optional[int]
    raw_response: str
    events: List[Dict[str, Any]]


class ListOfDatesTemporalAgent:
    """LLM-first temporal inference agent for relative date statements."""

    def __init__(self) -> None:
        self._workflow = self._build_graph()

    def _build_graph(self):
        g = StateGraph(TemporalInferenceState)
        g.add_node("infer_temporal_events", self._node_infer_temporal_events)
        g.add_node("validate_and_normalize", self._node_validate_and_normalize)
        g.add_edge(START, "infer_temporal_events")
        g.add_edge("infer_temporal_events", "validate_and_normalize")
        g.add_edge("validate_and_normalize", END)
        return g.compile()

    async def _node_infer_temporal_events(
        self, state: TemporalInferenceState
    ) -> Dict[str, Any]:
        from app.ai.gemini_client import gemini_client

        document_text = (state.get("document_text") or "").strip()
        if not document_text:
            return {"raw_response": "[]"}

        gemini_client.initialize()
        if not gemini_client.model:
            return {"raw_response": "[]"}

        anchor_year = state.get("anchor_year")
        anchor_txt = str(anchor_year) if isinstance(anchor_year, int) else "unknown"
        prompt = f"""You extract timeline events from legal text.

Capture BOTH:
1) Directly mentioned dates (explicit date strings in the text), and
2) Relative/implicit date scenarios, for example:
   - "approximately ten years"
   - "past four to five years"
   - "after five years", "before three years", "five years later"

Rules:
1) Use only evidence from the provided text.
2) Every explicit date mention that is materially relevant should appear in output.
3) If text contains explicit date/year anchors, use them for relative-year calculation.
4) If you infer year from relative phrase, output date as YYYY-01-01.
4) Do not invent facts not grounded in text.
5) Output only JSON array of objects:
[
  {{
    "date": "YYYY-MM-DD",
    "title": "short event title",
    "description": "short evidence-based explanation",
    "evidenceSnippet": "exact/nearby source text proving date (max 220 chars)",
    "pageNumber": integer (mandatory if inferable from text markers),
    "isIncident": true|false,
    "eventType": "Court Hearing|Deadline|Meeting|Filing|Other",
    "relevance": "High|Medium|Low"
  }}
]

Anchor year hint (if available): {anchor_txt}

Text:
{document_text[:20000]}
"""
        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Temporal agent LLM call failed: {}", e)
            raw = "[]"
        return {"raw_response": (raw or "[]").strip()}

    async def _node_validate_and_normalize(
        self, state: TemporalInferenceState
    ) -> Dict[str, Any]:
        raw = (state.get("raw_response") or "").strip()
        source_doc_id = str(state.get("source_doc_id") or "").strip()
        source_doc_name = str(state.get("source_doc_name") or "").strip()

        if not raw:
            return {"events": []}

        if "```" in raw:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.I)
            if m:
                raw = (m.group(1) or "").strip()

        arr: Any = []
        try:
            parsed = json.loads(raw)
            arr = parsed if isinstance(parsed, list) else parsed.get("events", [])
        except Exception:
            arr = []

        if not isinstance(arr, list):
            arr = []

        def _extract_page_markers(text: str) -> List[tuple[int, int]]:
            markers: List[tuple[int, int]] = []
            if not text:
                return markers
            for m in re.finditer(r"---\s*Page\s+(\d+)\s*---", text, flags=re.I):
                try:
                    markers.append((m.start(), int(m.group(1))))
                except Exception:
                    continue
            for m in re.finditer(r"--\s*(\d+)\s+of\s+\d+\s*--", text, flags=re.I):
                try:
                    markers.append((m.start(), int(m.group(1))))
                except Exception:
                    continue
            markers.sort(key=lambda x: x[0])
            return markers

        def _resolve_page_from_text(item: Dict[str, Any], text: str) -> int:
            raw_page = item.get("pageNumber")
            if isinstance(raw_page, int) and raw_page > 0:
                return raw_page
            if isinstance(raw_page, str):
                p = raw_page.strip()
                if p.isdigit() and int(p) > 0:
                    return int(p)

            markers = _extract_page_markers(text)
            if not markers:
                return 1

            anchors = [
                str(item.get("evidenceSnippet") or "").strip(),
                str(item.get("date") or "").strip(),
                str(item.get("title") or "").strip(),
                str(item.get("description") or "").strip(),
            ]
            haystack = (text or "").lower()
            pos = -1
            for anchor in anchors:
                if len(anchor) < 4:
                    continue
                pos = haystack.find(anchor.lower())
                if pos >= 0:
                    break

            if pos < 0:
                return markers[0][1] if markers and markers[0][1] > 0 else 1

            page = 1
            for marker_pos, marker_page in markers:
                if marker_pos <= pos:
                    page = marker_page if marker_page > 0 else page
                else:
                    break
            return page if page > 0 else 1

        document_text = str(state.get("document_text") or "")
        normalized: List[Dict[str, Any]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            date_val = str(item.get("date") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_val):
                continue
            page_number = _resolve_page_from_text(item, document_text)
            entry = {
                "date": date_val,
                "title": (str(item.get("title") or "").strip() or "Inferred Timeline Event"),
                "description": str(item.get("description") or "").strip(),
                "sourceDocId": source_doc_id or None,
                "sourceDocName": source_doc_name or None,
                "pageNumber": page_number,
                "isIncident": bool(item.get("isIncident", False)),
                "eventType": (
                    str(item.get("eventType") or "").strip() or "Other"
                ),
                "relevance": (
                    str(item.get("relevance") or "").strip() or "Medium"
                ),
            }
            if source_doc_name:
                entry["source"] = source_doc_name
                entry["sourceAnswer"] = source_doc_name
            normalized.append({k: v for k, v in entry.items() if v is not None and v != ""})
        return {"events": normalized}

    async def infer_temporal_events(
        self,
        *,
        document_text: str,
        source_doc_id: str,
        source_doc_name: str,
        anchor_year: Optional[int],
    ) -> List[Dict[str, Any]]:
        state: TemporalInferenceState = {
            "document_text": document_text,
            "source_doc_id": source_doc_id,
            "source_doc_name": source_doc_name,
            "anchor_year": anchor_year,
        }
        result = await self._workflow.ainvoke(state)
        out = result.get("events") if isinstance(result, dict) else []
        return out if isinstance(out, list) else []


list_of_dates_temporal_agent = ListOfDatesTemporalAgent()
