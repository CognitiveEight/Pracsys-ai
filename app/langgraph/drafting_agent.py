"""
Drafting Assistant agent.

Two responsibilities:

1) Proactive *suggested drafts* — generated alongside `analysis_report` /
   `procedural_timeline` when a workspace is created or synced. Output is a
   list of 4–6 `{title, relevance, description}` items shown in the Drafting
   Assistant panel.

2) Multi-turn drafting *conversation*, keyed by `actionable_id` (one draft
   session per lawyer click on "Start New Draft"). The lawyer picks a
   template / suggested-draft / blank; the agent analyzes the template
   against the case, asks for missing slots in chat, then renders the final
   draft as a Markdown string in `draftDocument.content`.

The agent reuses:
  - `app.processing.file_handlers.FileProcessor.process_file_by_extension`
    for template text extraction.
  - `app.processing.document_processor.document_processor._download_file`
    for fetching template URLs (signed GCS links).
  - `app.vectorstore.qdrant.vector_store` user/workspace state caches for
    template storage and draft-session persistence.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.ai.gemini_client import gemini_client
from app.prompts.drafting import (
    DRAFT_RENDER_INSTRUCTION,
    SUGGESTED_DRAFTS_INSTRUCTION,
    TEMPLATE_KICKOFF_INSTRUCTION,
    TEMPLATE_MATCH_INSTRUCTION,
    build_draft_render_prompt,
    build_suggested_drafts_prompt,
    build_template_kickoff_prompt,
    build_template_match_prompt,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMPLATE_CACHE_KIND = "template"
DRAFTING_SESSION_CACHE_KIND = "drafting_session"

_RELEVANCE_VALUES = {"High", "Medium", "Low"}
_VALID_SOURCES = {"template", "suggestedDraft", "blank"}
_MAX_SUGGESTIONS = 6

# Strip IPC / CrPC / Indian Evidence Act mentions defensively in case the
# model slips. The existing applicable-laws normaliser already does this for
# its own payload, but the drafting outputs are independent.
_FORBIDDEN_ACT_PATTERNS = (
    re.compile(r"\bindian penal code\b", re.IGNORECASE),
    re.compile(r"\bI\.?P\.?C\.?\b"),
    re.compile(r"\bcode of criminal procedure\s*,?\s*1973\b", re.IGNORECASE),
    re.compile(r"\bCr\.?P\.?C\.?\s*,?\s*1973\b"),
    re.compile(r"\bindian evidence act\s*,?\s*1872\b", re.IGNORECASE),
)


def _strip_code_fences(text: str) -> str:
    """Remove ```json``` fences and stray markdown if the model added them."""
    if not text:
        return ""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw).strip()
    return raw


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Parse text expected to be a JSON object; return None on failure."""
    raw = _strip_code_fences(text)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        # Try to locate the first {...} block
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _parse_json_array(text: str) -> List[Any]:
    """Parse text expected to be a JSON array; return [] on failure."""
    raw = _strip_code_fences(text)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    return data if isinstance(data, list) else []


def _looks_like_forbidden_act(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _FORBIDDEN_ACT_PATTERNS)


def _filename_for_extraction(name: Optional[str], extension: Optional[str]) -> str:
    """Produce a filename with extension so FileProcessor can route extraction."""
    name = (name or "template").strip()
    ext = (extension or "").strip().lstrip(".").lower()
    if ext and not name.lower().endswith("." + ext):
        return f"{name}.{ext}"
    return name


def _word_count(text: str) -> int:
    return len([t for t in re.split(r"\s+", text or "") if t])


# ---------------------------------------------------------------------------
# Suggested-drafts pipeline
# ---------------------------------------------------------------------------

def _normalize_relevance(value: Any) -> str:
    s = (str(value or "").strip()).title()
    return s if s in _RELEVANCE_VALUES else "Medium"


def _dedupe_suggestions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop near-duplicates by lowercased, whitespace-collapsed title."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        key = re.sub(r"\s+", " ", title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _filter_forbidden_acts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove any suggestion that mentions IPC / CrPC / Indian Evidence Act."""
    out: List[Dict[str, Any]] = []
    for item in items:
        combined = " ".join(
            str(item.get(k) or "") for k in ("title", "description")
        )
        if _looks_like_forbidden_act(combined):
            logger.debug(
                "Drafting suggestions: dropped item referencing IPC/CrPC/IEA: {}",
                item.get("title"),
            )
            continue
        out.append(item)
    return out


