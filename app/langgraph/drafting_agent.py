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
    DRAFT_TRANSLATE_INSTRUCTION,
    DRAFTING_CHAT_INSTRUCTION,
    SUGGESTED_DRAFTS_INSTRUCTION,
    TEMPLATE_CLASSIFY_INSTRUCTION,
    TEMPLATE_DIFF_INSTRUCTION,
    TEMPLATE_DOCUMENT_TYPE_LABELS,
    TEMPLATE_FILL_INSTRUCTION,
    TEMPLATE_KICKOFF_INSTRUCTION,
    TEMPLATE_MATCH_INSTRUCTION,
    build_chat_reply_prompt,
    build_draft_render_prompt,
    build_draft_translate_prompt,
    build_suggested_drafts_prompt,
    build_template_classify_prompt,
    build_template_diff_prompt,
    build_template_fill_prompt,
    build_template_kickoff_prompt,
    build_template_match_prompt,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMPLATE_CACHE_KIND = "template"
DRAFTING_SESSION_CACHE_KIND = "drafting_session"

# How many characters of the extracted template body to read when
# classifying the document type. Indian legal documents often spend the
# first 1-2k chars on court header, addressee block, "REGISTERED A.D."
# preambles, and party arrays — the document-type heading ("LEGAL
# NOTICE", "BAIL APPLICATION", "MOST RESPECTFULLY SHOWETH") commonly
# appears AFTER that boiler-plate, so a generous window matters.
_CLASSIFY_CONTENT_CHARS = 4000

# DOCX export — public bucket prefix for generated drafts, private prefix
# for the original template skeletons we retain so future drafts can
# reuse the lawyer's formatting.
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_TEMPLATE_GCS_PREFIX = "templates"
_DRAFT_GCS_PREFIX = "draft"