def _normalize_suggestions(raw: List[Any]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        cleaned.append(
            {
                "title": title,
                "relevance": _normalize_relevance(item.get("relevance")),
                "description": (item.get("description") or "").strip()[:160] or None,
            }
        )
    cleaned = _filter_forbidden_acts(_dedupe_suggestions(cleaned))
    return cleaned[:_MAX_SUGGESTIONS]


class DraftingAgent:
    """High-level entrypoint for the Drafting Assistant feature."""

    # ------------------------------------------------------------------
    # 1) Suggested drafts panel
    # ------------------------------------------------------------------

    async def suggest(
        self,
        *,
        role: str,
        case_summary: str,
        applicable_laws_brief: str = "",
        strategy_brief: str = "",
        analysis_brief: str = "",
        list_of_dates_brief: str = "",
    ) -> List[Dict[str, Any]]:
        """Return 4–6 proactive draft suggestions for the workspace.

        Falls back to an empty list on any failure — callers should not block
        workspace creation on this feature.
        """
        prompt = build_suggested_drafts_prompt(
            role=role,
            case_summary=case_summary,
            applicable_laws_brief=applicable_laws_brief,
            strategy_brief=strategy_brief,
            analysis_brief=analysis_brief,
            list_of_dates_brief=list_of_dates_brief,
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=SUGGESTED_DRAFTS_INSTRUCTION,
                temperature=0.4,
                max_tokens=2000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Drafting suggestions LLM call failed: {}", e)
            return []

        parsed = _parse_json_array(raw)
        suggestions = _normalize_suggestions(parsed)
        if not suggestions:
            logger.info(
                "Drafting suggestions produced no items (raw preview: {})",
                (raw or "")[:200],
            )
        else:
            logger.success(
                "Drafting suggestions: {} item(s) ready",
                len(suggestions),
            )
        return suggestions

    # ------------------------------------------------------------------
    # 2) Template ingestion (called from template_sync handler)
    # ------------------------------------------------------------------

    async def ingest_template(
        self,
        *,
        user_id: str,
        template_id: str,
        name: str,
        category: str,
        extension: str,
        mime_type: Optional[str],
        file_url: Optional[str],
        path: Optional[str],
        file_hash: Optional[str],
        previous_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Download a template, extract text, and persist under the user cache.

        Returns the cache payload on success, None on failure. If
        `previous_payload` has a matching `file_hash`, the existing
        extracted text is preserved (no re-download).
        """
        if not file_url:
            logger.warning(
                "Template ingest skipped — missing fileUrl (user={}, template={})",
                user_id,
                template_id,
            )
            return None

        # Skip re-extraction when content hash is stable.
        if (
            previous_payload
            and file_hash
            and previous_payload.get("file_hash") == file_hash
            and previous_payload.get("extracted_text")
        ):
            logger.debug(
                "Template {} content unchanged (hash match) — reusing cached extract",
                template_id,
            )
            extracted_text = previous_payload.get("extracted_text") or ""
            extraction_status = previous_payload.get("extraction_status") or "ready"
        else:
            try:
                # Reuse the document_processor download helper for retry/size validation.
                from app.processing.document_processor import document_processor

                if not document_processor.http_client:
                    await document_processor.initialize()
                filename = _filename_for_extraction(name, extension)
                content, _size = await document_processor._download_file(
                    file_url, filename
                )
            except Exception as e:
                logger.error(
                    "Template download failed (user={}, template={}, url={}): {}",
                    user_id,
                    template_id,
                    (file_url or "")[:80],
                    e,
                )
                return None

            try:
                from app.processing.file_handlers import file_processor

                extracted_text = await asyncio.to_thread(
                    file_processor.process_file_by_extension,
                    content,
                    _filename_for_extraction(name, extension),
                )
            except Exception as e:
                logger.error(
                    "Template extraction failed (user={}, template={}): {}",
                    user_id,
                    template_id,
                    e,
                )
                return None
            extracted_text = (extracted_text or "").strip()
            extraction_status = "ready" if extracted_text else "failed"

        now_iso = datetime.utcnow().isoformat()
        name_history: List[str] = []
        if previous_payload:
            name_history = list(previous_payload.get("name_history") or [])

        payload: Dict[str, Any] = {
            "template_id": template_id,
            "user_id": user_id,
            "category": category,
            "name": name,
            "extension": (extension or "").lstrip(".").lower(),
            "mime_type": mime_type or "",
            "file_hash": file_hash or "",
            "source_path": path or "",
            "source_url": file_url,
            "extracted_text": extracted_text,
            "extraction_status": extraction_status,
            "ingested_at": now_iso,
            "name_history": name_history,
        }

        try:
            from app.vectorstore.qdrant import vector_store

            ok = await vector_store.upsert_user_state_cache(
                user_id=user_id,
                kind=TEMPLATE_CACHE_KIND,
                scope_id=template_id,
                data=payload,
            )
            if not ok:
                logger.warning(
                    "Template cache upsert returned False (user={}, template={})",
                    user_id,
                    template_id,
                )
                return None
        except Exception as e:
            logger.warning(
                "Template cache upsert raised (user={}, template={}): {}",
                user_id,
                template_id,
                e,
            )
            return None

        logger.success(
            "Template ingested (user={}, template={}, status={}, chars={})",
            user_id,
            template_id,
            extraction_status,
            len(extracted_text),
        )
        return payload

    async def rename_template(
        self,
        *,
        user_id: str,
        template_id: str,
        new_name: str,
        previous_name: Optional[str],
    ) -> bool:
        """Patch the cached template's name; content untouched."""
        from app.vectorstore.qdrant import vector_store

        existing = await vector_store.get_user_state_cache(
            user_id=user_id,
            kind=TEMPLATE_CACHE_KIND,
            scope_id=template_id,
        )
        if not isinstance(existing, dict):
            logger.debug(
                "Rename: no cached template for user={} template={}",
                user_id,
                template_id,
            )
            return False
        history = list(existing.get("name_history") or [])
        if previous_name and previous_name not in history:
            history.append(previous_name)
        existing["name"] = new_name
        existing["name_history"] = history
        existing["ingested_at"] = datetime.utcnow().isoformat()
        ok = await vector_store.upsert_user_state_cache(
            user_id=user_id,
            kind=TEMPLATE_CACHE_KIND,
            scope_id=template_id,
            data=existing,
        )
        if ok:
            logger.info(
                "Template renamed (user={}, template={}): {!r} → {!r}",
                user_id,
                template_id,
                previous_name,
                new_name,
            )
        return ok

    async def delete_template(self, *, user_id: str, template_id: str) -> bool:
        from app.vectorstore.qdrant import vector_store

        ok = await vector_store.delete_user_state_cache(
            user_id=user_id,
            kind=TEMPLATE_CACHE_KIND,
            scope_id=template_id,
        )
        if ok:
            logger.info(
                "Template deleted from cache (user={}, template={})",
                user_id,
                template_id,
            )
        return ok

    async def get_template(
        self, *, user_id: str, template_id: str
    ) -> Optional[Dict[str, Any]]:
        from app.vectorstore.qdrant import vector_store

        data = await vector_store.get_user_state_cache(
            user_id=user_id,
            kind=TEMPLATE_CACHE_KIND,
            scope_id=template_id,
        )
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # 2b) Suggested-draft → lawyer's-library template matching
    # ------------------------------------------------------------------

    # Legally-meaningful tokens. Overlap on these counts toward a match;
    # everything else (stop words, dates, numbers) is ignored. Keep the list
    # focused on Indian-legal document-type signals — these reliably appear
    # in both AI-generated suggestion titles and lawyer-named template files.
    _MATCH_ANCHOR_TOKENS = frozenset(
        {
            # Document-type tokens (high discrimination — name a specific kind
            # of pleading / instrument).
            "bail", "anticipatory", "regular", "interim",
            "divorce", "maintenance", "matrimonial", "dowry",
            "domestic", "violence", "restitution", "custody",
            # Pleading types.
            "petition", "plaint", "writ", "appeal", "revision", "rejoinder",
            "synopsis", "argument", "arguments",
            # Notice / correspondence types.
            "notice", "reply", "demand", "recovery", "termination",
            # Instruments.
            "affidavit", "undertaking", "vakalatnama", "vakalatname",
            "mou", "agreement", "deed", "letterhead",
            # Criminal-process tokens.
            "complaint", "fir", "police",
            "cheque", "bounce", "138", "ndps", "narcotic", "psychotropic",
            # Regulatory / consumer.
            "rti", "consumer", "deficiency",
            # Procedural tokens.
            "miscellaneous", "arbitration",
            "summary", "interlocutory", "stay", "injunction",
            # BNS / BNSS / BSA abbreviations (specific enough to keep).
            "bns", "bnss", "bsa",
            # NOTE: generic single-word tokens like "application", "section",
            # "act", "legal", "service", "information", "statement", "letter",
            # "it" are deliberately NOT here — they appear in nearly every
            # Indian legal document body, so once we score on content (not
            # just names), they would generate noise that drowns out the
            # signal from discriminating tokens. The multi-word phrases below
            # still catch combinations like "bail application", "legal
            # notice", "written statement", "section 138" etc.
        }
    )

    # Multi-word phrases that, if present in BOTH the suggestion text AND a
    # template name, are strong evidence of a match even when single-token
    # overlap is weak.
    _MATCH_ANCHOR_PHRASES = (
        "bail application",
        "anticipatory bail",
        "legal notice",
        "reply notice",
        "section 138",
        "cheque bounce",
        "consumer complaint",
        "divorce petition",
        "domestic violence",
        "rti application",
        "demand notice",
        "recovery notice",
        "criminal complaint",
        "police complaint",
        "written statement",
        "miscellaneous application",
        "anticipatory bail application",
    )

    # Common English stop words to drop from token scoring.
    _MATCH_STOP_WORDS = frozenset(
        {
            "a", "an", "the", "of", "in", "on", "at", "for", "to", "by",
            "and", "or", "with", "without", "under", "before", "this",
            "that", "these", "those", "is", "are", "was", "were", "be",
            "as", "from", "into", "upon", "no", "any", "all", "such",
        }
    )

    @classmethod
    def _normalise_for_match(cls, text: str) -> str:
        """Lowercase, replace any non-letter/digit with whitespace, collapse runs.

        IMPORTANT: explicitly include "_", "-", "." in the strip set so that
        filename-style template names like "General_Bail_Application_Template_India"
        tokenise into individual words. The previous regex used ``[^\\w\\s]``,
        which preserves underscores (``\\w`` matches ``[A-Za-z0-9_]``) — that
        kept multi-word names as a single token and silently broke matching
        for every underscore- or dash-named template.
        """
        if not text:
            return ""
        # Replace anything that isn't an ASCII letter, digit, or whitespace
        # with a space. This catches "_", "-", ".", "'", etc.
        cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text).lower()
        # Drop standalone numbers (years / section numbers) — they create
        # spurious matches across unrelated documents.
        cleaned = re.sub(r"\b\d+\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @classmethod
    def _anchor_tokens(cls, text: str) -> set:
        """Tokens from `text` that intersect with the anchor vocabulary."""
        toks = [
            t for t in cls._normalise_for_match(text).split()
            if t and t not in cls._MATCH_STOP_WORDS
        ]
        return {t for t in toks if t in cls._MATCH_ANCHOR_TOKENS}

    @classmethod
    def _anchor_phrases_in(cls, text: str) -> set:
        """Anchor phrases present (substring) in normalised text."""
        n = " " + cls._normalise_for_match(text) + " "
        out = set()
        for phrase in cls._MATCH_ANCHOR_PHRASES:
            if f" {phrase} " in n:
                out.add(phrase)
        return out

    async def _find_matching_template_for_suggestion(
        self,
        *,
        user_id: str,
        suggestion_title: str,
        suggestion_description: str,
        fallback_text: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Pick a template from the lawyer's library that matches a clicked
        suggestion, using a deterministic fast-path with an LLM tiebreaker.

        `fallback_text` is folded into the search blob alongside
        `suggestion_title` / `suggestion_description` so the matcher can still
        find a template when BE hasn't yet shipped the suggestion fields on
        `user_message_request` — the kickoff message body (user content) is
        passed in as a fallback signal.

        Returns the full cached template payload on success, or None when no
        template is appropriate (falls back to AI generation downstream).
        """
        if not user_id:
            return None
        suggestion_text = " ".join(
            s for s in (suggestion_title, suggestion_description, fallback_text) if s
        ).strip()
        if not suggestion_text:
            logger.info(
                "Template matcher: no suggestion / fallback text — skipping match "
                "(user={})",
                user_id[:12],
            )
            return None

        try:
            from app.vectorstore.qdrant import vector_store

            entries = await vector_store.list_user_state_cache(
                user_id=user_id, kind=TEMPLATE_CACHE_KIND
            )
        except Exception as e:
            logger.warning("Template matcher: list_user_state_cache failed: {}", e)
            return None
        logger.info(
            "Template matcher invoked (user={}, library_size={}, suggestion_text={!r})",
            user_id[:12],
            len(entries) if entries else 0,
            suggestion_text[:160],
        )
        if not entries:
            return None  # Empty library — fast-out, no LLM call.

        # Filter to ready, text-bearing templates and project the bits we
        # need for scoring. Crucially, the **match blob** now combines the
        # template name (+ history) with the first ~1500 chars of the
        # extracted body. The body's opening lines are where document-type
        # signal is densest (cover-page headings, party blocks, "IN THE
        # COURT OF…", "BAIL APPLICATION", "MOST RESPECTFULLY SHOWETH").
        # This lets the matcher work even when the lawyer uploaded the
        # template with an uninformative filename like `document.docx` or
        # `Format_A.pdf`.
        ready: List[Dict[str, Any]] = []
        for ent in entries:
            data = ent.get("data") if isinstance(ent, dict) else None
            if not isinstance(data, dict):
                continue
            if (data.get("extraction_status") or "").lower() != "ready":
                continue
            extracted = (data.get("extracted_text") or "").strip()
            if not extracted:
                continue
            tid = (data.get("template_id") or ent.get("scope_id") or "").strip()
            if not tid:
                continue
            name = (data.get("name") or "").strip()
            history = [str(x).strip() for x in (data.get("name_history") or []) if x]
            content_head = extracted[:1500]
            match_blob = " | ".join(
                s for s in [name, *history, content_head] if s
            )
            ready.append(
                {
                    "templateId": tid,
                    "name": name,
                    "category": data.get("category") or "",
                    "match_blob": match_blob,
                    "content_head": content_head,
                    "payload": data,
                }
            )
        if not ready:
            return None

        suggestion_anchors = self._anchor_tokens(suggestion_text)
        suggestion_phrases = self._anchor_phrases_in(suggestion_text)

        # Deterministic fast-path: score each template by anchor overlap
        # against the enriched match_blob (name + history + content head).
        scored: List[Tuple[Dict[str, Any], int, int]] = []
        for t in ready:
            tpl_anchors = self._anchor_tokens(t["match_blob"])
            tpl_phrases = self._anchor_phrases_in(t["match_blob"])
            token_overlap = len(suggestion_anchors & tpl_anchors)
            phrase_overlap = len(suggestion_phrases & tpl_phrases)
            scored.append((t, token_overlap, phrase_overlap))

        # Sort by phrase overlap then token overlap, both descending.
        scored.sort(key=lambda r: (r[2], r[1]), reverse=True)

        # Per-candidate trace at INFO level so production logs reveal
        # exactly how each library template scored against the suggestion.
        logger.info(
            "Template matcher candidates (ready={}, suggestion_anchors={}, suggestion_phrases={}):",
            len(ready),
            sorted(suggestion_anchors),
            sorted(suggestion_phrases),
        )
        for entry, tok, phr in scored[:10]:
            logger.info(
                "  template={} name={!r} token_overlap={} phrase_overlap={}",
                entry["templateId"],
                (entry.get("name") or "")[:80],
                tok,
                phr,
            )

        # Candidates with any anchor signal at all.
        with_signal = [(t, tok, phr) for t, tok, phr in scored if (tok + phr) > 0]

        if not with_signal:
            # Zero anchor overlap with any template — save the LLM call.
            logger.info(
                "Drafting suggestion matcher: no anchor overlap with any of "
                "{} template(s); falling back to AI generation",
                len(ready),
            )
            return None

        if len(with_signal) == 1:
            # Single candidate with signal — confident deterministic match.
            entry, tok, phr = with_signal[0]
            logger.info(
                "Drafting suggestion matched to template (user={}, template={}, "
                "mode=fast-single, phrase_overlap={}, token_overlap={})",
                user_id[:12],
                entry["templateId"],
                phr,
                tok,
            )
            return entry["payload"]

        # Multiple candidates with anchor signal — defer to the LLM tiebreaker,
        # because deterministic counts cannot read modifiers like
        # "anticipatory" vs "regular" or "reply" vs "demand" that distinguish
        # otherwise similarly-named templates. We also pass a short content
        # snippet per candidate so the LLM can disambiguate when filenames
        # are uninformative (e.g. `document.docx`). Send up to the top 15
        # by score.
        shortlist: List[Dict[str, Any]] = []
        for entry, tok, phr in with_signal:
            shortlist.append(
                {
                    "templateId": entry["templateId"],
                    "name": entry["name"],
                    "category": entry["category"],
                    "contentSnippet": (entry.get("content_head") or "")[:500],
                }
            )
            if len(shortlist) >= 15:
                break

        prompt = build_template_match_prompt(
            suggestion_title=suggestion_title,
            suggestion_description=suggestion_description,
            templates=shortlist,
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=TEMPLATE_MATCH_INSTRUCTION,
                temperature=0.1,
                max_tokens=800,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Template matcher LLM tiebreaker failed: {}", e)
            return None

        parsed = _parse_json_object(raw) or {}
        chosen_id = (parsed.get("templateId") or "").strip()
        if not chosen_id or chosen_id.lower() == "null":
            logger.info(
                "Drafting suggestion matcher: LLM returned no match (reason={!r})",
                (parsed.get("reason") or "")[:120],
            )
            return None

        # Defensive — only honour ids we showed the model.
        chosen = next(
            (t for t in scored if t[0]["templateId"] == chosen_id), None
        )
        if chosen is None:
            logger.warning(
                "Template matcher: LLM returned unknown templateId={!r} — ignoring",
                chosen_id,
            )
            return None
        logger.info(
            "Drafting suggestion matched to template (user={}, template={}, mode=llm, reason={!r})",
            user_id[:12],
            chosen_id,
            (parsed.get("reason") or "")[:120],
        )
        return chosen[0]["payload"]

    # ------------------------------------------------------------------
    # 3) Multi-turn drafting conversation
    # ------------------------------------------------------------------

    async def handle_turn(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        request_fields: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Drive one turn of the drafting conversation.

        Returns a dict with:
            content:        conversational reply (markdown / plain text)
            draftStatus:    analyzing | gathering_info | ready | revising
            openQuestions:  list of {id, label} (possibly empty)
            draftDocument:  None or {title, format, content, version, ...}
            session_state:  updated session state to persist
        """
        actionable_id = (request_fields.get("actionableId") or "").strip()
        user_id = (request_fields.get("userId") or "").strip()
        workspace_id = (request_fields.get("workspaceId") or "").strip()
        source = (request_fields.get("source") or "").strip()
        if source and source not in _VALID_SOURCES:
            source = ""

        # ----- 3a. Load or initialize the session state -----
        session_state = await self._load_session_state(
            workspace_id=workspace_id, actionable_id=actionable_id
        )
        is_kickoff = (
            not session_state
            and (
                not conversation_history
                or source in _VALID_SOURCES
                or request_fields.get("templateId")
                or request_fields.get("suggestedDraftId")
            )
        )

        if is_kickoff:
            session_state = await self._handle_kickoff(
                user_message=user_message,
                case_context=case_context,
                request_fields=request_fields,
            )
        elif (session_state or {}).get("draft_status") == "ready":
            # The lawyer's reply after a delivered draft is *usually* an
            # acknowledgement ("ok", "thanks", "looks good"), not a real
            # revision instruction. Re-rendering on those would emit a
            # degenerate draft because the model has no actual edit to apply.
            # Only call the revision path when the message carries genuine
            # editing intent.
            if self._is_post_draft_acknowledgement(user_message):
                session_state["last_assistant_message"] = (
                    "Glad the draft looks good. Let me know any time if you "
                    "would like edits — for example, adjusting the prayer, "
                    "tightening the grounds, or adding a clause."
                )
                # draft_status stays "ready"; last_draft_document untouched.
                session_state["open_questions"] = []
            else:
                session_state = await self._handle_revision(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state or {},
                )
        else:
            session_state = await self._handle_followup(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state or {},
                request_fields=request_fields,
            )

        # Consume the per-turn delivery flag so it does NOT persist into the
        # next turn (BE only wants isDraft=true on the actual delivery turn).
        is_draft_delivery = bool(session_state.pop("_draft_just_delivered", False))

        # Persist the updated session state (best-effort).
        await self._save_session_state(
            workspace_id=workspace_id,
            actionable_id=actionable_id,
            user_id=user_id,
            session_state=session_state,
        )

        return {
            "content": session_state.get("last_assistant_message") or "",
            "draftStatus": session_state.get("draft_status") or "gathering_info",
            "openQuestions": list(session_state.get("open_questions") or []),
            "draftDocument": session_state.get("last_draft_document"),
            "isDraft": is_draft_delivery,
            "session_state": session_state,
        }

    # ------------------------------------------------------------------ helpers

    async def _load_session_state(
        self, *, workspace_id: str, actionable_id: str
    ) -> Optional[Dict[str, Any]]:
        if not workspace_id or not actionable_id:
            return None
        try:
            from app.workspace.persistence import restore_state

            data = await restore_state(
                workspace_id=workspace_id,
                kind=DRAFTING_SESSION_CACHE_KIND,
                chat_id=actionable_id,
            )
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug("Drafting session restore failed: {}", e)
            return None

    async def _save_session_state(
        self,
        *,
        workspace_id: str,
        actionable_id: str,
        user_id: str,
        session_state: Dict[str, Any],
    ) -> None:
        if not workspace_id or not actionable_id:
            return
        session_state["actionable_id"] = actionable_id
        session_state["user_id"] = user_id
        session_state["workspace_id"] = workspace_id
        session_state["updated_at"] = datetime.utcnow().isoformat()
        try:
            from app.workspace.persistence import persist_state

            persist_state(
                workspace_id=workspace_id,
                kind=DRAFTING_SESSION_CACHE_KIND,
                data=session_state,
                chat_id=actionable_id,
            )
        except Exception as e:
            logger.debug("Drafting session persist failed: {}", e)

    async def _resolve_template(
        self,
        *,
        user_id: str,
        request_fields: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Return the cached template payload, lazy-fetching if missing."""
        template_id = (request_fields.get("templateId") or "").strip()
        if not template_id:
            return None
        cached = await self.get_template(user_id=user_id, template_id=template_id)
        if cached and (cached.get("extracted_text") or "").strip():
            return cached
        # Cold cache: lazy-fetch using fields on the request.
        file_url = request_fields.get("templateUrl")
        if not file_url:
            logger.warning(
                "Template {} not cached and no templateUrl on request — cannot lazy-fetch",
                template_id,
            )
            return cached
        name = request_fields.get("templateOriginalName") or template_id
        mime_type = request_fields.get("templateMimeType")
        ext = ""
        if isinstance(name, str) and "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
        return await self.ingest_template(
            user_id=user_id,
            template_id=template_id,
            name=name,
            category=cached.get("category") if cached else "Document Draft",
            extension=ext,
            mime_type=mime_type,
            file_url=file_url,
            path=request_fields.get("templatePath") or "",
            file_hash=request_fields.get("templateFileHash"),
            previous_payload=cached,
        )

    # ------------------------------------------------------------------ kickoff

    async def _handle_kickoff(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        request_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        source = (request_fields.get("source") or "").strip()
        user_id = (request_fields.get("userId") or "").strip()
        role = (case_context.get("role") or "ADJUDICATOR").upper()
        case_summary = case_context.get("case_summary") or ""
        case_facts = case_context.get("case_facts_brief") or ""
        lawyer_instructions = (user_message or "").strip() or None
        suggestion_title = (request_fields.get("suggestionTitle") or "").strip() or None
        suggestion_description = (
            request_fields.get("suggestionDescription") or ""
        ).strip()

        # Resolve template — three paths:
        #  - explicit template pick: honour `templateId` directly.
        #  - suggested-draft click: scan the lawyer's Template Library for a
        #    structural match; if found, use it; otherwise fall through to
        #    AI-generated draft (current behaviour).
        #  - blank: no template lookup.
        template_payload: Optional[Dict[str, Any]] = None
        template_match_note: Optional[str] = None
        if source == "template" or request_fields.get("templateId"):
            template_payload = await self._resolve_template(
                user_id=user_id, request_fields=request_fields
            )
        elif source == "suggestedDraft":
            # `fallback_text` covers the case where BE has not yet shipped
            # `suggestionTitle` / `suggestionDescription` on
            # `user_message_request` — the lawyer's kickoff content often
            # still names the document type ("draft a bail application
            # ...") and is enough to drive matching deterministically.
            template_payload = await self._find_matching_template_for_suggestion(
                user_id=user_id,
                suggestion_title=suggestion_title or "",
                suggestion_description=suggestion_description,
                fallback_text=(user_message or "").strip(),
            )
            if template_payload:
                matched_name = template_payload.get("name") or ""
                template_match_note = (
                    f"Matched the lawyer's template '{matched_name}' from the "
                    f"library based on the suggested draft "
                    f"'{suggestion_title or 'New Draft'}'."
                )

        # Without a template / suggestion / instruction, we still kick off
        # but ask a broader scoping question.
        template_name = (template_payload or {}).get("name") if template_payload else ""
        template_text = (
            (template_payload or {}).get("extracted_text") if template_payload else ""
        ) or ""

        # ---------- Template kick-off (analyze + open questions) ----------
        if template_payload and template_text:
            prompt = build_template_kickoff_prompt(
                role=role,
                case_summary=case_summary,
                case_facts_brief=case_facts,
                template_name=template_name or "",
                template_text=template_text,
                lawyer_instructions=lawyer_instructions,
                draft_title_hint=suggestion_title,
                template_match_note=template_match_note,
            )
            raw = await self._safe_llm_json(
                prompt=prompt,
                system_instruction=TEMPLATE_KICKOFF_INSTRUCTION,
                temperature=0.35,
                max_tokens=3000,
            )
            parsed = _parse_json_object(raw) or {}
            message = (parsed.get("message") or "").strip()
            draft_title = (parsed.get("draftTitle") or "").strip() or (
                suggestion_title or template_name or "Untitled Draft"
            )
            open_qs = self._normalize_open_questions(parsed.get("openQuestions"))
            gathered = parsed.get("gatheredFacts")
            if not isinstance(gathered, dict):
                gathered = {}
            stripped_context = parsed.get("strippedContext") or []
            if not isinstance(stripped_context, list):
                stripped_context = []
            incompatible = bool(parsed.get("incompatible"))

            if not message:
                message = self._compose_questions_message(open_qs, draft_title)

            return {
                "source": "template",
                "template_id": template_payload.get("template_id"),
                "template_name": template_payload.get("name"),
                "suggested_draft_id": request_fields.get("suggestedDraftId"),
                "suggestion_title": suggestion_title,
                "draft_title": draft_title,
                "gathered_facts": gathered,
                "open_questions": open_qs,
                "stripped_context": stripped_context,
                "draft_status": (
                    "gathering_info" if not incompatible and open_qs else (
                        "analyzing" if incompatible else "gathering_info"
                    )
                ),
                "incompatible": incompatible,
                "current_draft_version": 0,
                "last_draft_document": None,
                "last_draft_markdown": "",
                "last_assistant_message": message,
                "created_at": datetime.utcnow().isoformat(),
            }

        # ---------- Suggested / blank kick-off (no template) ----------
        draft_title = suggestion_title or "New Draft"
        if source == "blank" and lawyer_instructions:
            draft_title = lawyer_instructions[:80]
        scoping_prompt = build_template_kickoff_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            template_name="(no template — start from scratch)",
            template_text=(
                "(no template was provided; produce open questions sufficient "
                "to draft the document titled above based on the case context)"
            ),
            lawyer_instructions=lawyer_instructions,
            draft_title_hint=suggestion_title,
        )
        raw = await self._safe_llm_json(
            prompt=scoping_prompt,
            system_instruction=TEMPLATE_KICKOFF_INSTRUCTION,
            temperature=0.4,
            max_tokens=2400,
        )
        parsed = _parse_json_object(raw) or {}
        message = (parsed.get("message") or "").strip()
        open_qs = self._normalize_open_questions(parsed.get("openQuestions"))
        gathered = parsed.get("gatheredFacts")
        if not isinstance(gathered, dict):
            gathered = {}
        if not message:
            message = self._compose_questions_message(open_qs, draft_title)

        return {
            "source": source or "blank",
            "template_id": None,
            "template_name": None,
            "suggested_draft_id": request_fields.get("suggestedDraftId"),
            "suggestion_title": suggestion_title,
            "draft_title": (parsed.get("draftTitle") or "").strip() or draft_title,
            "gathered_facts": gathered,
            "open_questions": open_qs,
            "stripped_context": [],
            "draft_status": "gathering_info" if open_qs else "ready",
            "incompatible": False,
            "current_draft_version": 0,
            "last_draft_document": None,
            "last_draft_markdown": "",
            "last_assistant_message": message,
            "created_at": datetime.utcnow().isoformat(),
        }

    # ------------------------------------------------------------------ followup

    async def _handle_followup(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        request_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update gathered facts from the user's message; ask remaining questions or render."""
        open_qs = list(session_state.get("open_questions") or [])
        gathered = dict(session_state.get("gathered_facts") or {})
        draft_title = session_state.get("draft_title") or "New Draft"

        # Map user's free-text response onto remaining open questions.
        if open_qs and (user_message or "").strip():
            mapping = await self._map_answers_to_questions(
                user_message=user_message,
                open_questions=open_qs,
            )
            for qid, value in (mapping or {}).items():
                if value:
                    gathered[qid] = value
        # Remaining open questions are those not in `gathered`.
        remaining = [q for q in open_qs if not gathered.get(q.get("id"))]
        session_state["gathered_facts"] = gathered
        session_state["open_questions"] = remaining

        if remaining:
            message = self._compose_questions_message(remaining, draft_title)
            session_state["draft_status"] = "gathering_info"
            session_state["last_assistant_message"] = message
            return session_state

        # All slots filled — render the final draft.
        return await self._render_and_attach(
            case_context=case_context,
            session_state=session_state,
            lawyer_instructions=(user_message or "").strip() or None,
            mode_override=None,
        )

    async def _map_answers_to_questions(
        self,
        *,
        user_message: str,
        open_questions: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """Use Gemini to parse the lawyer's free-text reply into slot values."""
        questions_block = "\n".join(
            f"- id={q.get('id')}: {q.get('label')}" for q in open_questions if q.get("id")
        )
        prompt = f"""You are parsing one chat reply from a lawyer answering numbered questions.

OPEN QUESTIONS (each has an id):
{questions_block}

LAWYER'S REPLY:
\"\"\"
{user_message}
\"\"\"

TASK: Return a JSON object mapping each question id the lawyer answered to a
short string capturing the answer. If the lawyer did not answer a particular
question, omit that id. Do not invent answers. Do not include any other text.

OUTPUT FORMAT:
{{"<id>": "<answer>", "...": "..."}}
"""
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=(
                    "You normalise a lawyer's free-text reply into a JSON map "
                    "keyed by open-question id. Output strict JSON only."
                ),
                temperature=0.1,
                max_tokens=1200,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.debug("Answer mapping LLM call failed: {}", e)
            return {}
        parsed = _parse_json_object(raw)
        if not parsed:
            return {}
        # Coerce values to strings.
        out: Dict[str, str] = {}
        for k, v in parsed.items():
            if isinstance(v, (str, int, float)):
                val = str(v).strip()
                if val:
                    out[str(k)] = val
        return out

    # ------------------------------------------------------------------ revision

    async def _handle_revision(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self._render_and_attach(
            case_context=case_context,
            session_state=session_state,
            lawyer_instructions=None,
            mode_override="revision",
            revision_instruction=(user_message or "").strip(),
        )

    # ------------------------------------------------------------------ render

    async def _render_and_attach(
        self,
        *,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        lawyer_instructions: Optional[str],
        mode_override: Optional[str],
        revision_instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        role = (case_context.get("role") or "ADJUDICATOR").upper()
        case_summary = case_context.get("case_summary") or ""
        case_facts = case_context.get("case_facts_brief") or ""
        draft_title = session_state.get("draft_title") or "New Draft"
        gathered = session_state.get("gathered_facts") or {}
        user_id = session_state.get("user_id") or case_context.get("user_id") or ""

        # Resolve template text on render (cache may have been hydrated since kickoff).
        template_text: Optional[str] = None
        template_name: Optional[str] = session_state.get("template_name")
        template_id: Optional[str] = session_state.get("template_id")
        if template_id and user_id:
            tpl = await self.get_template(user_id=user_id, template_id=template_id)
            if tpl:
                template_text = (tpl.get("extracted_text") or "").strip()
                template_name = template_name or tpl.get("name")

        source_value = session_state.get("source") or (
            "template" if template_id else "blank"
        )
        mode = mode_override or source_value or "blank"

        prev_version = int(session_state.get("current_draft_version") or 0)
        new_version = prev_version + 1
        session_state["draft_status"] = "revising" if mode == "revision" else "ready"

        prompt = build_draft_render_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            template_name=template_name,
            template_text=template_text,
            gathered_facts_json=json.dumps(gathered, ensure_ascii=False),
            draft_title=draft_title,
            lawyer_instructions=lawyer_instructions,
            mode=mode,
            previous_draft_markdown=session_state.get("last_draft_markdown") or None,
            revision_instruction=revision_instruction,
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=DRAFT_RENDER_INSTRUCTION,
                temperature=0.35,
                max_tokens=8000,
            )
        except Exception as e:
            logger.error("Draft render LLM call failed: {}", e)
            session_state["draft_status"] = "gathering_info"
            session_state["last_assistant_message"] = (
                "I ran into an issue while preparing the draft. "
                "Please retry, or share any additional details that may help."
            )
            return session_state

        markdown = _strip_code_fences(raw).strip()
        if not markdown:
            session_state["draft_status"] = "gathering_info"
            session_state["last_assistant_message"] = (
                "The draft could not be generated. Please confirm the details "
                "you have provided and try again."
            )
            return session_state

        draft_document = {
            "title": draft_title,
            "format": "markdown",
            "content": markdown,
            "version": new_version,
            "sourceTemplateId": template_id,
            "sourceTemplateName": template_name,
            "sourceType": source_value,
            "wordCount": _word_count(markdown),
        }
        # Conversational lead-in mirroring the screenshots (
        # "I have prepared the complete bail application below...")
        lead_in = (
            "I have prepared the complete draft below. "
            "Review the document, and let me know if you would like any "
            "edits — for example, adjusting the prayer or adding a clause."
        )
        if mode == "revision":
            lead_in = (
                "I have revised the draft as requested. Let me know if any "
                "further changes are needed."
            )

        # Embed the full draft Markdown inside the chat message body itself
        # so it always renders inline in the chat surface (matches the
        # reference UX in image (74).png — the bail-application body appears
        # in the AI's chat bubble). `draftDocument` is still emitted as a
        # structured payload for any dedicated editor surface that consumes
        # it. Without this, frontends that only render `content` would show
        # the lead-in but not the draft body.
        session_state["draft_status"] = "ready"
        session_state["current_draft_version"] = new_version
        session_state["last_draft_markdown"] = markdown
        session_state["last_draft_document"] = draft_document
        session_state["last_assistant_message"] = f"{lead_in}\n\n{markdown}"
        session_state["open_questions"] = []
        # Transient flag — true ONLY on the turn that just produced a draft
        # body, so BE can distinguish draft delivery from acknowledgements /
        # follow-up question turns. Consumed (popped) in handle_turn.
        session_state["_draft_just_delivered"] = True
        return session_state

    # ------------------------------------------------------------------ utils

    def _normalize_open_questions(self, raw: Any) -> List[Dict[str, str]]:
        if not isinstance(raw, list):
            return []
        seen_ids: set = set()
        out: List[Dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            qid = (item.get("id") or "").strip()
            label = (item.get("label") or "").strip()
            if not qid:
                # Derive a stable id from the label if the model forgot.
                slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
                qid = slug[:48] or f"q_{len(out) + 1}"
            if qid in seen_ids or not label:
                continue
            seen_ids.add(qid)
            out.append({"id": qid, "label": label})
        return out[:10]

    # Acknowledgement vocabulary used to detect "ok" / "thanks" replies after
    # a delivered draft so we do not spuriously re-render. Drafting-local on
    # purpose; intelligence chat keeps its own `is_user_affirmation_to_continue`
    # in `app/langgraph/chat_nodes.py` because that helper triggers continuation,
    # not the opposite (no-op acknowledgement) behaviour we need here.
    _ACK_PHRASES = frozenset(
        {
            # Pure affirmations
            "ok", "okay", "k", "kk", "yes", "yeah", "yep", "yup", "sure",
            "fine", "done", "right", "alright", "all right",
            # Positive feedback
            "thanks", "thank you", "thx", "ty", "tysm", "tq",
            "great", "perfect", "nice", "cool", "awesome", "super",
            "good", "good one", "well done",
            "looks good", "looks fine", "looks great", "looks ok",
            "all good", "that works", "works for me", "sounds good",
            "this is good", "this looks good",
            # Closing-out phrases
            "no thanks", "no thank you", "nothing else", "thats it", "that's it",
            "no changes", "no edits", "no more", "done thanks",
        }
    )

    _ACK_TOKENS = frozenset(
        {
            "ok", "okay", "k", "kk", "yes", "yeah", "yep", "yup", "sure",
            "fine", "done", "right", "alright",
            "thanks", "thank", "you", "thx", "ty", "tysm", "tq",
            "great", "perfect", "nice", "cool", "awesome", "super",
            "good", "well",
            "looks", "all", "that", "this", "is", "no", "more",
            "works", "for", "me", "sounds", "changes", "edits",
            "nothing", "else", "thats", "it",
        }
    )

    @staticmethod
    def _normalize_ack_text(text: str) -> str:
        """Lowercase, collapse whitespace, strip surrounding punctuation/emoji."""
        if not text:
            return ""
        # Drop emoji and most punctuation; keep letters, digits, apostrophes,
        # and whitespace so phrases like "thank you" / "that's it" survive.
        cleaned = re.sub(r"[^\w\s']", " ", text, flags=re.UNICODE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
        # Strip leading/trailing punctuation residue.
        return cleaned.strip(" \t.!?,;:")

    @classmethod
    def _is_post_draft_acknowledgement(cls, text: str) -> bool:
        """Return True when the lawyer's reply is acknowledgement-only.

        Used after `draft_status == "ready"` to short-circuit the revision
        re-render when the user merely confirmed the draft looks good.
        """
        raw = (text or "").strip()
        if not raw:
            return True  # empty payloads are not edit instructions
        normalised = cls._normalize_ack_text(raw)
        if not normalised:
            return True  # only punctuation / emoji
        # Hard cap on length: anything substantive a lawyer would write to
        # request a real edit will exceed this comfortably.
        if len(normalised) > 60:
            return False
        # Exact-phrase match (covers multi-word fixed expressions).
        if normalised in cls._ACK_PHRASES:
            return True
        # Token-by-token check: every word must belong to the ack vocab.
        tokens = [t for t in normalised.split() if t]
        if not tokens:
            return True
        return all(tok in cls._ACK_TOKENS for tok in tokens)

    def _compose_questions_message(
        self, open_questions: List[Dict[str, Any]], draft_title: str
    ) -> str:
        """Fallback message used only when the model fails to emit `message`.

        Mirrors the sectioned layout enforced by TEMPLATE_KICKOFF_INSTRUCTION
        so the lawyer's chat stays consistent if the structured-output call
        degrades. Each numbered question reuses the model-supplied label
        (which already carries "Title: question").
        """
        if not open_questions:
            return (
                f"I have initiated the drafting of {draft_title} based on the "
                f"present case context.\n\n"
                f"**Information Required to Complete the Draft:**\n"
                f"All required details have been captured from the case file. "
                f"Generating the draft now."
            )

        # Each label is expected to be "Title: question". When present,
        # split on the first colon so the title can be bolded inline.
        numbered_lines: List[str] = []
        for i, q in enumerate(open_questions, start=1):
            label = (q.get("label") or "").strip()
            if ":" in label:
                title_part, question_part = label.split(":", 1)
                numbered_lines.append(
                    f"{i}. **{title_part.strip()}:** {question_part.strip()}"
                )
            else:
                numbered_lines.append(f"{i}. {label}")
        questions_block = "\n".join(numbered_lines)

        return (
            f"I have initiated the drafting of {draft_title} based on the "
            f"present case context.\n\n"
            f"**Information Required to Complete the Draft:**\n"
            f"To finalise the draft, please provide the following details so "
            f"the document is complete and legally accurate:\n\n"
            f"{questions_block}"
        )

    async def _safe_llm_json(
        self,
        *,
        prompt: str,
        system_instruction: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        try:
            return await gemini_client.generate(
                prompt,
                system_instruction=system_instruction,
                temperature=temperature,
                max_tokens=max_tokens,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Drafting LLM call failed: {}", e)
            return ""


# Singleton — mirrors `procedural_timeline_agent` etc.
drafting_agent = DraftingAgent()