def _safe_filename(name: str, *, default: str = "draft") -> str:
    """Slugify a draft title for safe inclusion in a GCS object key.

    Rules (matches the convention used elsewhere in the platform):
      - Spaces → ``_``.
      - Any character outside ``[A-Za-z0-9._-]`` is stripped.
      - Empty result falls back to ``default``.
    """
    name = (name or "").strip()
    if not name:
        return default
    name = name.replace(" ", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", name)
    return cleaned or default

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
                max_tokens=12000,
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
        downloaded_bytes: Optional[bytes] = None
        ext_lower = (extension or "").lstrip(".").lower()
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
                downloaded_bytes = content
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

        # Persist a DOCX skeleton to GCS so future drafts can reproduce
        # the lawyer's formatting under the llm_diff path. Native DOCX
        # templates are stored as-is. PDF templates are converted to a
        # DOCX skeleton at ingest (pdf2docx → OCR fallback) so the same
        # editing pipeline applies. Reuse the previously stored URI
        # when the file hash hasn't changed.
        template_docx_gcs_uri: Optional[str] = None
        if (
            previous_payload
            and file_hash
            and previous_payload.get("file_hash") == file_hash
            and previous_payload.get("template_docx_gcs_uri")
        ):
            template_docx_gcs_uri = previous_payload.get("template_docx_gcs_uri")
        else:
            if ext_lower == "pdf" and downloaded_bytes:
                # Convert PDF → DOCX so the existing upload-as-DOCX block
                # below can handle it. On failure we log and continue
                # without a skeleton — the draft path will fall through
                # to skeleton_clone with defaults styling, which still
                # produces a usable DOCX.
                try:
                    from app.processing.pdf_to_docx import convert_pdf_to_docx

                    pdf_bytes_len = len(downloaded_bytes)
                    converted = await asyncio.to_thread(
                        convert_pdf_to_docx, downloaded_bytes, name,
                    )
                    if converted:
                        downloaded_bytes = converted
                        ext_lower = "docx"
                        logger.info(
                            "Template PDF→DOCX conversion succeeded "
                            "(user={}, template={}, in_bytes={}, out_bytes={})",
                            user_id, template_id,
                            pdf_bytes_len, len(converted),
                        )
                    else:
                        logger.warning(
                            "Template PDF→DOCX conversion produced no usable "
                            "output (user={}, template={}) — llm_diff path will "
                            "fall back to skeleton_clone for this template",
                            user_id, template_id,
                        )
                except Exception as exc:
                    logger.warning(
                        "Template PDF→DOCX conversion raised (user={}, "
                        "template={}): {} — continuing without skeleton",
                        user_id, template_id, exc,
                    )

        if (
            template_docx_gcs_uri is None
            and ext_lower == "docx"
            and downloaded_bytes
        ):
            try:
                from app.core.config import settings as _settings
                from app.storage import gcs as _gcs

                bucket = getattr(_settings, "gcs_docx_bucket_name", None)
                if bucket:
                    blob_name = (
                        f"{_TEMPLATE_GCS_PREFIX}/{user_id}/{template_id}.docx"
                    )
                    await asyncio.to_thread(
                        _gcs.upload_bytes,
                        bucket,
                        blob_name,
                        downloaded_bytes,
                        _DOCX_MIME,
                        public=False,  # skeleton stays internal
                    )
                    template_docx_gcs_uri = f"gs://{bucket}/{blob_name}"
                else:
                    logger.warning(
                        "Template skeleton upload skipped — "
                        "GCS_DOCX_BUCKET_NAME not set"
                    )
            except Exception as e:
                # Non-fatal: text extraction already succeeded; only the
                # formatted-DOCX export path will degrade for this template.
                logger.warning(
                    "Template skeleton upload failed (user={}, template={}): {}",
                    user_id,
                    template_id,
                    e,
                )

        now_iso = datetime.utcnow().isoformat()
        name_history: List[str] = []
        if previous_payload:
            name_history = list(previous_payload.get("name_history") or [])

        # Document-type classification. Reuse the cached classification when
        # the file hash is unchanged AND a previous label exists, otherwise
        # run the classifier on the freshly-extracted body head. Never block
        # ingest on classifier failure — leave the field empty and the
        # matcher's anchor-overlap fallback path will still operate.
        document_type = ""
        document_type_aliases: List[str] = []
        document_type_confidence = ""
        classified_at = ""
        hash_stable = bool(
            previous_payload
            and file_hash
            and previous_payload.get("file_hash") == file_hash
        )
        if (
            hash_stable
            and previous_payload
            and previous_payload.get("document_type")
        ):
            document_type = (previous_payload.get("document_type") or "").strip()
            prev_aliases = previous_payload.get("document_type_aliases") or []
            if isinstance(prev_aliases, list):
                document_type_aliases = [
                    str(a).strip() for a in prev_aliases if str(a).strip()
                ]
            document_type_confidence = (
                previous_payload.get("document_type_confidence") or ""
            )
            classified_at = previous_payload.get("classified_at") or ""
        elif extraction_status == "ready" and extracted_text:
            classified = await self._classify_document_type(
                name=name,
                content_head=extracted_text[:_CLASSIFY_CONTENT_CHARS],
                is_suggestion=False,
            )
            if classified:
                document_type = classified.get("documentType", "") or ""
                document_type_aliases = classified.get("aliases", []) or []
                document_type_confidence = classified.get("confidence", "") or ""
                classified_at = now_iso

        payload: Dict[str, Any] = {
            "template_id": template_id,
            "user_id": user_id,
            "category": category,
            "name": name,
            "extension": ext_lower,
            "mime_type": mime_type or "",
            "file_hash": file_hash or "",
            "source_path": path or "",
            "source_url": file_url,
            "extracted_text": extracted_text,
            "extraction_status": extraction_status,
            "ingested_at": now_iso,
            "name_history": name_history,
            "template_docx_gcs_uri": template_docx_gcs_uri,
            "document_type": document_type,
            "document_type_aliases": document_type_aliases,
            "document_type_confidence": document_type_confidence,
            "classified_at": classified_at,
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
            "Template ingested (user={}, template={}, status={}, chars={}, "
            "document_type={!r}, confidence={!r})",
            user_id,
            template_id,
            extraction_status,
            len(extracted_text),
            document_type or "(unclassified)",
            document_type_confidence or "(n/a)",
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
    # 2a) Document-type classification (shared by ingest + click-time match)
    # ------------------------------------------------------------------

    # Pre-compute a case-folded lookup of the canonical vocabulary so we can
    # snap LLM responses back to their canonical spelling regardless of how
    # the model casing/spacing comes back.
    _DOC_TYPE_CANONICAL = {
        label.lower(): label for label in TEMPLATE_DOCUMENT_TYPE_LABELS
    }

    async def _classify_document_type(
        self,
        *,
        name: str,
        content_head: str,
        is_suggestion: bool,
    ) -> Optional[Dict[str, Any]]:
        """Classify a template or a clicked suggestion into a canonical
        document type. Returns a dict with keys `documentType`, `aliases`,
        `confidence` on success; returns None on failure (the caller should
        treat that as "unclassified" — never block on it).
        """
        try:
            prompt = build_template_classify_prompt(
                name=name or "",
                content_head=content_head or "",
                is_suggestion=is_suggestion,
            )
            raw = await gemini_client.generate(
                prompt,
                system_instruction=TEMPLATE_CLASSIFY_INSTRUCTION,
                temperature=0.1,
                max_tokens=2000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning(
                "Template document-type classification LLM call failed "
                "(is_suggestion={}): {}",
                is_suggestion,
                e,
            )
            return None

        parsed = _parse_json_object(raw) or {}
        raw_type = (parsed.get("documentType") or "").strip()
        # Snap to canonical spelling. Anything unrecognised collapses to "Other"
        # so the matcher never has to deal with free-form labels.
        canonical = self._DOC_TYPE_CANONICAL.get(raw_type.lower(), "")
        if not canonical:
            canonical = "Other"
        raw_aliases = parsed.get("aliases") or []
        aliases: List[str] = []
        if isinstance(raw_aliases, list):
            for a in raw_aliases:
                s = str(a or "").strip().lower()
                if s and s != canonical.lower() and s not in aliases:
                    aliases.append(s)
                if len(aliases) >= 6:
                    break
        confidence = (parsed.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        return {
            "documentType": canonical,
            "aliases": aliases,
            "confidence": confidence,
        }

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
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """Pick a template from the lawyer's library that matches a clicked
        suggestion. Returns ``(payload, ready_library_size)``.

        Match strategy (in order):
          1. **Type-first**: classify the suggestion into a canonical
             document type (e.g. "Legal Notice") and filter the library to
             templates carrying the same `document_type`. If the candidate
             set is unique, return it deterministically; if multiple share
             the type, run an LLM tiebreaker on that filtered shortlist.
          2. **Anchor-overlap fallback**: if type-first found nothing
             (suggestion classified as "Other", or no template shares the
             type), fall through to the legacy anchor-token / anchor-phrase
             overlap path so this change is backward-compatible with
             previously-ingested template caches that lack `document_type`.

        Lazy backfill: when a template payload is missing `document_type`,
        we classify it on the fly (in parallel with siblings) and persist
        the result so subsequent clicks are fast.

        `fallback_text` is folded into the suggestion blob so the matcher
        can still find a template when BE has not yet shipped the
        suggestion fields on `user_message_request` — the kickoff message
        body is passed in as a fallback signal.

        The second tuple element (ready library size — templates with
        usable extracted text) lets the kickoff path decide whether to
        surface a "no close template found" note to the lawyer.
        """
        if not user_id:
            return None, 0
        suggestion_text = " ".join(
            s for s in (suggestion_title, suggestion_description, fallback_text) if s
        ).strip()
        if not suggestion_text:
            logger.info(
                "Template matcher: no suggestion / fallback text — skipping match "
                "(user={})",
                user_id[:12],
            )
            return None, 0

        try:
            from app.vectorstore.qdrant import vector_store

            entries = await vector_store.list_user_state_cache(
                user_id=user_id, kind=TEMPLATE_CACHE_KIND
            )
        except Exception as e:
            logger.warning("Template matcher: list_user_state_cache failed: {}", e)
            return None, 0
        library_size = len(entries) if entries else 0
        logger.info(
            "Template matcher invoked (user={}, library_size={}, suggestion_text={!r})",
            user_id[:12],
            library_size,
            suggestion_text[:160],
        )
        if not entries:
            return None, 0  # Empty library — fast-out, no LLM call.

        # Filter to ready, text-bearing templates and project the bits we
        # need for scoring. The **match blob** combines the template name
        # (+ history) with the first ~1500 chars of the extracted body —
        # opening lines are where document-type signal is densest (court
        # headers, party blocks, "BAIL APPLICATION", "MOST RESPECTFULLY
        # SHOWETH"), so this works even when filenames are uninformative
        # like `document.docx` or `Format_A.pdf`.
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
            # Use a generous window so both the classifier (lazy backfill
            # of un-classified templates) and the anchor-overlap fallback
            # have enough body text to pick up document-type markers that
            # appear after court headers / addressee blocks. The LLM
            # tiebreaker explicitly trims this down via `snippet_chars`.
            content_head = extracted[:_CLASSIFY_CONTENT_CHARS]
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
        ready_size = len(ready)
        if not ready:
            return None, 0

        # ---- 1) Type-first match -----------------------------------------
        # Classify the suggestion in parallel with any lazy backfill of
        # un-classified templates in the library. Bound concurrent calls so
        # large libraries do not blow up the LLM quota on a single click.
        suggestion_payload = {
            "name": suggestion_title or "",
            "content_head": " ".join(
                s for s in (
                    suggestion_title,
                    suggestion_description,
                    fallback_text,
                ) if s
            )[:2000],
        }
        backfill_targets = [
            t for t in ready
            if not (t["payload"].get("document_type") or "").strip()
        ]
        classify_jobs: List[Any] = [
            self._classify_document_type(
                name=suggestion_payload["name"],
                content_head=suggestion_payload["content_head"],
                is_suggestion=True,
            )
        ]
        for t in backfill_targets:
            classify_jobs.append(
                self._classify_document_type(
                    name=t["name"],
                    content_head=t["content_head"],
                    is_suggestion=False,
                )
            )
        try:
            results = await asyncio.gather(*classify_jobs, return_exceptions=True)
        except Exception as e:
            # asyncio.gather with return_exceptions=True shouldn't normally
            # raise, but guard against unexpected runtime errors.
            logger.warning("Template matcher: classification batch raised: {}", e)
            results = [None] * len(classify_jobs)

        suggestion_class = results[0] if results else None
        if isinstance(suggestion_class, BaseException):
            suggestion_class = None
        backfill_results = results[1:] if len(results) > 1 else []

        # Persist any lazy backfill so the next click is fast.
        for t, res in zip(backfill_targets, backfill_results):
            if isinstance(res, BaseException) or not res:
                continue
            doc_type = res.get("documentType") or ""
            if not doc_type:
                continue
            updated = dict(t["payload"])
            updated["document_type"] = doc_type
            updated["document_type_aliases"] = res.get("aliases") or []
            updated["document_type_confidence"] = res.get("confidence") or ""
            updated["classified_at"] = datetime.utcnow().isoformat()
            t["payload"] = updated
            try:
                await vector_store.upsert_user_state_cache(
                    user_id=user_id,
                    kind=TEMPLATE_CACHE_KIND,
                    scope_id=t["templateId"],
                    data=updated,
                )
            except Exception as e:
                logger.warning(
                    "Template matcher: lazy classification backfill upsert "
                    "failed (template={}): {}",
                    t["templateId"],
                    e,
                )

        # Log a one-line summary of the classification round.
        sugg_type = (
            (suggestion_class or {}).get("documentType") or ""
        ) if suggestion_class else ""
        sugg_conf = (
            (suggestion_class or {}).get("confidence") or ""
        ) if suggestion_class else ""
        logger.info(
            "Template matcher classification (suggestion_type={!r}, "
            "confidence={!r}, library_types=[{}])",
            sugg_type or "(unclassified)",
            sugg_conf or "(n/a)",
            ", ".join(
                f"{t['payload'].get('document_type') or '?'}"
                for t in ready[:10]
            ),
        )

        type_match_payload = self._select_by_type(
            ready=ready,
            suggestion_class=suggestion_class,
        )
        if type_match_payload is not None and len(type_match_payload) == 1:
            chosen = type_match_payload[0]
            logger.info(
                "Drafting suggestion matched to template (user={}, template={}, "
                "mode=type-match, type={!r})",
                user_id[:12],
                chosen["templateId"],
                sugg_type,
            )
            return chosen["payload"], ready_size
        if type_match_payload is not None and len(type_match_payload) > 1:
            # Multiple templates share the suggestion's document type —
            # tiebreak with the LLM. 1500-char snippets give it enough body
            # to distinguish variants (e.g. Section 138 vs general dispute
            # notice).
            chosen = await self._llm_tiebreak_template(
                user_id=user_id,
                suggestion_title=suggestion_title,
                suggestion_description=suggestion_description,
                candidates=type_match_payload,
                snippet_chars=1500,
                mode_label="type-tiebreak",
            )
            if chosen is not None:
                return chosen["payload"], ready_size
            # LLM tiebreaker on the type-matched shortlist gave us nothing
            # usable; fall through to the anchor-overlap path below.

        # ---- 2) Anchor-overlap fallback ---------------------------------
        suggestion_anchors = self._anchor_tokens(suggestion_text)
        suggestion_phrases = self._anchor_phrases_in(suggestion_text)

        scored: List[Tuple[Dict[str, Any], int, int]] = []
        for t in ready:
            tpl_anchors = self._anchor_tokens(t["match_blob"])
            tpl_phrases = self._anchor_phrases_in(t["match_blob"])
            token_overlap = len(suggestion_anchors & tpl_anchors)
            phrase_overlap = len(suggestion_phrases & tpl_phrases)
            scored.append((t, token_overlap, phrase_overlap))

        # Sort by phrase overlap then token overlap, both descending.
        scored.sort(key=lambda r: (r[2], r[1]), reverse=True)

        logger.info(
            "Template matcher anchor-fallback candidates (ready={}, "
            "suggestion_anchors={}, suggestion_phrases={}):",
            ready_size,
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

        with_signal = [(t, tok, phr) for t, tok, phr in scored if (tok + phr) > 0]
        if not with_signal:
            logger.warning(
                "Drafting suggestion matcher: no template selected from "
                "non-empty library (size={}, suggestion_type={!r}) — "
                "falling back to AI generation",
                ready_size,
                sugg_type or "(unclassified)",
            )
            return None, ready_size

        if len(with_signal) == 1:
            entry, tok, phr = with_signal[0]
            logger.info(
                "Drafting suggestion matched to template (user={}, template={}, "
                "mode=fast-single, phrase_overlap={}, token_overlap={})",
                user_id[:12],
                entry["templateId"],
                phr,
                tok,
            )
            return entry["payload"], ready_size

        # Multiple candidates with anchor signal — tiebreak via LLM.
        candidates = [t for t, _tok, _phr in with_signal]
        chosen = await self._llm_tiebreak_template(
            user_id=user_id,
            suggestion_title=suggestion_title,
            suggestion_description=suggestion_description,
            candidates=candidates,
            snippet_chars=1500,
            mode_label="anchor-tiebreak",
        )
        if chosen is None:
            logger.warning(
                "Drafting suggestion matcher: LLM tiebreaker returned no "
                "match from {} anchor-overlapping candidate(s); falling "
                "back to AI generation",
                len(candidates),
            )
            return None, ready_size
        return chosen["payload"], ready_size

    def _select_by_type(
        self,
        *,
        ready: List[Dict[str, Any]],
        suggestion_class: Optional[Dict[str, Any]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Filter `ready` templates to those whose `document_type` matches
        the suggestion's classification. Returns the matched list (possibly
        size 1) or None when type-first matching does not apply (suggestion
        unclassified, classified as "Other", or no template carries the
        same type)."""
        if not suggestion_class:
            return None
        sugg_type = (suggestion_class.get("documentType") or "").strip()
        if not sugg_type or sugg_type.lower() == "other":
            return None
        # Aliases the model returned for the suggestion give us extra
        # vocabulary to match against template aliases (lawyer phrasings
        # captured at template-ingest time).
        sugg_aliases = {
            str(a).strip().lower()
            for a in (suggestion_class.get("aliases") or [])
            if str(a).strip()
        }
        sugg_type_lower = sugg_type.lower()
        matched: List[Dict[str, Any]] = []
        for t in ready:
            tpl_type = (t["payload"].get("document_type") or "").strip().lower()
            if not tpl_type:
                continue
            if tpl_type == sugg_type_lower:
                matched.append(t)
                continue
            tpl_aliases = {
                str(a).strip().lower()
                for a in (t["payload"].get("document_type_aliases") or [])
                if str(a).strip()
            }
            if sugg_aliases and (sugg_aliases & {tpl_type, *tpl_aliases}):
                matched.append(t)
                continue
            if sugg_type_lower in tpl_aliases:
                matched.append(t)
        return matched or None

    async def _llm_tiebreak_template(
        self,
        *,
        user_id: str,
        suggestion_title: str,
        suggestion_description: str,
        candidates: List[Dict[str, Any]],
        snippet_chars: int,
        mode_label: str,
    ) -> Optional[Dict[str, Any]]:
        """Send a candidate shortlist to the LLM tiebreaker and return the
        chosen candidate (or None if the LLM declined / returned an unknown
        id). Honours only ids that appear in `candidates`.
        """
        if not candidates:
            return None
        shortlist: List[Dict[str, Any]] = []
        for entry in candidates[:15]:
            shortlist.append(
                {
                    "templateId": entry["templateId"],
                    "name": entry["name"],
                    "category": entry["category"],
                    "contentSnippet": (entry.get("content_head") or "")[:snippet_chars],
                }
            )
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
                max_tokens=12000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning(
                "Template matcher LLM tiebreaker failed (mode={}): {}",
                mode_label,
                e,
            )
            return None
        parsed = _parse_json_object(raw) or {}
        chosen_id = (parsed.get("templateId") or "").strip()
        if not chosen_id or chosen_id.lower() == "null":
            logger.info(
                "Template matcher LLM returned no match (mode={}, reason={!r})",
                mode_label,
                (parsed.get("reason") or "")[:120],
            )
            return None
        chosen = next(
            (c for c in candidates if c["templateId"] == chosen_id), None
        )
        if chosen is None:
            logger.warning(
                "Template matcher LLM returned unknown templateId={!r} "
                "(mode={}) — ignoring",
                chosen_id,
                mode_label,
            )
            return None
        logger.info(
            "Drafting suggestion matched to template (user={}, template={}, "
            "mode={}, reason={!r})",
            user_id[:12],
            chosen_id,
            mode_label,
            (parsed.get("reason") or "")[:120],
        )
        return chosen

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

        # Greeting fast-path — runs BEFORE any drafting-state branch.
        # A bare "hi" / "hello" / "good morning" must always route to
        # chat, regardless of session state, `source` signals, or
        # workspace case context. The frontend has been observed
        # auto-sending `source="blank"` whenever the drafting panel
        # opens; without this guard a greeting would silently fire a
        # full kickoff and the model would auto-infer a document type
        # from case context (e.g. an NDPS-bail case would always
        # produce a Bail Application scoping prompt for "hi"). The
        # only exception is when the lawyer explicitly clicked a
        # template or a suggested draft — `templateId` /
        # `suggestedDraftId` is unambiguous drafting intent and is
        # honoured even if the typed message looks like a greeting.
        has_explicit_template = bool(
            request_fields.get("templateId")
            or request_fields.get("suggestedDraftId")
        )
        if self._is_greeting(user_message) and not has_explicit_template:
            session_state = await self._handle_chat_question(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state or {},
                conversation_history=conversation_history,
            )
        else:
            # Explicit-action kickoff is only triggered by an unambiguous
            # signal: a template upload (`templateId`) or a suggested-draft
            # click (`suggestedDraftId`). `source="blank"` alone is too
            # weak — the frontend may send it on panel open, which would
            # otherwise force a kickoff on every fresh chat.
            explicit_kickoff = not session_state and has_explicit_template

            if explicit_kickoff:
                session_state = await self._handle_kickoff(
                    user_message=user_message,
                    case_context=case_context,
                    request_fields=request_fields,
                )
            elif (session_state or {}).get("incompatible"):
                # Incompatible-template flow — _handle_followup routes to
                # _handle_incompatible_reply at its top, where the
                # deterministic option-letter pre-check runs first and the
                # LLM intent classifier is the backstop. Keeping this
                # branch explicit so the generic intent classifier below
                # never overrides the incompatibility flow.
                session_state = await self._handle_followup(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state or {},
                    request_fields=request_fields,
                    conversation_history=conversation_history,
                )
            elif (session_state or {}).get("draft_status") == "gathering_info":
                # Strict gathering_info short-circuit: route the turn
                # directly to `_handle_followup`, bypassing the LLM
                # intent classifier entirely. The classifier was
                # observed mis-classifying structured scoping-answer
                # replies as `chat_question` (because of formal text
                # style), which then routed to `_handle_chat_question`
                # and emitted a misleading "I have everything I need
                # to finalize" reply without ever rendering the draft.
                # In this state the followup handler is the single
                # source of truth: it maps answers, renders when slots
                # are filled, honours the render-now ack shortcut, and
                # otherwise re-emits the structured remaining questions.
                session_state = await self._handle_followup(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state or {},
                    request_fields=request_fields,
                    conversation_history=conversation_history,
                )
            elif (
                (session_state or {}).get("draft_status") == "ready"
                and self._is_post_draft_acknowledgement(user_message)
            ):
                # Post-draft acknowledgement fast-path — deterministic,
                # free, and stable. Skips the classifier so a bare "ok" /
                # "thanks" never costs an LLM round-trip and never risks
                # misroute.
                session_state["last_assistant_message"] = (
                    "Glad the draft looks good. Let me know any time if "
                    "you would like edits — for example, adjusting the "
                    "prayer, tightening the grounds, or adding a clause."
                )
                session_state["open_questions"] = []
                # draft_status stays "ready"; last_draft_document untouched.
            elif (
                (session_state or {}).get("draft_status") == "ready"
                and (session_state or {}).get("last_draft_markdown")
                and self._extract_translate_target(user_message) is not None
            ):
                # Translate fast-path — the lawyer asked to translate the
                # delivered draft into a target language. Routes directly
                # to `_handle_translate_draft` which preserves every
                # structural element and only swaps the natural-language
                # text. The classifier never sees this turn, so it cannot
                # mis-route to a chat reply or a side-suggestion.
                target_language = self._extract_translate_target(user_message)
                session_state = await self._handle_translate_draft(
                    target_language=target_language or "Hindi",
                    case_context=case_context,
                    session_state=session_state or {},
                )
            else:
                # Per-turn intent classifier: route everything else
                # through a single Gemini call so free-form drafting
                # requests, mid-flow draft switches, scoping answers,
                # revisions, and plain chat / legal Q&A all reach the
                # right handler.
                session_state = await self._dispatch_classified_turn(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state or {},
                    request_fields=request_fields,
                    conversation_history=conversation_history,
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

    async def _dispatch_classified_turn(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        request_fields: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Route a non-kickoff, non-incompatible, non-ack turn by intent.

        Calls the per-turn intent classifier and dispatches to: the
        kickoff helper (free-form draft request or mid-flow switch),
        the revision/followup handlers (when the state matches), or the
        soft chat-reply handler (default — never fires a draft).
        """
        # Deterministic greeting fast-path — runs BEFORE the LLM. A bare
        # "hi" / "hello" / "good morning" should never trigger drafting,
        # but the LLM was observed inferring `draft_request` from case
        # context (workspace already had bail-application data, so it
        # decided the lawyer must want a bail application). This regex
        # check is free and instant; greetings always route to chat.
        if self._is_greeting(user_message):
            return await self._handle_chat_question(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state,
                conversation_history=conversation_history,
            )

        # Deterministic ordinal-reference resolver — when the assistant's
        # last list-bearing message offered a numbered/bulleted list of
        # documents and the lawyer's reply is "do the third one" /
        # "option 2" / "3rd please", short-circuit straight to the
        # `draft_request` route with the resolved document type. The LLM
        # classifier mis-routes these as `chat_question` because the
        # message names no document explicitly.
        resolved_doc_type = self._resolve_ordinal_reference(
            user_message, conversation_history
        )
        if resolved_doc_type:
            req = dict(request_fields)
            req["source"] = "blank"
            req["templateId"] = None
            req["suggestedDraftId"] = None
            req["suggestionTitle"] = resolved_doc_type
            return await self._handle_kickoff(
                user_message=user_message,
                case_context=case_context,
                request_fields=req,
            )

        # Affirmation-after-list auto-pick — when the assistant's most
        # recent message listed draft options and the lawyer replies
        # with a bare "yes" / "ok" / "sure" / "go ahead" (no ordinal,
        # no doc name), default to picking option #1. This handles the
        # observed flow where the AI lists 3 options, the lawyer says
        # "ok", and we want drafting to actually start instead of
        # falling through to chat. The lawyer can always correct with
        # "the third one" on a later turn — the ordinal resolver
        # catches that. `_is_render_now_reply` already covers the exact
        # affirmation vocabulary we want ("ok" / "yes" / "sure" /
        # "proceed" / "go ahead" / "do it" / "let's go" / …) so we
        # reuse it rather than duplicating the phrase set.
        if self._is_render_now_reply(user_message):
            list_items = self._extract_list_items_from_history(
                conversation_history
            )
            if list_items:
                first_doc_type = self._clean_list_item_to_doc_type(
                    list_items[0]
                )
                if first_doc_type:
                    req = dict(request_fields)
                    req["source"] = "blank"
                    req["templateId"] = None
                    req["suggestedDraftId"] = None
                    req["suggestionTitle"] = first_doc_type
                    return await self._handle_kickoff(
                        user_message=user_message,
                        case_context=case_context,
                        request_fields=req,
                    )

        intent_info = await self._classify_drafting_turn_intent(
            user_message=user_message,
            session_state=session_state,
            conversation_history=conversation_history,
        )
        label = intent_info.get("intent") or "chat_question"
        doc_type = (intent_info.get("document_type") or "").strip()
        current_status = (session_state.get("draft_status") or "").strip()

        if label == "draft_request":
            # Free-form drafting trigger (or mid-flow switch). Route via
            # the existing kickoff path with `source="blank"` and the
            # inferred document type as the suggestion title — this
            # drives `_handle_kickoff` into the no-template branch which
            # delegates to `_scope_blank_draft`. The lawyer's full
            # message is passed as `lawyer_instructions` so the scoping
            # call sees the raw intent verbatim.
            req = dict(request_fields)
            req["source"] = "blank"
            req["templateId"] = None
            req["suggestedDraftId"] = None
            if doc_type:
                req["suggestionTitle"] = doc_type
            return await self._handle_kickoff(
                user_message=user_message,
                case_context=case_context,
                request_fields=req,
            )

        if label == "revision_request" and current_status == "ready":
            return await self._handle_revision(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state,
            )

        if label == "scoping_answer" and current_status == "gathering_info":
            return await self._handle_followup(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state,
                request_fields=request_fields,
                conversation_history=conversation_history,
            )

        # Default — chat / Q&A / greeting / out-of-state intent / safe
        # fallback when the classifier returned a label that does not
        # match the current draft_status (e.g. "revision_request" while
        # status is still "gathering_info"). Replying conversationally
        # never costs the lawyer progress; a wrongly-fired draft would.
        return await self._handle_chat_question(
            user_message=user_message,
            case_context=case_context,
            session_state=session_state,
            conversation_history=conversation_history,
        )

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
        # When the suggested-draft path runs but no template matches AND
        # the library is non-empty, surface a short explanatory prefix on
        # the assistant's first reply so the lawyer learns why the upload
        # they expected to be reused wasn't picked.
        no_match_prefix: Optional[str] = None
        ready_library_size = 0
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
            template_payload, ready_library_size = await self._find_matching_template_for_suggestion(
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
            elif ready_library_size > 0:
                # Library has usable templates but none was a close enough
                # match. Tell the lawyer plainly so they can either pick a
                # template directly or upload a closer one.
                pretty_title = (suggestion_title or "this draft").strip() or "this draft"
                no_match_prefix = (
                    f"I couldn't find a close match in your Template Library "
                    f"for *{pretty_title}*, so I'm drafting this from "
                    f"scratch. If you have a closer template, upload it or "
                    f"open it directly from the library."
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
                max_tokens=12000,
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
        return await self._scope_blank_draft(
            role=role,
            case_summary=case_summary,
            case_facts=case_facts,
            source=source,
            lawyer_instructions=lawyer_instructions,
            suggestion_title=suggestion_title,
            suggested_draft_id=request_fields.get("suggestedDraftId"),
            no_match_prefix=no_match_prefix,
        )

    async def _scope_blank_draft(
        self,
        *,
        role: str,
        case_summary: str,
        case_facts: str,
        source: str,
        lawyer_instructions: Optional[str],
        suggestion_title: Optional[str],
        suggested_draft_id: Optional[str],
        no_match_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Produce a fresh, template-less scoping session_state.

        Used by the kickoff path when no template is in play AND by the
        incompatible-template `proceed_from_scratch` recovery — the lawyer
        chose to drop the incompatible template and draft from the case
        context, so we generate scoping questions for the document type.
        """
        draft_title = suggestion_title or "New Draft"
        if source == "blank" and lawyer_instructions:
            draft_title = lawyer_instructions[:80]
        scoping_prompt = build_template_kickoff_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            template_name="(no template — start from scratch)",
            template_text=(
                "(no template was provided. You MUST still produce the "
                "structured kickoff layout — the `message` field must "
                "contain BOTH `**Key Case Details Automatically "
                "Integrated:**` and `**Information Required to Complete "
                "the Draft:**` headers verbatim, with the auto-filled "
                "facts under the first header and AT LEAST THREE numbered "
                "titled scoping questions under the second header. The "
                "document type to draft is the DRAFT TITLE HINT above; "
                "infer the standard Indian-advocate skeleton for that "
                "document type — court heading, party array, grounds, "
                "prayer, verification — and produce the open questions "
                "needed to fill its case-variable slots. Do NOT reply "
                "with a single open-ended question or a chat-style "
                "paragraph; the structured layout is mandatory even "
                "when the requested document type is unusual.)"
            ),
            lawyer_instructions=lawyer_instructions,
            draft_title_hint=suggestion_title,
        )
        raw = await self._safe_llm_json(
            prompt=scoping_prompt,
            system_instruction=TEMPLATE_KICKOFF_INSTRUCTION,
            temperature=0.4,
            max_tokens=12000,
        )
        parsed = _parse_json_object(raw) or {}
        message = (parsed.get("message") or "").strip()
        open_qs = self._normalize_open_questions(parsed.get("openQuestions"))
        gathered = parsed.get("gatheredFacts")
        if not isinstance(gathered, dict):
            gathered = {}
        # Malformed-output guard: the prompt mandates the
        # `**Information Required to Complete the Draft:**` header. If
        # the LLM returned a non-empty `message` that does NOT contain
        # that header (e.g. drifted to a single-question chat reply for
        # an unusual doc type), fall back to the deterministic
        # structured composer so the lawyer always sees a numbered
        # question block.
        if not message or "Information Required to Complete the Draft" not in message:
            message = self._compose_questions_message(open_qs, draft_title)
        # Prepend the "no template matched" note when the kickoff
        # decided the lawyer should be told their library was searched
        # but nothing fit.
        if no_match_prefix:
            message = f"{no_match_prefix}\n\n{message}"

        return {
            "source": source or "blank",
            "template_id": None,
            "template_name": None,
            "suggested_draft_id": suggested_draft_id,
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
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Update gathered facts from the user's message; ask remaining questions or render."""
        # Incompatibility guard — when the kickoff flagged the chosen template
        # as fundamentally incompatible with the case, the lawyer's next reply
        # is a routing choice (proceed from scratch / change template /
        # cancel) OR a clarifying question about the options. Either way it
        # is NOT an answer to scoping questions, and falling through to the
        # render path here would silently produce a draft on a bare "ok".
        if session_state.get("incompatible"):
            return await self._handle_incompatible_reply(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state,
                request_fields=request_fields,
                conversation_history=conversation_history,
            )

        open_qs = list(session_state.get("open_questions") or [])
        gathered = dict(session_state.get("gathered_facts") or {})
        gathered_before = dict(gathered)
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
        gathered_anything_new = gathered != gathered_before
        session_state["gathered_facts"] = gathered
        session_state["open_questions"] = remaining

        if remaining:
            # Render-now shortcut: when the lawyer's message is an ack /
            # forward-action instruction ("ok", "proceed", "go ahead",
            # "draft it") AND no new facts were extracted this turn,
            # the lawyer is asking us to finalise with what we have.
            # The render prompt already emits `<placeholder>` labels for
            # any unanswered slots — see CONSTRAINTS in
            # `build_draft_render_prompt`. This breaks the "static
            # repeat" loop where the bot would re-ask the same scoping
            # questions on every "ok" turn.
            if (
                not gathered_anything_new
                and self._is_render_now_reply(user_message)
            ):
                return await self._render_and_attach(
                    case_context=case_context,
                    session_state=session_state,
                    lawyer_instructions=(user_message or "").strip() or None,
                    mode_override=None,
                )
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

    # Vocabulary used to detect "render now / proceed with what you
    # have" intent mid-scoping. Reused inside `_handle_followup` so a
    # bare "ok" / "proceed" / "go ahead" breaks the question-loop and
    # emits the draft with `<placeholder>` labels for missing slots.
    # The post-draft `_is_post_draft_acknowledgement` is also accepted
    # so any of "ok", "thanks", "looks good" trigger the shortcut.
    _RENDER_NOW_PHRASES = frozenset(
        {
            "proceed", "go ahead", "draft it", "draft it now",
            "render it", "render now", "just do it", "do it",
            "make it", "make the draft", "create the draft",
            "finish", "finalize", "finalise", "wrap it up",
            "go for it", "let's go", "lets go",
        }
    )

    @classmethod
    def _is_render_now_reply(cls, text: str) -> bool:
        """True iff the message is "render now / proceed" intent.

        Accepts:
          - Any reply caught by `_is_post_draft_acknowledgement`
            ("ok" / "thanks" / "looks good" / "yes" / ...).
          - Any tight render-now phrase from `_RENDER_NOW_PHRASES`.
        """
        raw = (text or "").strip()
        if not raw:
            return False
        if cls._is_post_draft_acknowledgement(raw):
            return True
        normalised = cls._normalize_ack_text(raw)
        return normalised in cls._RENDER_NOW_PHRASES

    async def _handle_incompatible_reply(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        request_fields: Dict[str, Any],
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Route the lawyer's reply after the kickoff flagged the template as incompatible.

        The kickoff asked: (a) draft from a fresh structure, (b) upload a
        different template, or (c) cancel. This handler classifies the reply
        and dispatches. Drafting never renders from here — it only resumes
        once the lawyer has chosen (a) AND answered the fresh scoping
        questions on a subsequent turn.

        When the lawyer asks a clarifying question or pushes back on a
        suggestion ("what are the other options?", "no, not criminal
        complaint"), the handler delegates to a contextual chat reply
        instead of re-emitting the canned options block — robotic
        repetition is what was making the bot feel like a static FAQ
        rather than a real assistant.
        """
        history = conversation_history or []
        text = (user_message or "").strip()
        draft_title = session_state.get("draft_title") or "New Draft"

        # Deterministic option-letter pre-check — runs FIRST. A bare "a" /
        # "(a)" / "option a" reply is unambiguously a routing choice; the
        # LLM classifier was observed mis-reading single-letter "a" as the
        # English article and defaulting to acknowledge_only. This regex
        # check is free, instant, and reliable for the common path.
        inferred_doc_type = ""
        option_intent = self._classify_option_letter_reply(text)
        if option_intent is not None:
            intent = option_intent
        elif not text or self._is_post_draft_acknowledgement(text):
            # Fast-path: pure acknowledgement (empty / "ok" / "thanks" / ...).
            # Reuse the existing detector so the vocabulary stays consistent
            # with the post-draft acknowledgement path and saves an LLM
            # round-trip on the most common reply.
            intent = "acknowledge_only"
        else:
            classifier_result = await self._classify_incompatible_intent(text)
            intent = classifier_result.get("intent") or "clarifying_question"
            inferred_doc_type = (classifier_result.get("document_type") or "").strip()

        if intent == "proceed_from_scratch":
            role = (case_context.get("role") or "ADJUDICATOR").upper()
            case_summary = case_context.get("case_summary") or ""
            case_facts = case_context.get("case_facts_brief") or ""
            # If the lawyer named a specific document type (e.g. "draft
            # the legal notice"), use that as the new suggestion title so
            # the scoping flow produces questions for the right document.
            # Otherwise fall back to the existing draft title (which came
            # from the original kickoff suggestion / option-(a) path).
            suggestion_title = (
                inferred_doc_type
                or session_state.get("suggestion_title")
                or draft_title
            )
            # Pass the lawyer's free-text reply as `lawyer_instructions`
            # only when they explicitly named the document — that way the
            # scoping prompt sees the verbatim intent and tailors the
            # questions to it. For option-letter / bare-affirmative paths,
            # there is no useful free-text instruction to forward.
            lawyer_instructions = text if inferred_doc_type else None
            new_state = await self._scope_blank_draft(
                role=role,
                case_summary=case_summary,
                case_facts=case_facts,
                source="blank",
                lawyer_instructions=lawyer_instructions,
                suggestion_title=suggestion_title,
                suggested_draft_id=session_state.get("suggested_draft_id"),
            )
            # Preserve session identity so persistence updates the same row.
            for k in ("actionable_id", "user_id", "workspace_id", "created_at"):
                if session_state.get(k):
                    new_state[k] = session_state[k]
            return new_state

        if intent == "change_template":
            session_state["template_id"] = None
            session_state["template_name"] = None
            session_state["incompatible"] = False
            session_state["draft_status"] = "analyzing"
            session_state["open_questions"] = []
            session_state["last_assistant_message"] = (
                "Understood. Please upload a template that fits this "
                "matter (for example, a Bail Application format from your "
                "library) and I will analyse it against the case context."
            )
            return session_state

        if intent == "cancel":
            session_state["template_id"] = None
            session_state["template_name"] = None
            session_state["incompatible"] = False
            session_state["draft_status"] = "gathering_info"
            session_state["open_questions"] = []
            session_state["last_assistant_message"] = (
                "Drafting session cancelled. Let me know any time when "
                "you would like to start a new draft."
            )
            return session_state

        if intent == "clarifying_question":
            # The lawyer is engaging with the choice — asking what an
            # option means, pushing back on a suggestion, exploring
            # alternatives. Route to a contextual chat reply that
            # actually responds to what they said and ends with a soft
            # re-offer of (a)/(b)/(c). The deterministic letter pre-check
            # still fires on their NEXT turn so picking an option remains
            # instant.
            return await self._handle_incompatible_clarification(
                user_message=text,
                case_context=case_context,
                session_state=session_state,
                conversation_history=history,
            )

        # acknowledge_only (fast-path only — pure "ok" / "thanks" etc.).
        # The LLM no longer returns this label; the deterministic ack
        # check is the sole route into this branch, so a one-line
        # restatement is appropriate.
        session_state["draft_status"] = "analyzing"
        session_state["open_questions"] = []
        session_state["last_assistant_message"] = (
            self._build_incompatible_options_message(draft_title)
        )
        return session_state

    async def _handle_incompatible_clarification(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Conversational reply when the lawyer asks about the options.

        Uses `DRAFTING_CHAT_INSTRUCTION` so the reply is chat-shaped (no
        court scaffolding) and uses `build_chat_reply_prompt` for the
        case context + recent transcript. A task-block addendum tells
        the model about the incompatible state, the three options, and
        the template name, and asks it to engage with the lawyer's
        message and close with a brief re-offer of choices — without
        copy-pasting the canned options block.

        State stays `incompatible=True` and `draft_status="analyzing"`
        — drafting still requires the lawyer to pick (a)/(b)/(c) on a
        later turn.
        """
        role = (case_context.get("role") or "ADJUDICATOR").upper()
        case_summary = case_context.get("case_summary") or ""
        case_facts = case_context.get("case_facts_brief") or ""
        template_name = (session_state.get("template_name") or "(uploaded template)").strip()
        draft_title = session_state.get("draft_title") or "this matter"

        base_prompt = build_chat_reply_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            conversation_history=conversation_history,
            user_message=user_message,
        )
        addendum = (
            "\n\nADDITIONAL CONTEXT FOR THIS REPLY:\n"
            f"You previously diagnosed the uploaded template "
            f"'{template_name}' as fundamentally incompatible with this "
            f"matter (intended draft: {draft_title}) and offered three "
            "options:\n"
            "  (a) Draft from a fresh structure using the case context — "
            "the uploaded template is set aside.\n"
            "  (b) Upload a different template that fits this matter.\n"
            "  (c) Cancel this drafting session.\n\n"
            "The lawyer has now sent the message shown above. They have "
            "NOT yet picked an option — they are asking a question, "
            "exploring alternatives, pushing back on a suggestion, or "
            "expressing some other concern.\n\n"
            "Reply naturally, like a real assistant: \n"
            "  - Address what they actually said. If they asked what an "
            "option means, explain it. If they rejected a document type "
            "(e.g. \"not a criminal complaint\"), suggest 2–3 concrete "
            "document alternatives that fit the case (legal/demand "
            "notice, civil plaint, affidavit, written statement, "
            "anticipatory bail application, etc.).\n"
            "  - Do NOT copy-paste the original options block verbatim. "
            "Vary the wording. Keep it short and human.\n"
            "  - END the reply with a brief, single-paragraph re-offer "
            "of the choice — phrase it differently each time. For "
            "example: \"Just tell me which of these you'd like and I'll "
            "start — or upload a fitting template, or say 'cancel' if "
            "you'd rather stop here.\"\n"
            "  - Do NOT produce any court-filing scaffolding (no court "
            "heading, no \"MOST RESPECTFULLY SHOWETH\", no signature "
            "block). This is a chat turn, not a draft."
        )
        prompt = base_prompt + addendum

        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=DRAFTING_CHAT_INSTRUCTION,
                temperature=0.5,
                max_tokens=12000,
            )
        except Exception as e:
            logger.error("Incompatible-clarification LLM call failed: {}", e)
            session_state["draft_status"] = "analyzing"
            session_state["open_questions"] = []
            session_state["last_assistant_message"] = (
                self._build_incompatible_options_message(draft_title)
            )
            return session_state

        reply = _strip_code_fences(raw).strip()
        if not reply:
            session_state["draft_status"] = "analyzing"
            session_state["open_questions"] = []
            session_state["last_assistant_message"] = (
                self._build_incompatible_options_message(draft_title)
            )
            return session_state

        # Stay in the incompatible/analyzing state — the lawyer has not
        # yet chosen (a)/(b)/(c). The deterministic letter pre-check
        # will fire instantly on their next turn if they pick one.
        session_state["draft_status"] = "analyzing"
        session_state["open_questions"] = []
        session_state["last_assistant_message"] = reply
        return session_state

    _OPTION_INTENT_BY_LETTER = {
        "a": "proceed_from_scratch",
        "b": "change_template",
        "c": "cancel",
    }

    @classmethod
    def _classify_option_letter_reply(cls, text: str) -> Optional[str]:
        """Deterministic match for the lawyer picking option (a) / (b) / (c).

        Returns the corresponding intent, or None if the reply does not
        look like an option-letter selection. Runs BEFORE the LLM
        classifier in `_handle_incompatible_reply` because the LLM has
        been observed to mis-read a bare "a" as the English article and
        default to `acknowledge_only`. Matches:
            "a"  "A"  "(a)"  "a)"  "a."  "a:"
            "option a"  "choice a"  "select a"
            "a - draft from scratch"  "a, draft from scratch"
            "go with a"  "let's go with a"  "pick a"
        Anything more complex is left to the LLM.
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 80:
            return None
        # Lowercase and strip surrounding punctuation / quotes / brackets.
        t = re.sub(r"^[\s\"'`(\[{]+", "", raw.lower())
        t = re.sub(r"[\s\"'`)\]}.,!?:;]+$", "", t)
        if not t:
            return None
        # Pure single letter — most common path.
        if t in cls._OPTION_INTENT_BY_LETTER:
            return cls._OPTION_INTENT_BY_LETTER[t]
        # "option a" / "choice a" / "select a" / "pick a" / "go with a" /
        # "let's go with a" / "i pick a" — match a single trailing letter.
        m = re.match(
            r"^(?:option|choice|select|pick|go with|let's go with|i pick|"
            r"i choose|i'll go with|go for)\s+\(?([abc])\)?$",
            t,
        )
        if m:
            return cls._OPTION_INTENT_BY_LETTER[m.group(1)]
        # "a - draft from scratch" / "a) draft from scratch" / "a, draft" —
        # the letter is the first token followed by a separator (`)`, `-`,
        # `—`, `,`, or `:`) and then an explanatory phrase. The `)` is
        # included in the separator class itself so the optional opening
        # `(` (already stripped by the lead-strip above for the "(a) …"
        # variant) does not need a matching closing `)`.
        m = re.match(r"^\(?([abc])\s*[-—,:)]\s*\S", t)
        if m:
            return cls._OPTION_INTENT_BY_LETTER[m.group(1)]
        return None

    # Greeting vocabulary used to short-circuit the per-turn intent
    # classifier. The LLM was observed mis-classifying a bare "hi" as a
    # draft_request because the workspace case context contained
    # drafting-relevant data — it inferred "they probably want to draft
    # the case I see in context". A bare greeting must NEVER infer
    # drafting intent; it routes straight to the chat handler.
    _GREETING_TOKENS = frozenset(
        {
            "hi", "hii", "hiii", "hello", "hellow", "hey", "heyy", "hola",
            "yo", "sup", "howdy", "namaste", "namaskar", "salaam",
            "good", "morning", "afternoon", "evening", "night",
            "greetings", "gm", "ga", "gn",
        }
    )

    # Tokens that may legitimately appear alongside a greeting without
    # turning it into a drafting request: "hi there", "hello team",
    # "good morning ji", "hey assistant", etc. We do NOT include any
    # verbs here — "hi draft" would NOT match.
    _GREETING_ATTENDANTS = frozenset(
        {
            "there", "ji", "sir", "madam", "team", "assistant", "ai",
            "bot", "all", "everyone", "folks", "buddy", "friend",
        }
    )

    @classmethod
    def _is_greeting(cls, text: str) -> bool:
        """True iff the message is a bare greeting / salutation.

        Matches "hi", "hello", "hey", "good morning", "namaste",
        "hi there", "good evening ji", etc. Anything longer than a short
        salutation, or containing tokens outside the greeting vocabulary,
        is left to the LLM. This is intentionally narrow — when in doubt
        return False; the LLM still has the safe default of
        chat_question.
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 40:
            return False
        # Drop emoji and punctuation, keep letters / digits / whitespace.
        cleaned = re.sub(r"[^\w\s']", " ", raw.lower(), flags=re.UNICODE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return False
        tokens = [t for t in cleaned.split() if t]
        if not tokens:
            return False
        # All tokens must be greetings or attendants. Anything else
        # ("draft", "fir", "complaint", a question word) makes this not
        # a pure greeting and falls through to the LLM.
        return all(
            tok in cls._GREETING_TOKENS or tok in cls._GREETING_ATTENDANTS
            for tok in tokens
        )

    # ------------------------------------------------------------------
    # Translate-intent detector — handles "translate in hindi" / "in
    # marathi please" / "hindi" replies. Runs after a draft has been
    # delivered (draft_status == "ready") so we can route them to the
    # dedicated translation handler rather than a chat or revision turn.
    # ------------------------------------------------------------------

    _TRANSLATABLE_LANGUAGES: Dict[str, str] = {
        # Indian languages (lowercase trigger -> canonical English name)
        "hindi": "Hindi",
        "marathi": "Marathi",
        "bengali": "Bengali",
        "bangla": "Bengali",
        "tamil": "Tamil",
        "telugu": "Telugu",
        "gujarati": "Gujarati",
        "punjabi": "Punjabi",
        "kannada": "Kannada",
        "malayalam": "Malayalam",
        "urdu": "Urdu",
        "odia": "Odia",
        "oriya": "Odia",
        "assamese": "Assamese",
        "sanskrit": "Sanskrit",
        "english": "English",
        # Common international languages (kept small intentionally)
        "french": "French",
        "spanish": "Spanish",
        "german": "German",
        "arabic": "Arabic",
        "mandarin": "Mandarin Chinese",
        "chinese": "Mandarin Chinese",
        "portuguese": "Portuguese",
        "russian": "Russian",
        "japanese": "Japanese",
    }

    # Verbs and nouns that signal a translation request.
    _TRANSLATE_VERBS = frozenset(
        {"translate", "translation", "convert", "render", "rewrite"}
    )

    @classmethod
    def _extract_translate_target(cls, text: str) -> Optional[str]:
        """Return canonical target-language name for a translate request.

        Returns None when the message is not a translate request, or when
        no language in `_TRANSLATABLE_LANGUAGES` is mentioned. Conservative
        on length: longer messages without an explicit translate-verb are
        rejected so phrases like "the hindi name of the accused is …"
        do NOT trigger translation.

        Matches:
            "translate in hindi"           "translate to marathi"
            "translate this to tamil"      "translate it into bengali"
            "convert to hindi"             "rewrite in tamil"
            "in hindi please"              "hindi translation"
            "hindi version"                "hindi"  (short standalone)
            "translate back to english"    "back to english"
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 120:
            return None
        # Lowercase + drop punctuation; keep letters/digits/whitespace.
        cleaned = re.sub(r"[^\w\s'-]", " ", raw.lower(), flags=re.UNICODE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return None
        tokens = [t for t in cleaned.split() if t]
        if not tokens:
            return None

        # Find the first language mention.
        lang_tokens = [t for t in tokens if t in cls._TRANSLATABLE_LANGUAGES]
        if not lang_tokens:
            return None
        target = cls._TRANSLATABLE_LANGUAGES[lang_tokens[0]]

        # Strict path: any explicit translate-verb anywhere → accept.
        if any(t in cls._TRANSLATE_VERBS for t in tokens):
            return target

        # Short-message path: ≤ 4 tokens, all of them either the
        # language, "in"/"to"/"into", "please"/"please"/"version"/
        # "back"/"now". Catches "hindi", "in hindi", "hindi please",
        # "back to english".
        if len(tokens) <= 4:
            allowed = (
                set(cls._TRANSLATABLE_LANGUAGES.keys())
                | {
                    "in", "to", "into", "as", "please", "version",
                    "back", "now", "the", "language", "translation",
                }
            )
            if all(t in allowed for t in tokens):
                return target

        # Otherwise the language token appears inside a longer message
        # without a translate verb — reject (e.g. "the hindi name of
        # the accused is Ravi").
        return None

    # ------------------------------------------------------------------
    # Ordinal-reference resolver — handles replies like "do the third
    # one" / "option 2" / "3rd please" that pick an item from a numbered
    # or bulleted list the assistant just produced. Runs BEFORE the LLM
    # classifier so the common case never costs a round-trip and never
    # mis-routes to chat_question.
    # ------------------------------------------------------------------

    # Only true ordinal forms here. Cardinals ("one", "two", ...) are
    # deliberately omitted because they collide with positional uses of
    # "one" ("the third one", "first one", "do the 2nd one") and
    # generic uses ("any of them is fine, do one"). Cardinal-only
    # picks like "ok 3" still route via the digit-only fallback below.
    _ORDINAL_WORD_TO_INDEX = {
        "first": 1, "1st": 1,
        "second": 2, "2nd": 2,
        "third": 3, "3rd": 3,
        "fourth": 4, "4th": 4,
        "fifth": 5, "5th": 5,
        "sixth": 6, "6th": 6,
        "seventh": 7, "7th": 7,
        "eighth": 8, "8th": 8,
        "ninth": 9, "9th": 9,
        "tenth": 10, "10th": 10,
    }

    @classmethod
    def _extract_ordinal_index(cls, text: str) -> Optional[int]:
        """Return the 1-based list position the user is picking, or None.

        Matches single-list-item picks like:
            "do the third one"     "the 3rd one"      "3rd please"
            "option 2"             "number 2"         "#3"
            "let's do the third"   "go with #3"       "pick option 4"
            "ok the second"        "yes, option 1"    "last one"
        Anything more complex (multiple ordinals, or a sentence with
        unrelated drafting verbs) is left to the LLM.
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 120:
            return None
        # Lowercase, strip outer quotes/brackets, collapse whitespace.
        t = re.sub(r"^[\s\"'`(\[{]+", "", raw.lower())
        t = re.sub(r"[\s\"'`)\]}.,!?:;]+$", "", t)
        if not t:
            return None
        # Strip leading filler acknowledgements so "ok do the third one"
        # reduces to "do the third one".
        t = re.sub(
            r"^(?:ok(?:ay)?|sure|yes|yeah|yep|alright|right)[\s,]+",
            "",
            t,
        )

        # "last" → the last item in the list. We resolve that against
        # the actual list length at the call site (return a sentinel
        # negative number that the caller maps to len(items)).
        if re.fullmatch(r"(?:the\s+)?last(?:\s+one)?", t):
            return -1  # sentinel: last
        if re.fullmatch(r"(?:do|pick|select|take|use|go\s+with)?\s*(?:the\s+)?last(?:\s+one)?", t):
            return -1

        # "#3" / "no. 3" / "no 3" / "number 3" / "option 3" / "choice 3"
        # Allow the keyword to appear anywhere — `#` does not produce a
        # `\b` after whitespace, so use `(?:^|\s|\b)` instead.
        m = re.search(
            r"(?:^|\s|\b)(?:#|no\.?|number|option|choice|pick|item)\s*(\d{1,2})\b",
            t,
        )
        if m:
            n = int(m.group(1))
            if 1 <= n <= 10:
                return n

        # Word/ordinal form anywhere in a short message:
        # "third", "the third one", "let's do the third", "go for fifth".
        for word, idx in cls._ORDINAL_WORD_TO_INDEX.items():
            # Word-boundary match so "first" does not match inside
            # "firstly". Already lowercase by this point.
            if re.search(rf"\b{re.escape(word)}\b", t):
                # Reject if the message also mentions an explicit
                # alternative number — ambiguous.
                if sum(
                    1 for w in cls._ORDINAL_WORD_TO_INDEX
                    if re.search(rf"\b{re.escape(w)}\b", t)
                ) > 1:
                    return None
                return idx

        # Bare standalone digit like "3" or "ok 3".
        m = re.fullmatch(r"(?:do\s+|pick\s+|select\s+|take\s+)?(\d{1,2})", t)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 10:
                return n
        return None

    # Numbered-list line patterns we recognise in assistant history:
    #   "1. Item title — desc"   "1) Item title"   "*   **Title:** desc"
    #   "- Title: desc"          "**1. Title:** desc"
    _LIST_LINE_PATTERNS = [
        # "**1. Title:** ..." (Markdown bold-numbered titles)
        re.compile(r"^\s*\*\*(\d{1,2})\.\s+([^*]+?)\*\*\s*(.*)$"),
        # "1. Title — ..."  "1. Title: ..."  "1) Title …"
        re.compile(r"^\s*(\d{1,2})[.)]\s+(.+)$"),
        # "* Title — ..." or "- Title — ..." (bulleted lists)
        re.compile(r"^\s*[*\-]\s+(.+)$"),
    ]

    @classmethod
    def _extract_list_items_from_history(
        cls,
        conversation_history: List[Dict[str, Any]],
    ) -> List[str]:
        """Return the bulleted / numbered items from the assistant's
        most recent message that contains a list, in order.

        Each item is the raw text after the list marker. Returns [] if
        no recent assistant message contains a list.
        """
        if not conversation_history:
            return []
        # Walk backwards through history; the most recent assistant turn
        # that contains a list wins.
        for turn in reversed(conversation_history):
            if not isinstance(turn, dict):
                continue
            who = (turn.get("role") or "").strip().lower()
            if who not in {"assistant", "ai", "bot", "model"}:
                continue
            content = (turn.get("content") or "").strip()
            if not content:
                continue

            numbered_items: List[Tuple[int, str]] = []
            bulleted_items: List[str] = []
            for raw_line in content.splitlines():
                line = raw_line.rstrip()
                if not line:
                    continue

                m_bold = cls._LIST_LINE_PATTERNS[0].match(line)
                if m_bold:
                    n = int(m_bold.group(1))
                    title = m_bold.group(2).strip()
                    rest = (m_bold.group(3) or "").strip()
                    item = title if not rest else f"{title} {rest}"
                    numbered_items.append((n, item))
                    continue

                m_num = cls._LIST_LINE_PATTERNS[1].match(line)
                if m_num:
                    n = int(m_num.group(1))
                    numbered_items.append((n, m_num.group(2).strip()))
                    continue

                m_bul = cls._LIST_LINE_PATTERNS[2].match(line)
                if m_bul:
                    bulleted_items.append(m_bul.group(1).strip())
                    continue

            if numbered_items:
                # Sort by the number the assistant emitted, in case the
                # list is interleaved with prose. De-dupe positions.
                seen: Dict[int, str] = {}
                for n, item in numbered_items:
                    seen.setdefault(n, item)
                return [seen[k] for k in sorted(seen.keys())]
            if bulleted_items and len(bulleted_items) >= 2:
                return bulleted_items
        return []

    @staticmethod
    def _clean_list_item_to_doc_type(item: str) -> str:
        """Strip a leading bold/colon title prefix from a list item to a
        canonical document-type label.

        Examples:
            "**Regular Bail Application:** A petition under …"
                -> "Regular Bail Application"
            "Regular Bail Application — A petition …"
                -> "Regular Bail Application"
            "Application for Production of Documents under Section 94"
                -> "Application for Production of Documents under Section 94"
        """
        text = (item or "").strip()
        if not text:
            return ""
        # Markdown bold prefix: "**Title:** rest" or "**Title** rest"
        m = re.match(r"^\*\*([^*]+?)\*\*\s*[:—\-]?\s*(.*)$", text)
        if m:
            title = m.group(1).strip().rstrip(":").strip()
            if title:
                return title
        # Plain "Title: rest" or "Title — rest" / "Title - rest"
        m = re.match(r"^([^:—\-]{3,80})\s*[:—\-]\s+\S", text)
        if m:
            return m.group(1).strip()
        # Otherwise return the first ~80 chars trimmed at a sentence
        # boundary so the doc type is at least short.
        sentence = re.split(r"[.!?]", text, maxsplit=1)[0]
        return sentence.strip()[:80].rstrip()

    @classmethod
    def _resolve_ordinal_reference(
        cls,
        user_message: str,
        conversation_history: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Detect "do the third one"-style picks; return resolved doc type.

        Returns the canonical document-type string when the message is
        an ordinal reference AND the recent assistant history contains a
        list at that position. Returns None otherwise — the LLM
        classifier is the backstop in that case.
        """
        idx = cls._extract_ordinal_index(user_message)
        if idx is None:
            return None
        items = cls._extract_list_items_from_history(conversation_history)
        if not items:
            return None
        if idx == -1:
            chosen = items[-1]
        elif 1 <= idx <= len(items):
            chosen = items[idx - 1]
        else:
            return None
        doc_type = cls._clean_list_item_to_doc_type(chosen)
        return doc_type or None

    _DRAFTING_TURN_INTENTS = {
        "draft_request",
        "revision_request",
        "scoping_answer",
        "acknowledgement",
        "chat_question",
    }

    async def _classify_drafting_turn_intent(
        self,
        *,
        user_message: str,
        session_state: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Classify a per-turn message inside the drafting assistant.

        Returns {"intent": <label>, "document_type": <short name or "">}.
        On any failure / unrecognised value, returns
        `{"intent": "chat_question", "document_type": ""}` — the safe
        default never fires a draft unexpectedly.
        """
        snippet = (user_message or "")[:600]
        current_status = (session_state.get("draft_status") or "").strip() or "(none)"
        is_incompatible = bool(session_state.get("incompatible"))

        # Compact transcript: last 4 turns is enough state for the classifier.
        history_lines: List[str] = []
        for turn in (conversation_history or [])[-4:]:
            if not isinstance(turn, dict):
                continue
            who = (turn.get("role") or "").strip().lower()
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            if who in {"user", "human", "lawyer"}:
                history_lines.append(f"LAWYER: {content[:300]}")
            elif who in {"assistant", "ai", "bot", "model"}:
                history_lines.append(f"ASSISTANT: {content[:300]}")
        history_block = "\n".join(history_lines) if history_lines else "(no prior turns)"

        prompt = (
            "You are a one-shot intent classifier for messages sent inside "
            "an Indian legal drafting assistant chat. The assistant can "
            "draft documents (FIR complaints, bail applications, legal "
            "notices, affidavits, plaints, written statements, ...), "
            "answer legal questions, and have conversational chat.\n\n"
            "CRITICAL RULE — intent is determined SOLELY by what the "
            "LAWYER MESSAGE says. The workspace's case context may "
            "contain a fully developed matter (parties, FIR numbers, "
            "applicable statutes, custody details), but THAT IS NOT "
            "EVIDENCE OF DRAFTING INTENT. The lawyer can have a loaded "
            "workspace and still be greeting, asking a question, or "
            "just chatting. NEVER infer draft_request from the case "
            "context — only from the actual message.\n\n"
            "Classify the lawyer's CURRENT MESSAGE into exactly one label:\n"
            "  - draft_request: the LAWYER MESSAGE must explicitly ask "
            "the assistant to produce a draft. The message must contain "
            "EITHER (a) a drafting verb directed at the assistant — "
            "\"draft\", \"create\", \"make\", \"write\", \"prepare\", "
            "\"generate\", \"give me\", \"start\", \"begin\" — OR (b) "
            "the lawyer naming a document type as the subject of a "
            "request — \"I need a bail application\", \"a legal notice "
            "would help\". Pure topic mentions like \"bail\" or \"the "
            "NDPS case\" are NOT draft_requests on their own.\n"
            "    Positive examples: \"draft an FIR\", \"I want a legal "
            "notice for recovery\", \"give me a bail application\", "
            "\"prepare an anticipatory bail application\", \"actually "
            "start an affidavit instead\".\n"
            "    ORDINAL PICKS: when the assistant's RECENT CONVERSATION "
            "contains a numbered or bulleted list of document types, a "
            "reply that selects one by position is ALSO a draft_request. "
            "Resolve the position against the assistant's list and put "
            "the chosen item in document_type. Examples (assume the "
            "assistant just listed \"1. Bail Application 2. Section 94 "
            "Application 3. CCTV Preservation Application\"):\n"
            "        \"ok do the third one\" → draft_request, document_type "
            "= \"CCTV Preservation Application\".\n"
            "        \"let's go with option 2\" → draft_request, "
            "document_type = \"Section 94 Application\".\n"
            "        \"3rd please\" / \"the third\" / \"#3\" → same as the "
            "first example.\n"
            "    Negative examples (NOT draft_request — these are "
            "chat_question or acknowledgement): \"hi\", \"hello\", "
            "\"good morning\", \"what can you do?\", \"explain section "
            "483 BNSS\", \"what are my options here?\".\n"
            "  - revision_request: the lawyer is asking for edits to a "
            "draft that has already been delivered. ONLY valid when the "
            "current draft_status is \"ready\". Examples: \"tighten the "
            "prayer\", \"add a clause about ...\", \"remove paragraph 5\", "
            "\"make the tone stronger\".\n"
            "  - scoping_answer: the lawyer is answering open scoping "
            "questions the assistant just asked. ONLY valid when "
            "draft_status is \"gathering_info\". Examples: \"FIR 142 of 12 "
            "May 2026, at PS Sec 14\", short factual replies matching the "
            "questions in the recent conversation.\n"
            "  - acknowledgement: bare acknowledgement of the assistant's "
            "last message — \"ok\", \"thanks\", \"got it\", \"looks "
            "good\". No actionable content.\n"
            "  - chat_question: anything else — legal Q&A (\"what is "
            "Section 318 BNS?\"), greeting (\"hi\", \"hello\", \"good "
            "morning\", \"what can you do?\"), clarification about a "
            "draft (\"why this clause?\"), free-form discussion. When "
            "in doubt, choose THIS label — it is the safe default and "
            "never fires a draft.\n\n"
            "For `draft_request`, also produce `document_type`: the short "
            "canonical name of the requested document (e.g. \"FIR "
            "Complaint\", \"Bail Application\", \"Anticipatory Bail "
            "Application\", \"Legal Notice\", \"Affidavit\", \"Plaint\", "
            "\"Written Statement\", \"Reply Notice\"). Extract ONLY from "
            "the lawyer's message or from a document type the assistant "
            "recently offered in the visible conversation history. DO "
            "NOT invent a document type from case-context details (e.g. "
            "do not output \"Bail Application\" just because the case "
            "context mentions an arrest). If the message does not name "
            "or unambiguously imply a document, leave document_type as "
            "an empty string. For every non-draft_request intent, leave "
            "document_type empty.\n\n"
            f"CURRENT draft_status: {current_status}\n"
            f"INCOMPATIBLE-TEMPLATE flag: {is_incompatible}\n\n"
            f"RECENT CONVERSATION (most recent last):\n{history_block}\n\n"
            f"LAWYER MESSAGE:\n\"\"\"{snippet}\"\"\"\n\n"
            "Output STRICT JSON only: "
            "{\"intent\": \"<label>\", \"document_type\": \"<short name "
            "or empty>\"} — no prose, no markdown fences."
        )
        try:
            raw = await self._safe_llm_json(
                prompt=prompt,
                system_instruction=(
                    "You are a strict single-label intent classifier. "
                    "Output a JSON object with two fields: `intent` "
                    "(one of: draft_request, revision_request, "
                    "scoping_answer, acknowledgement, chat_question) and "
                    "`document_type` (a short string, empty when the "
                    "intent is not draft_request). Never invent any "
                    "other intent label."
                ),
                temperature=0.0,
                max_tokens=120,
            )
        except Exception as e:
            logger.debug("Drafting-turn intent classifier failed: {}", e)
            return {"intent": "chat_question", "document_type": ""}
        parsed = _parse_json_object(raw) or {}
        label = (parsed.get("intent") or "").strip().lower()
        if label not in self._DRAFTING_TURN_INTENTS:
            label = "chat_question"
        doc_type = (parsed.get("document_type") or "").strip()
        if label != "draft_request":
            doc_type = ""
        return {"intent": label, "document_type": doc_type}

    # Drafting-state values that mean "there is active drafting work
    # in progress on this session" — when one of these is set, a chat
    # turn must NOT clobber draft_status or open_questions, or the
    # lawyer loses scoping progress / draft context on every chat turn.
    _ACTIVE_DRAFT_STATUSES = {
        "analyzing",
        "gathering_info",
        "ready",
        "revising",
    }

    async def _handle_chat_question(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Reply to a non-drafting message using the soft chat prompt.

        Behaviour depends on whether a draft is in progress:
          - When draft_status is "analyzing" / "gathering_info" /
            "ready" / "revising", **preserve** all drafting fields and
            only update `last_assistant_message`. The next scoping
            answer / revision / ack still finds the session intact.
          - Otherwise (no active draft / draft_status=="chatting" /
            empty state), set draft_status="chatting" and clear
            open_questions as before — there is no scoping progress to
            preserve.
        """
        role = (case_context.get("role") or "ADJUDICATOR").upper()
        case_summary = case_context.get("case_summary") or ""
        case_facts = case_context.get("case_facts_brief") or ""
        prior_status = (session_state.get("draft_status") or "").strip()
        preserve_draft_state = prior_status in self._ACTIVE_DRAFT_STATUSES

        prompt = build_chat_reply_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            conversation_history=conversation_history,
            user_message=user_message,
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=DRAFTING_CHAT_INSTRUCTION,
                temperature=0.4,
                max_tokens=12000,
            )
        except Exception as e:
            logger.error("Drafting chat reply LLM call failed: {}", e)
            session_state["last_assistant_message"] = (
                "I ran into an issue replying just now. Could you re-send "
                "your message, or rephrase it?"
            )
            if not preserve_draft_state:
                session_state["draft_status"] = "chatting"
            return session_state
        reply = _strip_code_fences(raw).strip() or (
            "I am not sure how to respond to that. Could you rephrase, "
            "or tell me which document you would like me to help draft?"
        )
        session_state["last_assistant_message"] = reply
        if not preserve_draft_state:
            session_state["draft_status"] = "chatting"
            # No active draft → no scoping questions to keep.
            session_state["open_questions"] = []
        # When preserving, we deliberately leave draft_status,
        # open_questions, gathered_facts, draft_title, last_draft_* etc.
        # untouched so the next scoping / revision turn picks up where
        # the lawyer left off.
        return session_state

    async def _classify_incompatible_intent(
        self, user_message: str
    ) -> Dict[str, str]:
        """Classify the reply to the incompatibility warning.

        Returns a dict:
            {"intent": <label>, "document_type": <short name or "">}

        `intent` is one of: proceed_from_scratch, change_template,
        cancel, clarifying_question. On any failure / unrecognised
        value, returns
        `{"intent": "clarifying_question", "document_type": ""}` — that
        label routes to a contextual chat reply that engages with the
        message and re-offers the options without robotically repeating
        the canned block.

        `document_type` is populated when the intent is
        `proceed_from_scratch` AND the lawyer explicitly named the
        document type to draft (e.g. "draft the legal notice" →
        "Legal Notice"). Empty string otherwise — the caller falls
        back to the existing draft title.

        Note: pure acknowledgements ("ok", "thanks") never reach this
        method — they are caught earlier by
        `_is_post_draft_acknowledgement`. So any input here is a
        non-trivial reply that deserves a real classification.
        """
        snippet = (user_message or "")[:400]
        prompt = (
            "You are classifying a single user reply in a legal-drafting "
            "chat. The assistant previously told the lawyer that the "
            "uploaded template is fundamentally incompatible with the case "
            "and asked them to choose one of three options:\n"
            "  (a) Draft from a fresh structure (drop the template, draft "
            "from the case context).\n"
            "  (b) Upload a different template.\n"
            "  (c) Cancel the drafting session.\n\n"
            f"USER REPLY:\n\"\"\"{snippet}\"\"\"\n\n"
            "IMPORTANT: a single-letter reply like \"a\", \"A\", \"(a)\", "
            "\"a)\", or \"option a\" is the lawyer SELECTING that option — "
            "it is NOT the English article. Map a/A/(a) → proceed_from_scratch, "
            "b/B/(b) → change_template, c/C/(c) → cancel. Do this even if "
            "the reply is just one character.\n\n"
            "Classify the reply into exactly one label:\n"
            "  - proceed_from_scratch — the lawyer chose (a), OR explicitly "
            "asks the assistant to draft a specific document the assistant "
            "has previously mentioned in this conversation (legal notice, "
            "civil plaint, demand notice, police complaint, affidavit, "
            "criminal complaint, etc.), OR generically says to proceed / "
            "draft / go ahead / use a fresh structure.\n"
            "    Examples that MUST classify as proceed_from_scratch:\n"
            "      \"draft the legal notice\"     → document_type: \"Legal Notice\"\n"
            "      \"go ahead with the civil plaint\" → \"Civil Plaint\"\n"
            "      \"yes, the demand notice please\"  → \"Demand Notice\"\n"
            "      \"let's do the police complaint\"  → \"Police Complaint\"\n"
            "      \"start the affidavit\"            → \"Affidavit\"\n"
            "      \"draft it\" / \"yes proceed\" / \"go ahead\" → document_type: \"\"\n"
            "  - change_template — the lawyer chose (b), or asks to upload "
            "/ use / try a different template.\n"
            "  - cancel — the lawyer chose (c), or asks to cancel / stop / "
            "abandon / abort / nevermind / scrap this.\n"
            "  - clarifying_question — the lawyer is asking ABOUT the "
            "options without picking one (\"what are the other options?\", "
            "\"what does (b) mean?\", \"any alternatives?\"), pushing back "
            "on a suggestion without naming a replacement (\"i don't want "
            "a criminal complaint\"), expressing confusion, or otherwise "
            "engaging with the choice but NOT instructing the assistant "
            "to draft something. This is the catch-all for non-empty "
            "replies that do not unambiguously select (a)/(b)/(c), cancel, "
            "or instruct drafting.\n\n"
            "For proceed_from_scratch, also produce `document_type`: the "
            "short canonical name of the document the lawyer named (e.g. "
            "\"Legal Notice\", \"Civil Plaint\", \"Demand Notice\", \"FIR "
            "Complaint\", \"Police Complaint\", \"Affidavit\", "
            "\"Anticipatory Bail Application\", \"Written Statement\"). "
            "Use the lawyer's wording when clear; otherwise leave it as "
            "an empty string. For every other intent, leave it empty.\n\n"
            "Output STRICT JSON only: "
            "{\"intent\": \"<label>\", \"document_type\": \"<short name "
            "or empty>\"} — no prose, no markdown fences."
        )
        try:
            raw = await self._safe_llm_json(
                prompt=prompt,
                system_instruction=(
                    "You are a strict classifier. Output a JSON object "
                    "with two fields: `intent` (one of: "
                    "proceed_from_scratch, change_template, cancel, "
                    "clarifying_question) and `document_type` (a short "
                    "string, empty unless the intent is "
                    "proceed_from_scratch AND the user explicitly named "
                    "a document to draft). Never invent any other intent "
                    "label. When the user is not clearly selecting (a)/"
                    "(b)/(c), choose clarifying_question — unless they "
                    "are explicitly asking you to draft a specific "
                    "document, in which case proceed_from_scratch."
                ),
                temperature=0.0,
                max_tokens=120,
            )
        except Exception as e:
            logger.debug("Incompatible-intent classifier failed: {}", e)
            return {"intent": "clarifying_question", "document_type": ""}
        parsed = _parse_json_object(raw) or {}
        intent = (parsed.get("intent") or "").strip().lower()
        if intent not in {
            "proceed_from_scratch",
            "change_template",
            "cancel",
            "clarifying_question",
        }:
            intent = "clarifying_question"
        doc_type = (parsed.get("document_type") or "").strip()
        if intent != "proceed_from_scratch":
            doc_type = ""
        return {"intent": intent, "document_type": doc_type}

    def _build_incompatible_options_message(self, draft_title: str) -> str:
        """Static restatement of the (a)/(b)/(c) options.

        Used when the lawyer's reply is bare acknowledgement — we keep
        the state as `incompatible=True` and re-emit the choice prompt
        rather than guessing intent.
        """
        title = (draft_title or "this draft").strip() or "this draft"
        return (
            "The uploaded template does not fit the present matter, so I "
            "have not started drafting yet. Please choose how you would "
            "like to proceed:\n\n"
            "**How would you like to proceed?**\n\n"
            f"a) **Draft from a fresh structure** for {title}, using the "
            "case context — the uploaded template will be set aside.\n"
            "b) **Upload a different template** that fits this matter.\n"
            "c) **Cancel** this drafting session.\n\n"
            "Please reply with **(a)**, **(b)**, or **(c)** — or tell me "
            "in your own words."
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
                max_tokens=12000,
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

    async def _handle_translate_draft(
        self,
        *,
        target_language: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Translate the most-recent draft into the requested language.

        Reads `last_draft_markdown`, calls Gemini with the strict
        `DRAFT_TRANSLATE_INSTRUCTION`, emits the translated draft as a
        version-bumped `last_draft_document`. Frontend receives this as
        a fresh draft delivery (`isDraft=true`) and replaces the rendered
        body in the document panel. The previous English version remains
        in the chat transcript for reference.
        """
        target = (target_language or "").strip() or "Hindi"
        source_markdown = (session_state.get("last_draft_markdown") or "").strip()
        if not source_markdown:
            # Safety net — the dispatcher should already have ensured a
            # draft exists before invoking this handler. If state got
            # corrupted, reply gently rather than blowing up.
            session_state["last_assistant_message"] = (
                "I don't have a draft to translate yet. Once we finalise "
                "a draft I can produce a "
                f"{target} version of it."
            )
            return session_state

        role = (case_context.get("role") or "ADJUDICATOR").upper()
        case_summary = case_context.get("case_summary") or ""
        prompt = build_draft_translate_prompt(
            role=role,
            case_summary=case_summary,
            draft_markdown=source_markdown,
            target_language=target,
        )
        try:
            # Same headroom as draft render — a translated long
            # rejoinder needs the same room as the English original.
            raw = await gemini_client.generate(
                prompt,
                system_instruction=DRAFT_TRANSLATE_INSTRUCTION,
                temperature=0.2,
                max_tokens=32000,
            )
        except Exception as e:
            logger.error("Draft translate LLM call failed: {}", e)
            session_state["last_assistant_message"] = (
                "I ran into an issue translating the draft just now. "
                "The original draft is still on file — please retry, "
                "or let me know if you'd like to revise it instead."
            )
            return session_state

        translated = _strip_code_fences(raw).strip()
        if not translated:
            session_state["last_assistant_message"] = (
                "The translation came back empty. The original draft is "
                "still on file — please retry."
            )
            return session_state

        prev_version = int(session_state.get("current_draft_version") or 0)
        new_version = prev_version + 1
        prev_doc = session_state.get("last_draft_document") or {}
        base_title = (prev_doc.get("title") or session_state.get("draft_title")
                      or "Draft")
        # Title gets a language suffix so the frontend can show both
        # versions in a draft history if it tracks them. Avoid double-
        # suffixing on round-trips like English → Hindi → English.
        base_title_clean = re.sub(r"\s*\[[^\]]+\]\s*$", "", base_title).strip()
        new_title = f"{base_title_clean} [{target}]"

        # Translated drafts get their own DOCX export too.
        translate_user_id = (
            session_state.get("user_id") or case_context.get("user_id") or ""
        )
        translate_template_id = session_state.get("template_id")
        translate_tpl: Optional[Dict[str, Any]] = None
        if translate_template_id and translate_user_id:
            translate_tpl = await self.get_template(
                user_id=translate_user_id, template_id=translate_template_id
            )
        translated_download_url = await self._export_draft_docx(
            markdown=translated,
            draft_title=new_title,
            tpl=translate_tpl,
            user_id=translate_user_id,
            template_id=translate_template_id,
            workspace_id=session_state.get("workspace_id") or "",
            actionable_id=session_state.get("actionable_id") or "",
            version=new_version,
            role=role,
            case_summary=case_summary,
            case_facts_brief=(case_context.get("case_facts_brief") or ""),
            gathered_facts=(session_state.get("gathered_facts") or {}),
            lawyer_instructions=None,
        )

        translated_doc = {
            "title": new_title,
            "format": "markdown",
            "content": translated,
            "version": new_version,
            "sourceTemplateId": prev_doc.get("sourceTemplateId"),
            "sourceTemplateName": prev_doc.get("sourceTemplateName"),
            "sourceType": prev_doc.get("sourceType")
                or session_state.get("source") or "blank",
            "wordCount": _word_count(translated),
            "language": target,
            "translatedFromVersion": prev_version or 1,
            "downloadUrl": translated_download_url,
        }

        lead_in = (
            f"I have translated the draft into {target}. Review the "
            "document below — let me know if any term needs adjustment."
        )

        session_state["draft_status"] = "ready"
        session_state["current_draft_version"] = new_version
        session_state["last_draft_markdown"] = translated
        session_state["last_draft_document"] = translated_doc
        session_state["last_assistant_message"] = f"{lead_in}\n\n{translated}"
        session_state["open_questions"] = []
        # Per-turn delivery flag — consumed in handle_turn so BE marks
        # this turn as a draft delivery (frontend re-renders the
        # document panel).
        session_state["_draft_just_delivered"] = True
        return session_state

    # ------------------------------------------------------------------ docx export

    # ------------------------- DOCX path dispatcher -------------------------
    #
    # The dispatcher works for ANY template the lawyer can upload — it
    # never assumes a specific placeholder convention or layout. The
    # decision is principled, observable (every branch is logged), and
    # defaults safely: when in doubt we use skeleton-clone, which
    # *always* produces a coherent DOCX because the AI's markdown is
    # the source of truth for content and the template only contributes
    # styling.
    #
    # Strong placeholders ([bracket] / <angle>) are the signal that a
    # template is *explicitly designed* for AI fill (the lawyer marked
    # slots). Without them we don't risk in-place edit. With them we
    # still bail to skeleton-clone if the AI's markdown is shorter than
    # the template — that's the catalog-subset pattern.

    _SUBSET_RATIO = 0.7         # md/tpl below this → treat as subset
    _SUBSET_MIN_TPL_LEN = 500   # templates shorter than this skip the check

    @staticmethod
    def _template_text_length(template_docx_bytes: bytes) -> int:
        """Total visible text length across body, tables, headers, footers."""
        try:
            from io import BytesIO
            from docx import Document

            doc = Document(BytesIO(template_docx_bytes))
            total = 0
            for p in doc.paragraphs:
                total += len(p.text or "")
            for t in doc.tables:
                for row in t.rows:
                    for cell in row.cells:
                        for p in cell.paragraphs:
                            total += len(p.text or "")
            for section in doc.sections:
                for part in (section.header, section.footer):
                    try:
                        for p in part.paragraphs:
                            total += len(p.text or "")
                    except Exception:
                        continue
            return total
        except Exception:
            return 0

    @classmethod
    def _decide_render_path(
        cls,
        markdown: str,
        template_docx_bytes: Optional[bytes],
    ) -> Tuple[str, Dict[str, Any]]:
        """Choose the DOCX render path. Returns (path, debug_info).

        Possible paths: ``llm_diff`` | ``skeleton_clone`` | ``defaults``.

        ``llm_diff`` (the primary path when a template is present): clone
        the lawyer's template, ask Gemini for ``{keep_sections,
        text_edits}`` that describe how to transform the template into
        the lawyer's drafted content. Drops unrequested sections in
        catalog templates; replaces only the exact text the model
        identifies as fill content (placeholders, choice markers,
        labelled blanks). Every other byte of the template is preserved
        — same fonts, headings, numbering, tables, headers/footers,
        theme. This works for ANY template format because the matching
        is done by the LLM rather than fixed regex patterns.

        ``skeleton_clone`` runs if ``llm_diff`` fails (LLM error, bad
        JSON, zero applicable edits) — uses AI markdown as content with
        the template's fonts and styles inherited.

        ``defaults`` runs when there is no template at all — standard
        legal styling.
        """
        info: Dict[str, Any] = {
            "has_template": bool(template_docx_bytes),
            "tpl_text_len": 0,
            "md_len": len(markdown or ""),
        }

        if not template_docx_bytes:
            return "defaults", info

        info["tpl_text_len"] = cls._template_text_length(template_docx_bytes)
        return "llm_diff", info

    async def _fallback_heuristic_fill(
        self,
        *,
        markdown: str,
        template_docx_bytes: bytes,
        template_id: Optional[str],
        role: Optional[str],
        case_summary: Optional[str],
        case_facts_brief: Optional[str],
        gathered_facts: Optional[Dict[str, Any]],
        lawyer_instructions: Optional[str],
    ) -> Optional[bytes]:
        """Heuristic template-fill fallback used when LLM diff returns nothing.

        Order of attempts:
        1. **Catalog detection** — if the template looks like a catalog
           (≥2 detected top-level sections) AND the markdown selects a
           subset of them, run :func:`fill_template_sectional` and
           apply known placeholder values inside the survivors.
        2. **Placeholder fill** — if the template has lawyer-marked
           slots ([bracket]/<angle>/{{handle}}/(Foo_Bar)), call Gemini
           for values and apply :func:`fill_template`.

        Returns DOCX bytes on success, ``None`` on failure (caller then
        falls all the way back to skeleton-clone).
        """
        try:
            from app.processing.docx_template_filler import (
                detect_template_sections,
                extract_markdown_section_selections,
                extract_placeholders,
                fill_template,
                fill_template_sectional,
                select_template_sections,
            )
        except Exception as exc:
            logger.warning("Heuristic fill imports failed: {}", exc)
            return None

        # --- Catalog path ---
        try:
            tpl_sections = await asyncio.to_thread(
                detect_template_sections, template_docx_bytes
            )
        except Exception as exc:
            logger.warning("Section detection raised: {}", exc)
            tpl_sections = []

        if tpl_sections and len(tpl_sections) >= 2:
            md_nums, md_titles = extract_markdown_section_selections(markdown or "")
            kept = select_template_sections(
                tpl_sections,
                requested_titles=md_titles,
                requested_numbers=md_nums,
            )
            if kept:
                logger.info(
                    "Heuristic catalog fill: keeping {} of {} sections "
                    "(numbers={})",
                    len(kept), len(tpl_sections),
                    [s.number for s in kept],
                )
                try:
                    placeholders = await asyncio.to_thread(
                        extract_placeholders, template_docx_bytes
                    )
                    values = await self._extract_template_placeholder_values(
                        placeholders=placeholders,
                        role=role or "ADJUDICATOR",
                        case_summary=case_summary or "",
                        case_facts_brief=case_facts_brief or "",
                        gathered_facts=gathered_facts or {},
                        lawyer_instructions=lawyer_instructions,
                        draft_markdown=markdown,
                    ) or {}
                    return await asyncio.to_thread(
                        fill_template_sectional,
                        template_docx_bytes,
                        kept,
                        values,
                    )
                except Exception as exc:
                    logger.warning("Heuristic catalog fill failed: {}", exc)

        # --- Placeholder fill path ---
        try:
            placeholders = await asyncio.to_thread(
                extract_placeholders, template_docx_bytes
            )
            if not placeholders:
                return None
            strong = sum(
                1 for ph in placeholders
                if ph.kind in ("bracket", "angle", "handlebar", "paren_label")
            )
            if strong == 0:
                return None
            values = await self._extract_template_placeholder_values(
                placeholders=placeholders,
                role=role or "ADJUDICATOR",
                case_summary=case_summary or "",
                case_facts_brief=case_facts_brief or "",
                gathered_facts=gathered_facts or {},
                lawyer_instructions=lawyer_instructions,
                draft_markdown=markdown,
            )
            if not values:
                return None
            logger.info(
                "Heuristic placeholder fill: applying {} values to "
                "template {}",
                len(values), template_id,
            )
            return await asyncio.to_thread(
                fill_template, template_docx_bytes, values,
            )
        except Exception as exc:
            logger.warning("Heuristic placeholder fill failed: {}", exc)
            return None

    async def _extract_template_diff(
        self,
        *,
        template_docx_bytes: bytes,
        markdown: str,
        role: str,
        case_summary: str,
        case_facts_brief: str,
        gathered_facts: Dict[str, Any],
        lawyer_instructions: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Ask Gemini for ``{keep_sections, text_edits}`` for a template.

        This is the heart of the LLM-guided render path. The model sees
        the template's plain text + the AI's markdown, and returns the
        minimal set of edits required to make the template carry the
        markdown's content.

        Returns the parsed dict on success, ``None`` on any failure so
        the caller can fall back to skeleton-clone.
        """
        try:
            from app.processing.docx_template_filler import extract_template_text

            template_text = extract_template_text(template_docx_bytes)
        except Exception as exc:
            logger.warning("Failed to extract template text for diff: {}", exc)
            return None

        if not template_text.strip():
            return None

        prompt = build_template_diff_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts_brief,
            gathered_facts_json=json.dumps(gathered_facts, ensure_ascii=False),
            draft_markdown=markdown,
            template_text=template_text,
            lawyer_instructions=lawyer_instructions,
        )
        logger.info(
            "Template diff LLM call — template_text={} chars, markdown={} chars",
            len(template_text), len(markdown or ""),
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=TEMPLATE_DIFF_INSTRUCTION,
                temperature=0.15,
                # Headroom for 50–100 edits across a multi-section catalog.
                # Each edit is ~100 chars so 16k tokens is comfortable.
                max_tokens=16000,
            )
        except Exception as exc:
            logger.warning("Template diff LLM call failed: {}", exc)
            return None

        if not raw:
            logger.warning("Template diff LLM returned empty response")
            return None

        # Log a short preview so we can debug post-hoc if parsing fails.
        preview = raw.strip()[:200].replace("\n", " ")
        logger.debug("Template diff raw response (preview): {}", preview)

        parsed = _parse_json_object(raw)
        if not parsed:
            logger.warning(
                "Template diff LLM returned non-JSON output. Preview: {}",
                preview,
            )
            return None

        # Normalise the shape.
        raw_keep = parsed.get("keep_sections") or []
        keep_sections: List[int] = []
        if isinstance(raw_keep, list):
            for n in raw_keep:
                try:
                    keep_sections.append(int(n))
                except (TypeError, ValueError):
                    continue

        raw_edits = parsed.get("text_edits") or []
        text_edits: List[Dict[str, str]] = []
        skipped_no_match = 0
        skipped_recursive = 0
        skipped_too_short = 0
        skipped_too_many = 0
        skipped_other = 0
        if isinstance(raw_edits, list):
            for e in raw_edits:
                if not isinstance(e, dict):
                    skipped_other += 1
                    continue
                find = e.get("find")
                replace = e.get("replace")
                if not isinstance(find, str) or not find:
                    skipped_other += 1
                    continue
                if replace is None:
                    skipped_other += 1
                    continue
                rep = str(replace)
                if find == rep:
                    continue  # no-op

                # --- Defensive validation ---
                # Edits that would cause a runaway replacement, match
                # in too many places, or are too short to be specific
                # are all rejected here so they never reach the DOCX
                # editor.
                if len(find) < 4:
                    skipped_too_short += 1
                    logger.debug(
                        "Diff edit too short — skipping. find={!r}",
                        find,
                    )
                    continue
                if find in rep:
                    # Recursive replacement (find substring of replace)
                    # would cause infinite/runaway expansion at apply
                    # time. Reject.
                    skipped_recursive += 1
                    logger.warning(
                        "Diff edit is recursive (find is substring of "
                        "replace) — skipping. find={!r} replace={!r}",
                        find[:60],
                        rep[:60],
                    )
                    continue

                # Validate the find string is locatable in the template
                # (exact or whitespace-normalised) AND that it doesn't
                # match an unreasonable number of times — that's a sign
                # the find string is too generic.
                located_find: Optional[str] = None
                if find in template_text:
                    located_find = find
                else:
                    find_norm = " ".join(find.split())
                    tpl_norm = " ".join(template_text.split())
                    if find_norm and find_norm in tpl_norm:
                        located_find = find_norm

                if not located_find:
                    skipped_no_match += 1
                    logger.debug(
                        "Diff edit find not in template — skipping. find={!r}",
                        find[:80],
                    )
                    continue

                match_count = template_text.count(located_find)
                if match_count == 0:
                    # Fallback for whitespace-normalised match.
                    match_count = " ".join(template_text.split()).count(located_find)
                if match_count > 20:
                    skipped_too_many += 1
                    logger.warning(
                        "Diff edit too generic — skipping. find={!r} "
                        "matches {} times in template.",
                        located_find[:60],
                        match_count,
                    )
                    continue

                text_edits.append({"find": located_find, "replace": rep})

        logger.info(
            "Template diff result: keep_sections={} | applicable_edits={} "
            "(returned={}, no_match={}, recursive={}, too_short={}, "
            "too_many={}, other={})",
            keep_sections,
            len(text_edits),
            len(raw_edits) if isinstance(raw_edits, list) else 0,
            skipped_no_match,
            skipped_recursive,
            skipped_too_short,
            skipped_too_many,
            skipped_other,
        )
        return {"keep_sections": keep_sections, "text_edits": text_edits}

    async def _extract_template_placeholder_values(
        self,
        *,
        placeholders: List[Any],
        role: str,
        case_summary: str,
        case_facts_brief: str,
        gathered_facts: Dict[str, Any],
        lawyer_instructions: Optional[str],
        draft_markdown: str,
    ) -> Optional[Dict[str, str]]:
        """Ask Gemini to map each detected placeholder to a filled value.

        Returns ``{raw_placeholder_text -> replacement}`` on success, or
        ``None`` if the call/parse fails. Placeholders the model couldn't
        confidently fill are returned with their raw text unchanged so
        the lawyer can complete them manually.
        """
        if not placeholders:
            return None

        prompt_placeholders: List[Dict[str, str]] = []
        for ph in placeholders:
            raw = getattr(ph, "raw", None) or (ph.get("raw") if isinstance(ph, dict) else None)
            label = getattr(ph, "label", None) or (ph.get("label") if isinstance(ph, dict) else "")
            kind = getattr(ph, "kind", None) or (ph.get("kind") if isinstance(ph, dict) else "")
            if not raw:
                continue
            prompt_placeholders.append({
                "raw": raw,
                "label": label or "",
                "kind": kind or "",
            })

        if not prompt_placeholders:
            return None

        prompt = build_template_fill_prompt(
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts_brief,
            gathered_facts_json=json.dumps(gathered_facts, ensure_ascii=False),
            draft_markdown=draft_markdown,
            placeholders=prompt_placeholders,
            lawyer_instructions=lawyer_instructions,
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=TEMPLATE_FILL_INSTRUCTION,
                temperature=0.2,
                max_tokens=4000,
            )
        except Exception as e:
            logger.warning("Template fill LLM call failed: {}", e)
            return None

        parsed = _parse_json_object(raw or "")
        if not parsed:
            logger.warning("Template fill LLM returned non-JSON output")
            return None

        # Coerce keys/values to clean strings and drop entries that don't
        # match a detected placeholder (model may hallucinate extras).
        valid_raws = {ph["raw"] for ph in prompt_placeholders}
        values: Dict[str, str] = {}
        for k, v in parsed.items():
            if not isinstance(k, str) or k not in valid_raws:
                continue
            if v is None:
                continue
            sval = str(v).strip()
            if not sval:
                continue
            # If the model returned the placeholder text unchanged, skip —
            # the run already has that text, no need to overwrite.
            if sval == k:
                continue
            values[k] = sval

        logger.info(
            "Template fill mapped {}/{} placeholders",
            len(values),
            len(prompt_placeholders),
        )
        return values or None

    async def _export_draft_docx(
        self,
        *,
        markdown: str,
        draft_title: str,
        tpl: Optional[Dict[str, Any]],
        user_id: str,
        template_id: Optional[str],
        workspace_id: str,
        actionable_id: str,
        version: int,
        # Context used by the placeholder-fill path. All optional so the
        # method stays callable from translate / revision paths that may
        # not have every field handy.
        role: Optional[str] = None,
        case_summary: Optional[str] = None,
        case_facts_brief: Optional[str] = None,
        gathered_facts: Optional[Dict[str, Any]] = None,
        lawyer_instructions: Optional[str] = None,
    ) -> Optional[str]:
        """Render the draft Markdown into a .docx, upload to GCS, return public URL.

        Two paths produce the DOCX, in priority order:

        1. **Placeholder-fill path** — when the template has detectable
           placeholders (``[bracket]``, ``<angle>``, ``{{handlebar}}``,
           ``___underscore``), we ask Gemini for a value per placeholder
           and replace them **in-place** in the original template bytes.
           Output is byte-for-byte identical to the template except in
           the filled slots (full fidelity).
        2. **Skeleton-clone path** — when the template has no
           placeholders (or the filler fails), we clone the template,
           clear the fill area, and re-render the AI's markdown using
           the template's heading / body styles. Lower fidelity but
           still inherits fonts and section structure.

        Returns None on any failure — markdown content still ships in
        ``draftDocument.content`` and the caller omits ``downloadUrl``.
        """
        try:
            from app.core.config import settings as _settings
            from app.processing.docx_generator import render_markdown_to_docx
            from app.storage import gcs as _gcs

            bucket = getattr(_settings, "gcs_docx_bucket_name", None)
            if not bucket:
                logger.warning(
                    "DOCX export skipped — GCS_DOCX_BUCKET_NAME not configured"
                )
                return None

            template_docx_bytes: Optional[bytes] = None
            if tpl and tpl.get("template_docx_gcs_uri"):
                try:
                    b, n = _gcs.parse_gs_uri(tpl["template_docx_gcs_uri"])
                    template_docx_bytes = await asyncio.to_thread(
                        _gcs.download_bytes, b, n
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch template skeleton from GCS "
                        "(template={}, uri={}): {}",
                        template_id,
                        tpl.get("template_docx_gcs_uri"),
                        exc,
                    )
                    template_docx_bytes = None

            # Lazy backfill: legacy templates ingested before this feature
            # have no `template_docx_gcs_uri`. Try one re-download via the
            # cached `source_url`; if it works, convert (PDF→DOCX) when
            # needed and upload to GCS, then persist the URI to the
            # template cache for next time.
            if (
                template_docx_bytes is None
                and tpl
                and (tpl.get("extension") or "").lower() in ("docx", "pdf")
                and tpl.get("source_url")
            ):
                template_docx_bytes = await self._lazy_backfill_template_skeleton(
                    tpl=tpl,
                    user_id=user_id,
                    template_id=template_id or "",
                    bucket=bucket,
                )

            # ----- Choose the DOCX render path -----
            chosen_path, dispatch_info = self._decide_render_path(
                markdown, template_docx_bytes
            )
            logger.info(
                "DOCX dispatch: path={} | template={} | tpl_len={} | md_len={}",
                chosen_path,
                dispatch_info["has_template"],
                dispatch_info["tpl_text_len"],
                dispatch_info["md_len"],
            )

            docx_bytes: Optional[bytes] = None
            path_used = chosen_path

            if chosen_path == "llm_diff":
                # LLM-guided template edit. Gemini sees the template's
                # text plus the AI's markdown and returns
                # {keep_sections, text_edits}. We clone the template,
                # drop unrequested catalog sections, and apply the
                # edits in place. Template formatting (fonts, headings,
                # alignment, numbering, tables, headers/footers, theme)
                # is preserved byte-for-byte; only the slot text is
                # replaced.
                try:
                    from app.processing.docx_template_filler import (
                        apply_template_diff as _apply_diff,
                    )

                    diff = await self._extract_template_diff(
                        template_docx_bytes=template_docx_bytes,
                        markdown=markdown,
                        role=role or "ADJUDICATOR",
                        case_summary=case_summary or "",
                        case_facts_brief=case_facts_brief or "",
                        gathered_facts=gathered_facts or {},
                        lawyer_instructions=lawyer_instructions,
                    )
                    if diff and (diff.get("text_edits")
                                 or diff.get("keep_sections")):
                        keep = diff.get("keep_sections") or []
                        edits = diff.get("text_edits") or []
                        docx_bytes = await asyncio.to_thread(
                            _apply_diff,
                            template_docx_bytes,
                            keep,
                            edits,
                        )
                    else:
                        logger.warning(
                            "LLM diff returned no usable output — "
                            "falling back to sectional/placeholder fill"
                        )
                except Exception as exc:
                    logger.warning(
                        "LLM diff failed: {} — falling back to "
                        "sectional/placeholder fill",
                        exc,
                    )

                # Heuristic fallback chain — try sectional_fill /
                # placeholder_fill before skeleton-clone so we still
                # preserve template formatting when the LLM diff path
                # didn't produce a result.
                if docx_bytes is None:
                    docx_bytes = await self._fallback_heuristic_fill(
                        markdown=markdown,
                        template_docx_bytes=template_docx_bytes,
                        template_id=template_id,
                        role=role,
                        case_summary=case_summary,
                        case_facts_brief=case_facts_brief,
                        gathered_facts=gathered_facts,
                        lawyer_instructions=lawyer_instructions,
                    )
                    if docx_bytes is not None:
                        path_used = "heuristic_fill"

                if docx_bytes is None:
                    path_used = "skeleton_clone"

            if path_used == "skeleton_clone":
                # AI markdown drives content; template provides fonts,
                # styles, theme, and (when detectable) preamble. Works
                # for ANY template — prose, catalog, form, mixed.
                docx_bytes = await asyncio.to_thread(
                    render_markdown_to_docx,
                    markdown,
                    template_docx_bytes=template_docx_bytes,
                    title=draft_title,
                )
            elif path_used == "defaults":
                # No template — standard legal styling for AI-only drafts.
                docx_bytes = await asyncio.to_thread(
                    render_markdown_to_docx,
                    markdown,
                    template_docx_bytes=None,
                    title=draft_title,
                )

            # Object key convention (matches the rest of the platform):
            #   draft/{userId}/{uuid4}-{safeOriginalName}.docx
            # A fresh UUID per upload prevents collisions across revisions
            # and across users re-using the same draft title. The user id
            # scopes the object to its owner for easy lifecycle policies.
            import uuid as _uuid

            safe_user = user_id or "anonymous"
            safe_name = _safe_filename(draft_title) + ".docx"
            file_uuid = _uuid.uuid4().hex
            blob_name = (
                f"{_DRAFT_GCS_PREFIX}/{safe_user}/{file_uuid}-{safe_name}"
            )
            await asyncio.to_thread(
                _gcs.upload_bytes,
                bucket,
                blob_name,
                docx_bytes,
                _DOCX_MIME,
            )
            # V2 signed URL — long-lived (~25 years), works under UBLA.
            # Matches the platform's TS uploader (expires 2050-01-01).
            url = await asyncio.to_thread(
                _gcs.generate_signed_url, bucket, blob_name
            )
            logger.success(
                "DOCX exported (user={}, workspace={}, session={}, "
                "version={}, path={}): {}",
                safe_user,
                workspace_id or "-",
                actionable_id or "-",
                version,
                path_used,
                url,
            )
            return url
        except Exception as e:
            logger.warning(
                "DOCX export failed (workspace={}, session={}, version={}): {}",
                workspace_id,
                actionable_id,
                version,
                e,
            )
            return None

    async def _lazy_backfill_template_skeleton(
        self,
        *,
        tpl: Dict[str, Any],
        user_id: str,
        template_id: str,
        bucket: str,
    ) -> Optional[bytes]:
        """Re-download a legacy template's bytes and persist them to GCS.

        Best-effort: returns the bytes on success so the current render
        can use them; the cache is updated in the background so subsequent
        renders skip the round-trip.
        """
        source_url = tpl.get("source_url")
        if not source_url:
            return None
        ext = (tpl.get("extension") or "").lower()
        try:
            from app.processing.document_processor import document_processor

            if not document_processor.http_client:
                await document_processor.initialize()
            content, _size = await document_processor._download_file(
                source_url,
                _filename_for_extraction(
                    tpl.get("name") or "template",
                    ext or "docx",
                ),
            )
        except Exception as exc:
            logger.warning(
                "Lazy template re-download failed (template={}, url expired?): {}",
                template_id,
                exc,
            )
            return None

        # PDFs need conversion before we can store them as an editable
        # DOCX skeleton. Conversion is best-effort; on failure we abort
        # the backfill and the dispatcher falls through to skeleton_clone.
        if ext == "pdf":
            try:
                from app.processing.pdf_to_docx import convert_pdf_to_docx

                converted = await asyncio.to_thread(
                    convert_pdf_to_docx, content, tpl.get("name") or "template",
                )
                if not converted:
                    logger.warning(
                        "Lazy PDF→DOCX conversion produced no usable output "
                        "(template={}) — skipping backfill",
                        template_id,
                    )
                    return None
                content = converted
            except Exception as exc:
                logger.warning(
                    "Lazy PDF→DOCX conversion raised (template={}): {}",
                    template_id, exc,
                )
                return None

        try:
            from app.storage import gcs as _gcs

            blob_name = f"{_TEMPLATE_GCS_PREFIX}/{user_id}/{template_id}.docx"
            await asyncio.to_thread(
                _gcs.upload_bytes,
                bucket,
                blob_name,
                content,
                _DOCX_MIME,
                public=False,
            )
            new_uri = f"gs://{bucket}/{blob_name}"
        except Exception as exc:
            logger.warning(
                "Lazy template upload to GCS failed (template={}): {}",
                template_id,
                exc,
            )
            return content  # still usable for this render

        try:
            from app.vectorstore.qdrant import vector_store

            updated_payload = dict(tpl)
            updated_payload["template_docx_gcs_uri"] = new_uri
            await vector_store.upsert_user_state_cache(
                user_id=user_id,
                kind=TEMPLATE_CACHE_KIND,
                scope_id=template_id,
                data=updated_payload,
            )
            logger.info(
                "Lazy backfill complete — template={} now has skeleton at {}",
                template_id,
                new_uri,
            )
        except Exception as exc:
            logger.debug(
                "Lazy template cache update failed (template={}): {}",
                template_id,
                exc,
            )

        return content

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
        tpl: Optional[Dict[str, Any]] = None
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
            # 32000 tokens ≈ 30–40 pages of typical legal prose, giving
            # long templates (rejoinders, evidence affidavits, multi-
            # affidavit packs) room to render in full. Previous 12000-
            # cap truncated long renders mid-sentence and the user only
            # saw a partial draft.
            raw = await gemini_client.generate(
                prompt,
                system_instruction=DRAFT_RENDER_INSTRUCTION,
                temperature=0.35,
                max_tokens=32000,
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

        # Generate the .docx artifact, upload to GCS, and collect the
        # download URL. Any failure here is non-fatal — markdown still
        # ships in `draftDocument.content` and the URL is simply
        # omitted. Case context is forwarded so the placeholder-fill
        # path (when the template uses [bracket]-style slots) can map
        # each slot to a value from the lawyer's gathered facts.
        download_url: Optional[str] = await self._export_draft_docx(
            markdown=markdown,
            draft_title=draft_title,
            tpl=tpl,
            user_id=user_id,
            template_id=template_id,
            workspace_id=session_state.get("workspace_id") or "",
            actionable_id=session_state.get("actionable_id") or "",
            version=new_version,
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            gathered_facts=gathered,
            lawyer_instructions=lawyer_instructions,
        )

        draft_document = {
            "title": draft_title,
            "format": "markdown",
            "content": markdown,
            "version": new_version,
            "sourceTemplateId": template_id,
            "sourceTemplateName": template_name,
            "sourceType": source_value,
            "wordCount": _word_count(markdown),
            "downloadUrl": download_url,
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
