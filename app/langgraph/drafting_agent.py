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
import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.ai.gemini_client import gemini_client
from app.prompts.drafting import (
    DRAFT_RENDER_INSTRUCTION,
    DRAFT_TRANSLATE_INSTRUCTION,
    DRAFTING_CHAT_INSTRUCTION,
    DRAFTING_ROUTER_INSTRUCTION,
    SUGGESTED_DRAFTS_INSTRUCTION,
    TEMPLATE_APPLICABILITY_INSTRUCTION,
    TEMPLATE_CLASSIFY_INSTRUCTION,
    TEMPLATE_DIFF_INSTRUCTION,
    TEMPLATE_DOCUMENT_TYPE_LABELS,
    TEMPLATE_FILL_INSTRUCTION,
    TEMPLATE_KICKOFF_INSTRUCTION,
    TEMPLATE_MATCH_INSTRUCTION,
    TEMPLATE_PROFILE_INSTRUCTION,
    TEMPLATE_VARIABLE_KINDS,
    build_chat_reply_prompt,
    build_draft_render_prompt,
    build_draft_translate_prompt,
    build_router_prompt,
    build_suggested_drafts_prompt,
    build_template_applicability_prompt,
    build_template_classify_prompt,
    build_template_diff_prompt,
    build_template_fill_prompt,
    build_template_kickoff_prompt,
    build_template_match_prompt,
    build_template_profile_prompt,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Templates are no longer persisted in Qdrant — they live in the in-memory
# `app.langgraph.template_store.template_store`, fed in real time by the
# inbound `template_sync` socket event (backend re-pushes the full library
# on reconnect). Only drafting-session state still uses the Qdrant cache.
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


def _is_missing_object_error(exc: Exception) -> bool:
    """True when a download error means the file is GONE (not transient).

    Detects a definitive "object does not exist" from GCS / HTTP:
    ``404 NoSuchKey`` / ``No such object`` / ``The specified key does not
    exist``. Deliberately does NOT match ``403`` (expired signed URL) or
    network/timeout errors — we only treat a confirmed-missing object as
    an orphan worth purging from the template cache.
    """
    msg = str(exc).lower()
    if "nosuchkey" in msg:
        return True
    if "no such object" in msg:
        return True
    if "specified key does not exist" in msg:
        return True
    if "404" in msg and "not found" in msg:
        return True
    return False

_RELEVANCE_VALUES = {"High", "Medium", "Low"}
_VALID_SOURCES = {"template", "suggestedDraft", "blank"}
_MAX_SUGGESTIONS = 6
# When the lawyer has reusable templates, the panel may carry a few extra
# cards so every applicable template is guaranteed a slot without crowding
# out the LLM's free case-driven suggestions. See _ensure_template_coverage.
_MAX_SUGGESTIONS_WITH_TEMPLATES = 8

# Version of the template "drafting blueprint" profiling schema. Bump this
# whenever the profiler's variable-capture rules change so already-cached
# templates (profiled under an older schema) are re-profiled on next use rather
# than serving a stale registry. v2 adds capture of content/substantiation
# clauses (grounds, claims/defences, heads of loss/relief, representations …).
# v3 adds: email in sender block always captured; hardcoded demand/compensation
# amounts always captured as per-matter variables (fill_from_case: false).
PROFILE_SCHEMA_VERSION = 3

# Defaults for the drafting-blueprint controls the lawyer can override in the
# dashboard. Used when we PROPOSE a fresh blueprint (the user then edits these
# on the "AI Drafting Blueprint & Variables Approval" screen).
_DEFAULT_DRAFT_DEPTH = "Detailed & Comprehensive"
_DEFAULT_STRUCTURE_STYLE = "Sectioned Clauses"
_DEFAULT_GUARDRAIL_LEVEL = "Comprehensive"

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


# High-signal markers of a slot that was left UNFILLED in a rendered draft.
# These are the placeholder shapes the render prompt is told to fall back to
# when neither the case context nor the lawyer supplied a value — we scan the
# finished draft for them so a draft with holes is never shipped as "complete"
# without flagging exactly what is still missing.
_RESIDUAL_PLACEHOLDER_PATTERNS = (
    re.compile(r"<[^<>\n]{1,80}>"),            # <Client Address>, <Issuance Date>
    re.compile(r"\{\{[^{}\n]{1,80}\}\}"),      # {{handlebars}}
    re.compile(r"_{3,}"),                       # ____ underscore blanks
    re.compile(r"X{4,}", re.IGNORECASE),        # XXXX
    re.compile(r"\(\s*to be filled[^)]*\)", re.IGNORECASE),
    re.compile(r"\[(?:[^\[\]\n]*?(?:address|name|number|date|amount|insert|"
               r"to be filled|tbd|xxxx|placeholder)[^\[\]\n]*?)\]", re.IGNORECASE),
)


def _residual_placeholders(markdown: str) -> List[str]:
    """Return the distinct unfilled-placeholder tokens left in a rendered draft.

    Used as a post-render safety net: if the kickoff failed to ask for a slot
    and neither the case nor the lawyer supplied it, the render leaves a marked
    placeholder. We surface those so the draft is never silently shipped with
    holes. Conservative by design — only high-signal markers (angle/handlebar
    tokens, underscore blanks, XXXX, "to be filled", and bracket tokens whose
    text names a slot) are matched, to avoid flagging ordinary legal prose."""
    if not markdown:
        return []
    found: List[str] = []
    seen: set = set()
    for pat in _RESIDUAL_PLACEHOLDER_PATTERNS:
        for m in pat.finditer(markdown):
            tok = m.group(0).strip()
            key = tok.lower()
            if tok and key not in seen:
                seen.add(key)
                found.append(tok)
    return found


_PLACEHOLDER_TOKEN_RE = re.compile(r"<[^<>\n]{1,60}>")


def _restore_regressed_values(
    previous_md: str, new_md: str
) -> "tuple[str, List[str]]":
    """Repair a re-render that re-blanked an already-filled field — for ANY
    field in ANY template, by aligning whole lines (layout-independent, no
    field names or value patterns hard-coded).

    A revision changes very little, so the new draft is nearly line-for-line
    identical to the previous one. For each NEW-draft line that still contains a
    `<placeholder>`, find the most similar PREVIOUS-draft line that is itself
    placeholder-FREE (i.e. was filled). If they match closely enough, the field
    regressed — restore the whole previous line. Lines that were empty in both
    drafts have no placeholder-free counterpart, so they are left untouched.
    Restoring the entire line (rather than a regex-extracted value) means it
    works regardless of layout — `Survey No. 262/4`, `(GSTIN: …)`,
    `Enrollment No.: …`, inline prose, etc. Deterministic safety net behind the
    prompt-level preservation rule. Returns ``(repaired_md, restored_labels)``."""
    if not previous_md or not new_md:
        return new_md, []
    import difflib

    prev_lines = previous_md.splitlines()
    # Candidate sources: previous lines that are filled (no placeholder) and
    # carry enough text to match on.
    prev_candidates = [
        p for p in prev_lines
        if p.strip() and not _PLACEHOLDER_TOKEN_RE.search(p)
    ]
    if not prev_candidates:
        return new_md, []

    def _tokens(s: str) -> List[str]:
        return [t for t in re.split(r"\s+", s) if t]

    new_lines = new_md.splitlines()
    out_lines = list(new_lines)
    restored: List[str] = []
    for i, line in enumerate(new_lines):
        if not _PLACEHOLDER_TOKEN_RE.search(line):
            continue  # this line is fine — nothing re-blanked here
        # The "scaffolding" around the placeholder (label / prose), minus the
        # empty slot itself. The filled previous line must contain this text.
        new_stable = _PLACEHOLDER_TOKEN_RE.sub("", line).strip()
        nt = _tokens(new_stable)
        if not nt or len(new_stable) < 3:
            continue  # too little context to match safely
        best = None
        best_contained = 0.0
        best_ratio = 0.0
        for pline in prev_candidates:
            pt = set(_tokens(pline))
            # Fraction of the regressed line's scaffolding tokens present in the
            # candidate — length-insensitive, so a long value doesn't lower it.
            contained = sum(1 for t in nt if t in pt) / len(nt)
            if contained < best_contained:
                continue
            ratio = difflib.SequenceMatcher(None, new_stable, pline.strip()).ratio()
            if contained > best_contained or ratio > best_ratio:
                best_contained = contained
                best_ratio = ratio
                best = pline
        # The scaffolding fully matches a filled previous line ⇒ the field was
        # filled before and got re-blanked. Restore the whole previous line.
        # Single-token scaffolds need a stronger overall-similarity floor to
        # avoid spurious matches.
        if (
            best is not None
            and best_contained >= 0.8
            and (len(nt) >= 2 or best_ratio >= 0.6)
        ):
            out_lines[i] = best
            restored.append(new_stable[:48])
    return "\n".join(out_lines), restored


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
        _desc = (item.get("description") or "").strip()
        if len(_desc) > 220:
            # Trim at a word boundary (never mid-word) so the card never shows a
            # broken sentence fragment.
            _desc = _desc[:220].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
        cleaned.append(
            {
                "title": title,
                "relevance": _normalize_relevance(item.get("relevance")),
                "description": _desc or None,
                # Template pre-match fields always present so the backend
                # reliably receives both id and name (populated later by
                # _attach_template_matches when user_id is supplied; left
                # None on the legacy no-user_id path).
                "templateId": None,
                "templateName": None,
                "matchMode": None,
            }
        )
    cleaned = _filter_forbidden_acts(_dedupe_suggestions(cleaned))
    return cleaned[:_MAX_SUGGESTIONS]


class DraftingAgent:
    """High-level entrypoint for the Drafting Assistant feature."""

    # Bound the suggested-draft pre-match cache so it cannot grow without
    # limit across a long-lived session (one entry per distinct
    # user+suggestion-title; oldest evicted first).
    _PREMATCH_CACHE_MAX = 256

    def __init__(self) -> None:
        # Bridge the suggested-drafts pre-match decision to the kickoff
        # click. The frontend/backend does not always forward the
        # pre-matched ``templateId`` when the lawyer clicks a suggestion
        # card, and a fresh click-time re-match can disagree with the
        # pre-match (the LLM applicability check is non-deterministic) —
        # leaving the lawyer with a "matched" badge but a draft produced
        # from scratch. We remember the pre-match ``templateId`` keyed by
        # (user_id, normalized title) so the kickoff reuses the SAME
        # template the panel advertised.
        self._suggestion_prematch: "OrderedDict[str, str]" = OrderedDict()

    @staticmethod
    def _prematch_key(user_id: str, title: str) -> str:
        return f"{(user_id or '').strip()}::{(title or '').strip().lower()}"

    def _remember_prematch(
        self, user_id: str, title: str, template_id: str
    ) -> None:
        """Record a suggestion's pre-matched templateId for later reuse on
        click. No-op when any part is missing.

        First-match-wins LOCK: once a suggestion title is bound to a
        template, a later re-match (e.g. triggered by the lawyer uploading
        a *different* template) must NOT silently re-point the same card to
        the new template. We keep the original binding and only refresh its
        LRU position so it isn't evicted. A binding is only released
        explicitly via `_forget_prematch` (used when the locked template is
        confirmed deleted). This is what makes a matched card "frozen" — the
        template it advertised is the template the click will use.
        """
        if not (user_id and title and template_id):
            return
        key = self._prematch_key(user_id, title)
        cache = self._suggestion_prematch
        existing = cache.get(key)
        if existing and existing != template_id:
            # Locked to a different template already — keep the original
            # binding, just touch it so it survives LRU eviction.
            cache.move_to_end(key)
            logger.debug(
                "Prematch lock held: title={!r} stays bound to {} "
                "(ignored re-point to {})",
                (title or "")[:60],
                existing,
                template_id,
            )
            return
        cache[key] = template_id
        cache.move_to_end(key)
        while len(cache) > self._PREMATCH_CACHE_MAX:
            cache.popitem(last=False)

    def _forget_prematch(self, user_id: str, title: str) -> None:
        """Release a suggestion's pre-matched binding. Used when the locked
        template is confirmed gone so the card can be re-matched cleanly.
        No-op when nothing is bound."""
        if not (user_id and title):
            return
        self._suggestion_prematch.pop(self._prematch_key(user_id, title), None)

    def _forget_prematch_by_template_id(
        self, user_id: str, template_id: str
    ) -> int:
        """Release EVERY pre-match lock (for this user) pointing at a given
        template_id — used when that template is deleted so a later suggestion
        click can never recall the dead id. Returns how many bindings were
        dropped."""
        if not (user_id and template_id):
            return 0
        prefix = f"{(user_id or '').strip()}::"
        tid = template_id.strip()
        stale_keys = [
            k for k, v in self._suggestion_prematch.items()
            if k.startswith(prefix) and v == tid
        ]
        for k in stale_keys:
            self._suggestion_prematch.pop(k, None)
        return len(stale_keys)

    def _recall_prematch(self, user_id: str, title: str) -> Optional[str]:
        """Return the pre-matched templateId for this suggestion title, or
        None if we never matched one."""
        if not (user_id and title):
            return None
        return self._suggestion_prematch.get(self._prematch_key(user_id, title))

    @staticmethod
    def _recover_suggestion_title(user_message: str) -> Optional[str]:
        """Recover the clicked card's title from the kickoff message when the
        backend omits the ``suggestionTitle`` field.

        The frontend embeds the title in ``content`` even when it drops the
        explicit field, e.g.::

            Please draft a complete "Legal Notice for Rescission of Contract
            under Section 16 of the Sale of Goods Act, 1930" for this case ...

        Returns the first reasonably-long double-quoted span, or None.
        """
        if not user_message:
            return None
        m = re.search(r'"([^"]{4,200})"', user_message)
        return m.group(1).strip() if m else None

    @staticmethod
    def _recover_template_name(user_message: str) -> Optional[str]:
        """Recover an explicit template name from the kickoff message when
        the frontend phrases the click as ``using the template "<NAME>"``.
        Returns the quoted name, or None."""
        if not user_message:
            return None
        m = re.search(
            r'using the template\s+"([^"]{2,200})"',
            user_message,
            re.IGNORECASE,
        )
        return m.group(1).strip() if m else None

    async def _resolve_template_id_by_name(
        self, *, user_id: str, name: str
    ) -> Optional[str]:
        """Look up a ready template's id by (normalized) name. Used to bridge
        a click whose content names a template but carries no templateId."""
        if not (user_id and name):
            return None
        try:
            ready, _ = await self._tm_gather(user_id=user_id, suggestion_text="")
        except Exception:
            return None
        # Exact (normalised) equality only. The previous loose substring
        # test (`norm in tname or tname in norm`) let a newer, longer-named
        # template win by partial overlap and return the WRONG template —
        # exactly the bug where uploading another template hijacked a card's
        # advertised template. Normalise both sides so filename punctuation
        # / casing differences don't block a genuine exact match.
        norm = self._normalise_for_match(name)
        if not norm:
            return None
        for t in ready:
            tname = self._normalise_for_match(t.get("name") or "")
            if tname and tname == norm:
                return t.get("templateId")
        return None

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
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return 4–6 proactive draft suggestions for the workspace.

        When ``user_id`` is supplied, each suggestion is also pre-matched
        against the lawyer's template library so the response carries
        ``templateId`` / ``templateName`` / ``matchMode`` for the
        frontend to render — and so the click path can skip a second
        matcher round-trip and pass ``templateId`` straight to the
        kickoff handler. Legacy callers that omit ``user_id`` get the
        original shape (no template fields).

        Falls back to an empty list on any failure — callers should not
        block workspace creation on this feature.
        """
        # Gather the lawyer's ready templates up front (when we know the
        # user) so the suggestion LLM can surface drafts that reuse an
        # existing template. Template-blind otherwise — see the empty
        # default in build_suggested_drafts_prompt.
        template_library_brief = ""
        ready_templates: List[Dict[str, Any]] = []
        if user_id:
            try:
                ready_templates, _ = await self._tm_gather(
                    user_id=user_id, suggestion_text=""
                )
                template_library_brief = self._format_template_library_brief(
                    ready_templates
                )
            except Exception as exc:
                logger.warning(
                    "Suggested-drafts template-library gather failed "
                    "(non-fatal): {}",
                    exc,
                )

        prompt = build_suggested_drafts_prompt(
            role=role,
            case_summary=case_summary,
            applicable_laws_brief=applicable_laws_brief,
            strategy_brief=strategy_brief,
            analysis_brief=analysis_brief,
            list_of_dates_brief=list_of_dates_brief,
            template_library_brief=template_library_brief,
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
            return suggestions

        # Pre-match templates per suggestion so the lawyer sees which
        # template (if any) will be used before clicking. Runs in
        # parallel; per-item failures degrade gracefully (item just
        # lacks the template fields, click path will re-match).
        if user_id:
            try:
                suggestions = await self._attach_template_matches(
                    user_id=user_id, suggestions=suggestions
                )
                # Remember each matched suggestion's templateId keyed by
                # title so the kickoff click reuses the SAME template the
                # panel showed — even if the backend doesn't forward
                # templateId on click. See _recall_prematch in _handle_kickoff.
                for s in suggestions:
                    if (
                        isinstance(s, dict)
                        and s.get("matchMode") == "matched"
                        and s.get("templateId")
                    ):
                        self._remember_prematch(
                            user_id, s.get("title") or "", s["templateId"]
                        )
            except Exception as exc:
                logger.warning(
                    "Pre-match for suggestions failed (non-fatal): {}", exc
                )

            # Guarantee that every reusable template whose LEGAL PURPOSE fits
            # this case surfaces as a card — independent of whether the LLM
            # happened to propose a draft of that type this round (the LLM
            # step is non-deterministic and can silently drop an applicable
            # template when case details change). See _ensure_template_coverage.
            try:
                suggestions = await self._ensure_template_coverage(
                    user_id=user_id,
                    role=role,
                    suggestions=suggestions,
                    ready_templates=ready_templates,
                    case_summary=case_summary,
                    applicable_laws_brief=applicable_laws_brief,
                    strategy_brief=strategy_brief,
                    analysis_brief=analysis_brief,
                    list_of_dates_brief=list_of_dates_brief,
                )
            except Exception as exc:
                logger.warning(
                    "Template coverage pass failed (non-fatal): {}", exc
                )

        logger.success(
            "Drafting suggestions: {} item(s) ready (pre-matched={})",
            len(suggestions),
            bool(user_id),
        )
        return suggestions

    @staticmethod
    def _format_template_library_brief(
        ready: List[Dict[str, Any]], *, cap: int = 15
    ) -> str:
        """Render the ready-template list as compact one-line-per-template
        hints for the suggestion prompt.

        Carries only the document TYPE and PURPOSE — never the template's
        original party names / facts — so the suggester cannot leak the
        source matter's details into unrelated suggestions. Returns an empty
        string when there is nothing reusable (prompt then behaves exactly
        as the template-blind form).
        """
        if not ready:
            return ""
        lines: List[str] = []
        for t in ready[:cap]:
            payload = t.get("payload") or {}
            doc_type = (payload.get("document_type") or t.get("category") or "").strip()
            # Prefer the fuller legal purpose (filing_context) over the coarse
            # one-line document_purpose so the suggester understands what each
            # template is actually for. Still TYPE/PURPOSE only — never the
            # template's original party names or facts.
            purpose = (
                t.get("filing_context")
                or payload.get("filing_context")
                or t.get("purpose")
                or ""
            ).strip()
            label = doc_type or "Unclassified template"
            if purpose:
                lines.append(f"- {label} (purpose: {purpose[:240]})")
            else:
                lines.append(f"- {label}")
        return "\n".join(lines)

    async def _attach_template_matches(
        self,
        *,
        user_id: str,
        suggestions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Run ``_find_matching_template_for_suggestion`` per suggestion
        in parallel and attach ``templateId`` / ``templateName`` /
        ``matchMode`` to each item.

        Per-item failures are isolated — a single matcher failure does
        not poison the whole panel; the item just comes back without
        the template fields (frontend behaves as if no match was
        attempted).
        """

        async def _match_one(item: Dict[str, Any]) -> Dict[str, Any]:
            title = (item.get("title") or "").strip()
            description = (item.get("description") or "").strip()
            try:
                payload, library_size = await self._find_matching_template_for_suggestion(
                    user_id=user_id,
                    suggestion_title=title,
                    suggestion_description=description,
                    fallback_text="",
                )
            except Exception as exc:
                logger.warning(
                    "Pre-match for suggestion {!r} failed: {}", title[:60], exc
                )
                return item  # leave fields unset; click-time matcher will retry

            enriched = dict(item)
            if payload and isinstance(payload, dict):
                enriched["templateId"] = payload.get("template_id") or None
                enriched["templateName"] = payload.get("name") or None
                enriched["matchMode"] = "matched"
            elif library_size > 0:
                enriched["templateId"] = None
                enriched["templateName"] = None
                enriched["matchMode"] = "no-match"
            else:
                # Empty library: no point asserting "no match" — the
                # lawyer hasn't given us anything to match against.
                enriched["templateId"] = None
                enriched["templateName"] = None
                enriched["matchMode"] = None
            return enriched

        return await asyncio.gather(*(_match_one(s) for s in suggestions))

    async def _ensure_template_coverage(
        self,
        *,
        user_id: str,
        role: str,
        suggestions: List[Dict[str, Any]],
        ready_templates: List[Dict[str, Any]],
        case_summary: str,
        applicable_laws_brief: str = "",
        strategy_brief: str = "",
        analysis_brief: str = "",
        list_of_dates_brief: str = "",
    ) -> List[Dict[str, Any]]:
        """Guarantee every reusable template whose LEGAL PURPOSE fits this
        case surfaces as a suggested-draft card.

        The LLM suggestion step is non-deterministic and judges by case
        posture, so it can silently drop a perfectly applicable template when
        case details change (different client / brand / matter). This pass
        closes that gap: for every ready template NOT already bound to a
        matched card, it runs a focused applicability check keyed on the
        template's legal purpose + document type (never its original parties).
        Applicable templates get a guaranteed, case-framed card with the
        template pre-bound.

        Template-backed cards take priority over AI-only suggestions so an
        applicable template is never crowded out; the merged panel is capped
        at ``_MAX_SUGGESTIONS_WITH_TEMPLATES``. Returns the input unchanged
        when there is nothing to add.
        """
        if not user_id or not ready_templates:
            return suggestions

        # Templates already bound to a matched card need no separate coverage.
        covered_ids = {
            (s.get("templateId") or "").strip()
            for s in suggestions
            if isinstance(s, dict)
            and s.get("matchMode") == "matched"
            and (s.get("templateId") or "").strip()
        }
        uncovered = [
            t
            for t in ready_templates
            if (t.get("templateId") or "").strip()
            and (t.get("templateId") or "").strip() not in covered_ids
        ]
        if not uncovered:
            return suggestions

        async def _check_one(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            tid = (t.get("templateId") or "").strip()
            tname = (t.get("name") or "").strip()
            payload = t.get("payload") or {}
            doc_type = (
                payload.get("document_type")
                or t.get("document_type")
                or t.get("category")
                or ""
            ).strip()
            legal_purpose = (
                t.get("filing_context")
                or payload.get("filing_context")
                or t.get("purpose")
                or ""
            ).strip()
            prompt = build_template_applicability_prompt(
                role=role,
                case_summary=case_summary,
                applicable_laws_brief=applicable_laws_brief,
                strategy_brief=strategy_brief,
                analysis_brief=analysis_brief,
                list_of_dates_brief=list_of_dates_brief,
                template_document_type=doc_type,
                template_legal_purpose=legal_purpose,
            )
            try:
                raw = await gemini_client.generate(
                    prompt,
                    system_instruction=TEMPLATE_APPLICABILITY_INSTRUCTION,
                    temperature=0.1,
                    max_tokens=2000,
                    response_mime_type="application/json",
                )
            except Exception as exc:
                logger.warning(
                    "Template applicability check failed for {!r} "
                    "(non-fatal): {}",
                    tname[:60],
                    exc,
                )
                return None
            obj = _parse_json_object(raw)
            if not isinstance(obj, dict):
                return None
            # Coerce defensively: the model occasionally returns the string
            # "false" (truthy in Python) instead of a JSON boolean.
            applicable = obj.get("applicable")
            if isinstance(applicable, str):
                applicable = applicable.strip().lower() in {"true", "yes", "1"}
            if not applicable:
                return None
            title = (obj.get("title") or "").strip() or doc_type or tname
            if not title:
                return None
            desc = (obj.get("description") or "").strip()
            if len(desc) > 220:
                desc = desc[:220].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
            logger.info(
                "Template coverage: template {} ({!r}) deemed applicable — "
                "guaranteeing card {!r}",
                tid,
                tname[:50],
                title[:80],
            )
            return {
                "title": title,
                "relevance": _normalize_relevance(obj.get("relevance")),
                "description": desc or None,
                "templateId": tid,
                "templateName": tname or None,
                "matchMode": "matched",
            }

        results = await asyncio.gather(*(_check_one(t) for t in uncovered))
        guaranteed = _filter_forbidden_acts(
            [r for r in results if isinstance(r, dict)]
        )
        if not guaranteed:
            return suggestions

        # Lock each guaranteed card's template so the kickoff click reuses
        # the SAME template the panel advertised (mirrors suggest()).
        for g in guaranteed:
            self._remember_prematch(user_id, g.get("title") or "", g["templateId"])

        # Merge in priority order so no template-backed card is crowded out
        # by an AI-only suggestion: new guaranteed cards, then existing
        # template-backed (matched) cards, then AI-only cards. Dedupe by
        # templateId first, then by normalized title; cap the result.
        existing_matched = [
            s
            for s in suggestions
            if isinstance(s, dict)
            and s.get("matchMode") == "matched"
            and (s.get("templateId") or "").strip()
        ]
        existing_other = [
            s
            for s in suggestions
            if not (
                isinstance(s, dict)
                and s.get("matchMode") == "matched"
                and (s.get("templateId") or "").strip()
            )
        ]

        merged: List[Dict[str, Any]] = []
        seen_ids: set = set()
        seen_titles: set = set()

        def _push(item: Dict[str, Any]) -> None:
            if not isinstance(item, dict):
                return
            tid = (item.get("templateId") or "").strip()
            tkey = re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())
            if tid and tid in seen_ids:
                return
            if tkey and tkey in seen_titles:
                return
            if tid:
                seen_ids.add(tid)
            if tkey:
                seen_titles.add(tkey)
            merged.append(item)

        for item in (*guaranteed, *existing_matched, *existing_other):
            _push(item)

        capped = merged[:_MAX_SUGGESTIONS_WITH_TEMPLATES]
        logger.info(
            "Template coverage: {} guaranteed template card(s) added; "
            "panel {} -> {} card(s)",
            len(guaranteed),
            len(suggestions),
            len(capped),
        )
        return capped

    async def rematch_unmatched_suggestions(
        self,
        *,
        user_id: str,
        suggestions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Refresh an *already-emitted* suggestions panel after the lawyer's
        template library grew, WITHOUT disturbing matched cards.

        Freeze semantics (the lawyer's requested behaviour):
          - A card with ``matchMode == "matched"`` whose ``templateId`` still
            exists in the library is kept verbatim — the template it
            advertised stays its template, even though a new template was
            just uploaded.
          - A matched card whose ``templateId`` was deleted is demoted to
            unmatched (its stale prematch lock is released) so it can pick up
            a current template.
          - Only the unmatched cards are re-run through
            ``_attach_template_matches`` against the now-larger library, so a
            freshly-uploaded template can fill in a card that previously had
            no match.

        Order is preserved. Used by the socket re-emit path instead of a
        full LLM regeneration, which would re-roll the cards and lose their
        bindings. Falls back to returning the input unchanged on any error.
        """
        if not suggestions:
            return suggestions

        frozen_idx: Dict[int, Dict[str, Any]] = {}
        to_match: List[Tuple[int, Dict[str, Any]]] = []
        for idx, item in enumerate(suggestions):
            if not isinstance(item, dict):
                frozen_idx[idx] = item
                continue
            tid = (item.get("templateId") or "").strip()
            is_matched = (item.get("matchMode") == "matched") and bool(tid)
            if is_matched:
                exists = await self.get_template(user_id=user_id, template_id=tid)
                if exists and (exists.get("extracted_text") or "").strip():
                    frozen_idx[idx] = item  # keep the advertised binding
                    # Re-assert the lock from the payload so a click without a
                    # forwarded templateId still recalls THIS template — even
                    # if the in-memory lock was lost across an AI restart.
                    self._remember_prematch(user_id, item.get("title") or "", tid)
                    continue
                # Locked template gone — release the lock and re-match.
                self._forget_prematch(user_id, item.get("title") or "")
                demoted = dict(item)
                demoted["templateId"] = None
                demoted["templateName"] = None
                demoted["matchMode"] = None
                to_match.append((idx, demoted))
            else:
                to_match.append((idx, item))

        rematched: Dict[int, Dict[str, Any]] = {}
        if to_match:
            try:
                results = await self._attach_template_matches(
                    user_id=user_id,
                    suggestions=[item for _, item in to_match],
                )
            except Exception as exc:
                logger.warning(
                    "rematch_unmatched_suggestions: _attach_template_matches "
                    "failed (non-fatal, keeping prior cards): {}",
                    exc,
                )
                results = [item for _, item in to_match]
            for (idx, _orig), enriched in zip(to_match, results):
                rematched[idx] = enriched
                # Lock any newly-acquired binding so the click reuses it.
                if (
                    isinstance(enriched, dict)
                    and enriched.get("matchMode") == "matched"
                    and enriched.get("templateId")
                ):
                    self._remember_prematch(
                        user_id, enriched.get("title") or "", enriched["templateId"]
                    )

        merged: List[Dict[str, Any]] = []
        for idx in range(len(suggestions)):
            if idx in frozen_idx:
                merged.append(frozen_idx[idx])
            else:
                merged.append(rematched.get(idx, suggestions[idx]))
        logger.info(
            "rematch_unmatched_suggestions: {} card(s) — {} frozen, {} re-matched",
            len(suggestions),
            len(frozen_idx),
            len(to_match),
        )
        return merged

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
        kind: Optional[str] = None,
        formatting: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Download a template, extract text, and persist under the user cache.

        Returns the cache payload on success, None on failure. If
        `previous_payload` has a matching `file_hash`, the existing
        extracted text is preserved (no re-download).

        When `formatting.blueprint` is supplied (a re-upload of an
        already-approved template), that lawyer-approved blueprint is stored as
        authoritative and the AI profiler is NOT re-run.
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
        document_purpose = ""
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
            document_purpose = (previous_payload.get("document_purpose") or "").strip()
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
                document_purpose = classified.get("purpose", "") or ""
                classified_at = now_iso

        # Drafting blueprint profile — tone / filing_context / structure_outline
        # / interactive_variables. Reuse the cached profile when the file hash
        # is unchanged AND a previous profile exists; otherwise extract it from
        # the freshly-extracted body. Never block ingest on profiler failure —
        # the kickoff falls back to its freeform placeholder scan when absent.
        interactive_variables: List[Dict[str, Any]] = []
        template_tone = ""
        filing_context = ""
        structure_outline: List[str] = []
        profiled_at = ""
        profile_schema_version = 0
        # "AI Suggested Fields" (quick-add chips) — carried forward, refreshed
        # on a fresh profile. Approved-blueprint re-uploads keep the prior set.
        quick_add: List[str] = list((previous_payload or {}).get("quick_add") or [])
        # Lawyer-approved blueprint controls (override the AI's defaults). Carried
        # forward from a previous payload, or set when an approved blueprint is
        # supplied on this event.
        draft_structure = (previous_payload or {}).get("draft_structure") or ""
        draft_depth = (previous_payload or {}).get("draft_depth") or ""
        guardrail_level = (previous_payload or {}).get("guardrail_level") or ""
        blueprint_approved = bool((previous_payload or {}).get("blueprint_approved"))

        supplied_blueprint = (formatting or {}).get("blueprint") if formatting else None
        if supplied_blueprint:
            # Scenario B: a re-upload that already carries the approved blueprint
            # — store it authoritative, do NOT re-profile.
            resolved = await self._resolve_approved_blueprint(
                blueprint=supplied_blueprint,
                existing_variables=(previous_payload or {}).get("interactive_variables") or [],
                document_type=document_type,
            )
            interactive_variables = resolved["interactive_variables"]
            template_tone = resolved["template_tone"]
            filing_context = resolved["filing_context"]
            structure_outline = resolved["structure_outline"]
            draft_structure = resolved["draft_structure"]
            draft_depth = resolved["draft_depth"]
            guardrail_level = resolved["guardrail_level"]
            blueprint_approved = True
            profiled_at = now_iso
            profile_schema_version = PROFILE_SCHEMA_VERSION
        elif (
            hash_stable
            and previous_payload
            and previous_payload.get("interactive_variables")
            and previous_payload.get("profile_schema_version") == PROFILE_SCHEMA_VERSION
        ):
            interactive_variables = (
                previous_payload.get("interactive_variables") or []
            )
            template_tone = (previous_payload.get("template_tone") or "").strip()
            filing_context = (previous_payload.get("filing_context") or "").strip()
            prev_outline = previous_payload.get("structure_outline") or []
            if isinstance(prev_outline, list):
                structure_outline = [str(s).strip() for s in prev_outline if str(s).strip()]
            profiled_at = previous_payload.get("profiled_at") or ""
            profile_schema_version = previous_payload.get("profile_schema_version") or 0
        elif extraction_status == "ready" and extracted_text:
            profile = await self._extract_template_profile(
                name=name,
                document_type=document_type,
                extracted_text=extracted_text,
                docx_bytes=downloaded_bytes if ext_lower == "docx" else None,
            )
            if profile:
                interactive_variables = profile.get("interactive_variables") or []
                template_tone = profile.get("tone") or ""
                filing_context = profile.get("filing_context") or ""
                structure_outline = profile.get("structure_outline") or []
                quick_add = profile.get("quick_add") or []
                profiled_at = now_iso
                profile_schema_version = profile.get(
                    "profile_schema_version", PROFILE_SCHEMA_VERSION
                )

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
            "document_purpose": document_purpose,
            "classified_at": classified_at,
            # Drafting blueprint (used by the kickoff to ask the right
            # fill-questions and to render in the template's tone).
            "interactive_variables": interactive_variables,
            "template_tone": template_tone,
            "filing_context": filing_context,
            "structure_outline": structure_outline,
            "quick_add": quick_add,
            "profiled_at": profiled_at,
            "profile_schema_version": profile_schema_version,
            # Lawyer-approved blueprint controls (empty until approved).
            "draft_structure": draft_structure,
            "draft_depth": draft_depth,
            "guardrail_level": guardrail_level,
            "blueprint_approved": blueprint_approved,
            "kind": (kind or "file"),
        }

        try:
            from app.langgraph.template_store import template_store

            ok = await template_store.put(
                user_id=user_id,
                template_id=template_id,
                payload=payload,
            )
            if not ok:
                logger.warning(
                    "Template cache put returned False (user={}, template={})",
                    user_id,
                    template_id,
                )
                return None
        except Exception as e:
            logger.warning(
                "Template cache put raised (user={}, template={}): {}",
                user_id,
                template_id,
                e,
            )
            return None

        logger.success(
            "Template ingested (user={}, template={}, status={}, chars={}, "
            "document_type={!r}, confidence={!r}, purpose={!r}, "
            "variables={}, tone={!r}, filing_context={})",
            user_id,
            template_id,
            extraction_status,
            len(extracted_text),
            document_type or "(unclassified)",
            document_type_confidence or "(n/a)",
            document_purpose or "(none)",
            len(interactive_variables),
            template_tone or "(none)",
            "yes" if filing_context else "no",
        )
        return payload

    # ------------------------------------------------------------------
    # Drafting-blueprint approval loop (dashboard ↔ AI)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_label(text: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()

    @staticmethod
    def _slug(text: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")

    _EXECUTION_HINTS = (
        "advocate", "enrol", "enroll", "designation", "issuance", "place of",
        "witness", "signature", "signatory", "law firm", "letterhead",
        "authority letter", "notice period", "compliance period",
    )
    _KIND_HINTS = (
        ("date", "date"), ("dob", "date"), ("amount", "amount"),
        ("rate", "amount"), ("salary", "amount"), ("rent", "amount"),
        ("emi", "amount"), ("deposit", "amount"), ("compensation", "amount"),
        ("damages", "amount"), ("balance", "amount"),
        ("section", "statute"), ("act", "statute"), ("statute", "statute"),
        ("relief", "relief"), ("prayer", "relief"), ("demand", "relief"),
        ("name", "party"), ("client", "party"), ("recipient", "party"),
        ("advocate", "party"), ("address", "party"), ("email", "party"),
        ("phone", "party"), ("father", "party"), ("mother", "party"),
        ("ground", "substantiation"), ("breach", "substantiation"),
        ("misconduct", "substantiation"), ("cruelty", "substantiation"),
        ("infringement", "substantiation"), ("defamatory", "substantiation"),
        ("warranty", "substantiation"), ("representation", "substantiation"),
        ("facts", "substantiation"), ("statement", "substantiation"),
    )

    def _heuristic_enrich_variable(self, label: str) -> Dict[str, Any]:
        """Best-effort metadata for a name-only variable when LLM enrichment is
        unavailable. Aligns to the canonical variable vocabulary by keyword."""
        norm = self._norm_label(label)
        kind = "fact"
        for needle, k in self._KIND_HINTS:
            if needle in norm:
                kind = k
                break
        fill_from_case = not any(h in norm for h in self._EXECUTION_HINTS)
        return {
            "id": self._slug(label) or "var",
            "label": (label or "").strip()[:80],
            "question": f"What is the {(label or 'value').strip()}?"[:240],
            "kind": kind,
            "fill_from_case": fill_from_case,
            "required": True,
        }

    async def _enrich_variables(
        self, rows: List[Dict[str, Any]], document_type: str
    ) -> Dict[str, Dict[str, Any]]:
        """LLM-enrich name-only variables (one batched call) → {id: enriched}.

        Falls back to heuristics per row on any failure. `rows` carry at least a
        `label` (and an `id`)."""
        labels = [(r.get("id"), (r.get("label") or "").strip()) for r in rows if (r.get("label") or "").strip()]
        out: Dict[str, Dict[str, Any]] = {}
        if not labels:
            return out
        listing = "\n".join(f"- {lid}: {lab}" for lid, lab in labels)
        prompt = (
            f"DOCUMENT TYPE: {document_type or '(unclassified)'}\n"
            "For each interactive-variable label below (a fill-in field a lawyer "
            "added to an Indian legal-document template), return JSON. Keep ids "
            "EXACTLY as given.\n\n"
            f"VARIABLES:\n{listing}\n\n"
            "Output strict JSON only: {\"variables\":[{\"id\":\"<same id>\","
            "\"question\":\"<one focused question asking for this value>\","
            "\"kind\":\"<party|date|amount|statute|fact|relief|execution|"
            "substantiation|other>\",\"fillFromCase\":<true if normally in the "
            "case file, false if only the lawyer knows it at drafting time>,"
            "\"required\":<true|false>}]}"
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=(
                    "You map legal-document template variable names to their "
                    "drafting metadata. Indian-law grounding (BNS/BNSS/BSA). "
                    "JSON only, no commentary."
                ),
                temperature=0.1,
                max_tokens=4000,
                response_mime_type="application/json",
            )
            parsed = _parse_json_object(raw) or {}
            for item in parsed.get("variables") or []:
                if not isinstance(item, dict):
                    continue
                vid = (item.get("id") or "").strip().lower()
                if not vid:
                    continue
                fill = item.get("fillFromCase")
                if fill is None:
                    fill = item.get("fill_from_case")
                out[vid] = {
                    "question": (item.get("question") or "").strip(),
                    "kind": (item.get("kind") or "").strip().lower(),
                    "fill_from_case": bool(fill) if fill is not None else None,
                    "required": item.get("required"),
                }
        except Exception as e:
            logger.warning("Variable enrichment LLM call failed: {}", e)
        return out

    async def _autofill_variables_from_context(
        self,
        variables: List[Dict[str, Any]],
        case_context: Dict[str, Any],
    ) -> Dict[str, str]:
        """Extract a value for every registry variable the CASE CONTEXT clearly
        supports → ``{variable_id: value}``. One focused Gemini call so auto-fill
        is reliable (the kickoff LLM recites facts in prose but under-populates
        gatheredFacts). Omits variables the context does not clearly provide;
        never guesses/fabricates. Returns ``{}`` on failure."""
        vars_ = [v for v in (variables or []) if isinstance(v, dict) and v.get("id")]
        if not vars_:
            return {}
        listing = "\n".join(
            f"- {v.get('id')}: {(v.get('label') or '').strip()}"
            f" — {(v.get('question') or '').strip()}"
            for v in vars_
        )
        ctx_parts = [
            ("CASE SUMMARY", case_context.get("case_summary")),
            ("CASE FACTS / EVIDENCE", case_context.get("case_facts_brief")),
            ("APPLICABLE LAWS", case_context.get("applicable_laws_brief")),
            ("STRATEGY", case_context.get("strategy_brief")),
        ]
        context_block = "\n\n".join(
            f"{label}:\n{(val or '').strip()}"
            for label, val in ctx_parts
            if (val or "").strip()
        ) or "(no case context available)"
        prompt = (
            "From the CASE CONTEXT, extract the value for each VARIABLE below "
            "that the context CLEARLY and COMPLETELY supports. Return strict "
            "JSON only: {\"values\": {\"<variable id>\": \"<value exactly as "
            "the case states it>\"}}. OMIT any variable the context does not "
            "clearly provide — do NOT guess, infer loosely, or fabricate. Keep "
            "ids EXACTLY as given.\n\n"
            "QUALITY GATE — a value is only valid when it is COMPLETE and PROPER "
            "for a legal document. Reject and OMIT any variable whose extracted "
            "value fails any of these checks:\n"
            "• Names: must be a full legal name (first + last, or title + name). "
            "OMIT if the context only has an informal label such as 'the client', "
            "'the petitioner', 'the complainant', 'the respondent', 'the sister', "
            "'the husband', 'the opposite party', or any other role-reference "
            "without an actual name.\n"
            "• Dates: must be an explicit calendar date in DD/MM/YYYY or "
            "equivalent written form. OMIT if the context only says 'recently', "
            "'shortly after', 'last month', or any relative/vague time reference.\n"
            "• Amounts: must include both a currency symbol/code and a specific "
            "figure. OMIT if the context only says 'a large sum', 'significant "
            "amount', or any other non-specific monetary reference.\n"
            "• Contact identifiers (phone numbers, WhatsApp numbers, email "
            "addresses) are NEVER valid values for name, address, or "
            "client-detail variables — OMIT those variables entirely when the "
            "only identifier in context is a contact number or email.\n"
            "• When in doubt about whether a value is complete, OMIT it — a "
            "missing value triggers a question; a wrong value silently corrupts "
            "the document.\n\n"
            f"VARIABLES:\n{listing}\n\n"
            f"CASE CONTEXT:\n\"\"\"\n{context_block}\n\"\"\""
        )
        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=(
                    "You extract case-variable values from an Indian legal "
                    "matter's context for document drafting. Extract only what "
                    "is clearly present; never fabricate. JSON only."
                ),
                temperature=0.1,
                max_tokens=8000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Auto-fill extraction LLM call failed: {}", e)
            return {}
        parsed = _parse_json_object(raw) or {}
        values = parsed.get("values")
        if not isinstance(values, dict):
            # Tolerate a flat {id: value} object.
            values = parsed if isinstance(parsed, dict) else {}
        valid_ids = {(v.get("id") or "").strip().lower() for v in vars_}
        out: Dict[str, str] = {}
        for k, val in (values or {}).items():
            kid = str(k).strip().lower()
            if kid not in valid_ids:
                continue
            if isinstance(val, (str, int, float)):
                sval = str(val).strip()
                if sval and self._is_filled_value(sval):
                    out[kid] = sval
        return out

    async def _reconcile_variables(
        self,
        approved: Any,
        existing: List[Dict[str, Any]],
        document_type: str,
    ) -> List[Dict[str, Any]]:
        """Merge the lawyer-approved variable set with our existing registry.

        The approved list is the authoritative SET (added vars included,
        removed vars dropped). Matched vars reuse our AI metadata; name-only
        added vars are enriched. Returns normalized variable dicts."""
        existing_by_id: Dict[str, Dict[str, Any]] = {}
        existing_by_label: Dict[str, Dict[str, Any]] = {}
        for v in existing or []:
            if not isinstance(v, dict):
                continue
            vid = (v.get("id") or "").strip().lower()
            lab = self._norm_label(v.get("label"))
            if vid:
                existing_by_id[vid] = v
            if lab:
                existing_by_label[lab] = v

        rows: List[Dict[str, Any]] = []
        incomplete: List[Dict[str, Any]] = []
        for item in approved or []:
            if isinstance(item, str):
                item = {"label": item}
            if not isinstance(item, dict):
                continue
            label = (item.get("label") or "").strip()
            vid = (item.get("id") or "").strip().lower()
            if not (label or vid):
                continue
            match = None
            if vid and vid in existing_by_id:
                match = existing_by_id[vid]
            elif label and self._norm_label(label) in existing_by_label:
                match = existing_by_label[self._norm_label(label)]
            m = match or {}
            fill = item.get("fillFromCase")
            if fill is None:
                fill = item.get("fill_from_case")
            if fill is None:
                fill = m.get("fill_from_case")
            required = item.get("required")
            if required is None:
                required = m.get("required")
            row = {
                "id": vid or (m.get("id") or "").strip().lower() or self._slug(label) or f"var_{len(rows)+1}",
                "label": label or m.get("label") or "",
                "question": (item.get("question") or "").strip() or (m.get("question") or "").strip(),
                "kind": (item.get("kind") or "").strip().lower() or (m.get("kind") or "").strip().lower(),
                "fill_from_case": fill,
                "required": required,
            }
            rows.append(row)
            if not row["question"] or not row["kind"] or row["fill_from_case"] is None or row["required"] is None:
                incomplete.append(row)

        if incomplete:
            enriched = await self._enrich_variables(incomplete, document_type)
            for row in incomplete:
                e = enriched.get(row["id"])
                fallback = self._heuristic_enrich_variable(row["label"])
                src = e or {}
                if not row["question"]:
                    row["question"] = src.get("question") or fallback["question"]
                if not row["kind"]:
                    row["kind"] = src.get("kind") or fallback["kind"]
                if row["fill_from_case"] is None:
                    fv = src.get("fill_from_case")
                    row["fill_from_case"] = fv if fv is not None else fallback["fill_from_case"]
                if row["required"] is None:
                    rv = src.get("required")
                    row["required"] = rv if rv is not None else fallback["required"]

        # Final normalization (validates kind, caps count, de-dupes).
        return self._normalize_interactive_variables(rows)

    async def _resolve_approved_blueprint(
        self,
        *,
        blueprint: Dict[str, Any],
        existing_variables: List[Dict[str, Any]],
        document_type: str,
        existing_tone: str = "",
        existing_context: str = "",
        existing_outline: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Turn an approved blueprint dict into the internal stored fields."""
        bp = blueprint or {}
        variables = await self._reconcile_variables(
            bp.get("variables"), existing_variables, document_type
        )
        tone = (bp.get("tone") or "").strip() or (existing_tone or "")
        context = (bp.get("context") or "").strip() or (existing_context or "")
        outline = existing_outline if existing_outline is not None else []
        return {
            "interactive_variables": variables,
            "template_tone": tone[:160],
            "filing_context": context[:800],
            "structure_outline": [str(s).strip() for s in (outline or []) if str(s).strip()],
            "draft_structure": (bp.get("structure") or "").strip(),
            "draft_depth": (bp.get("depth") or "").strip(),
            "guardrail_level": (bp.get("legalGuardrails") or bp.get("legal_guardrails") or "").strip(),
        }

    async def build_blueprint_proposal(
        self, *, user_id: str, template_id: str, force_reprofile: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Build the AI's blueprint proposal for the dashboard approval screen.

        Profiles the template on demand if it was not profiled at ingest.
        When ``force_reprofile`` is True (the "Regenerate Extra Info" action),
        the profiler is re-run from the cached text even if a profile already
        exists, and any prior approval is reset (a fresh proposal supersedes it
        until the lawyer re-approves). Returns the `template_blueprint_proposal`
        payload, or None when the template is unknown."""
        tpl = await self.get_template(user_id=user_id, template_id=template_id)
        if not tpl:
            logger.warning(
                "build_blueprint_proposal: template not in store (user={}, template={})",
                str(user_id)[:12],
                template_id,
            )
            return None

        variables = tpl.get("interactive_variables") or []
        tone = (tpl.get("template_tone") or "").strip()
        context = (tpl.get("filing_context") or "").strip()
        outline = tpl.get("structure_outline") or []
        # Profile now if ingest did not (profiler failed / older path), OR when a
        # regenerate was explicitly requested (force a fresh profile).
        if (force_reprofile or not variables) and (tpl.get("extracted_text") or "").strip():
            profile = await self._extract_template_profile(
                name=tpl.get("name") or "",
                document_type=tpl.get("document_type") or "",
                extracted_text=tpl.get("extracted_text") or "",
                docx_bytes=None,
            )
            if profile:
                variables = profile.get("interactive_variables") or []
                tone = (profile.get("tone") or "").strip() or tone
                context = (profile.get("filing_context") or "").strip() or context
                outline = profile.get("structure_outline") or outline
                updated = {
                    **tpl,
                    "interactive_variables": variables,
                    "template_tone": tone,
                    "filing_context": context,
                    "structure_outline": outline,
                    "quick_add": profile.get("quick_add") or [],
                    "profiled_at": datetime.utcnow().isoformat(),
                    "profile_schema_version": PROFILE_SCHEMA_VERSION,
                }
                # A regenerated proposal is no longer the previously-approved
                # blueprint — reset the flag so it awaits fresh approval.
                if force_reprofile:
                    updated["blueprint_approved"] = False
                try:
                    from app.langgraph.template_store import template_store

                    await template_store.put(
                        user_id=user_id, template_id=template_id, payload=updated
                    )
                    tpl = updated
                except Exception as e:
                    logger.warning("build_blueprint_proposal: persist failed: {}", e)

        proposal_vars = [
            {
                "id": v.get("id"),
                "label": v.get("label"),
                "question": v.get("question"),
                "kind": v.get("kind"),
                "fillFromCase": bool(v.get("fill_from_case", True)),
                "required": bool(v.get("required", True)),
            }
            for v in variables
            if isinstance(v, dict)
        ]
        quick_add = [
            str(q).strip() for q in (tpl.get("quick_add") or []) if str(q).strip()
        ]
        status = "ready" if (proposal_vars or tone or context) else "failed"
        return {
            "userId": user_id,
            "templateId": template_id,
            "fileHash": tpl.get("file_hash") or "",
            "status": status,
            # Library pill grouping, derived from the classified document type.
            "draftCategory": self._draft_category_for(tpl.get("document_type") or ""),
            "blueprint": {
                "context": context,
                "tone": tone,
                "structure": tpl.get("draft_structure") or _DEFAULT_STRUCTURE_STYLE,
                "depth": tpl.get("draft_depth") or _DEFAULT_DRAFT_DEPTH,
                "legalGuardrails": tpl.get("guardrail_level") or _DEFAULT_GUARDRAIL_LEVEL,
                "variables": proposal_vars,
                # Optional one-click "AI Suggested Fields" chips.
                "quickAdd": quick_add,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def _draft_category_for(document_type: str) -> str:
        """Map a classified document type to a library-pill category
        (Notice | Affidavit | Petition | Other). Keyword-based, order matters
        (notice wins over petition for 'Reply Notice', etc.)."""
        t = (document_type or "").strip().lower()
        if not t:
            return "Other"
        if "notice" in t:
            return "Notice"
        if "affidavit" in t:
            return "Affidavit"
        if any(
            w in t for w in (
                "petition", "plaint", "written statement", "application",
                "complaint", "appeal", "suit", "bail", "writ",
            )
        ):
            return "Petition"
        return "Other"

    async def apply_approved_blueprint(
        self, *, user_id: str, template_id: str, blueprint: Dict[str, Any]
    ) -> bool:
        """Store the lawyer-approved blueprint as authoritative (file unchanged).

        Reconciles the approved variable set with our registry, enriches any
        name-only added variables, and persists the controls (tone, context,
        structure, depth, guardrails). Returns False when the template is
        unknown (the snapshot will backfill it later)."""
        tpl = await self.get_template(user_id=user_id, template_id=template_id)
        if not tpl:
            logger.warning(
                "apply_approved_blueprint: template not in store (user={}, template={})",
                str(user_id)[:12],
                template_id,
            )
            return False
        resolved = await self._resolve_approved_blueprint(
            blueprint=blueprint or {},
            existing_variables=tpl.get("interactive_variables") or [],
            document_type=tpl.get("document_type") or "",
            existing_tone=(tpl.get("template_tone") or ""),
            existing_context=(tpl.get("filing_context") or ""),
            existing_outline=tpl.get("structure_outline") or [],
        )
        updated = {
            **tpl,
            "interactive_variables": resolved["interactive_variables"],
            "template_tone": resolved["template_tone"],
            "filing_context": resolved["filing_context"],
            "structure_outline": resolved["structure_outline"],
            "draft_structure": resolved["draft_structure"],
            "draft_depth": resolved["draft_depth"],
            "guardrail_level": resolved["guardrail_level"],
            "blueprint_approved": True,
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "profiled_at": datetime.utcnow().isoformat(),
        }
        try:
            from app.langgraph.template_store import template_store

            await template_store.put(
                user_id=user_id, template_id=template_id, payload=updated
            )
        except Exception as e:
            logger.warning("apply_approved_blueprint: persist failed: {}", e)
            return False
        logger.success(
            "Applied approved blueprint (template={}, vars={}, tone={!r}, "
            "depth={!r}, structure={!r}, guardrails={!r})",
            template_id,
            len(resolved["interactive_variables"]),
            resolved["template_tone"] or "(none)",
            resolved["draft_depth"] or "(none)",
            resolved["draft_structure"] or "(none)",
            resolved["guardrail_level"] or "(none)",
        )
        return True

    async def store_preset(
        self,
        *,
        user_id: str,
        template_id: str,
        name: str,
        category: str,
        formatting: Optional[Dict[str, Any]],
    ) -> bool:
        """Cache a court/letterhead PRESET (no file body) — its page/court/
        letterhead layout is applied at DOCX export, not used as draft content."""
        now_iso = datetime.utcnow().isoformat()
        payload = {
            "template_id": template_id,
            "user_id": user_id,
            "name": name or template_id,
            "category": category or "Custom",
            "kind": "preset",
            "extension": "",
            "extracted_text": "",
            "extraction_status": "n/a",
            "formatting": formatting or {},
            "ingested_at": now_iso,
        }
        try:
            from app.langgraph.template_store import template_store

            await template_store.put(
                user_id=user_id, template_id=template_id, payload=payload
            )
        except Exception as e:
            logger.warning("store_preset: persist failed (template={}): {}", template_id, e)
            return False
        logger.success(
            "Stored preset (template={}, category={!r})", template_id, category or "(none)"
        )
        return True

    async def rename_template(
        self,
        *,
        user_id: str,
        template_id: str,
        new_name: str,
        previous_name: Optional[str],
    ) -> bool:
        """Patch the cached template's name; content untouched."""
        from app.langgraph.template_store import template_store

        existing = await template_store.get(
            user_id=user_id,
            template_id=template_id,
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
        ok = await template_store.put(
            user_id=user_id,
            template_id=template_id,
            payload=existing,
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
        from app.langgraph.template_store import template_store

        ok = await template_store.delete(
            user_id=user_id,
            template_id=template_id,
        )
        # Always release any suggestion pre-match locks pointing at this
        # template — even if the store had already evicted it — so a later
        # suggested-draft click can never recall the deleted id.
        released = self._forget_prematch_by_template_id(user_id, template_id)
        if ok:
            logger.info(
                "Template deleted from cache (user={}, template={}, "
                "prematch_locks_released={})",
                user_id,
                template_id,
                released,
            )
        return ok

    async def get_template(
        self, *, user_id: str, template_id: str
    ) -> Optional[Dict[str, Any]]:
        from app.langgraph.template_store import template_store

        data = await template_store.get(
            user_id=user_id,
            template_id=template_id,
        )
        return data if isinstance(data, dict) else None

    async def _purge_orphan_template(
        self, *, user_id: str, template_id: str
    ) -> None:
        """Delete a template whose backing file was confirmed gone (404).

        Self-heal helper called when a download returns a definitive
        object-missing error. Best-effort and non-fatal — never raises
        into the render path.
        """
        try:
            ok = await self.delete_template(
                user_id=user_id, template_id=template_id
            )
            if ok:
                logger.info(
                    "Self-heal: purged orphan template {} for user {} "
                    "(backing file 404/NoSuchKey)",
                    template_id,
                    (user_id or "")[:12],
                )
        except Exception as exc:
            logger.warning(
                "Self-heal: failed to purge orphan template {}: {}",
                template_id,
                exc,
            )

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
        # One-line "what this document is for / when to use it" — gives the
        # matcher an explicit purpose signal beyond the coarse type label.
        purpose = " ".join(str(parsed.get("purpose") or "").split())[:280]
        return {
            "documentType": canonical,
            "aliases": aliases,
            "confidence": confidence,
            "purpose": purpose,
        }

    # How much of the template body to feed the profiler. Larger than the
    # classifier window because placeholders / execution fields (signing
    # date, witness, enrolment no.) commonly live at the very end of the
    # document, and the variable registry must catch those too.
    _PROFILE_CONTENT_CHARS = 16000
    _MAX_INTERACTIVE_VARIABLES = 40

    @staticmethod
    def _deterministic_placeholders(docx_bytes: Optional[bytes]) -> List[str]:
        """Best-effort literal-placeholder seed for the profiler.

        Reuses the DOCX placeholder regex (`extract_placeholders`) so every
        literal slot ([...], <...>, {{...}}, (Pascal_Case)) is guaranteed to
        reach the profiler. Returns the raw slot strings, de-duplicated and
        order-preserving. Never raises — returns [] when bytes are absent or
        unparseable (PDF-only templates, etc.)."""
        if not docx_bytes:
            return []
        try:
            from app.processing.docx_template_filler import extract_placeholders

            hits = extract_placeholders(docx_bytes)
        except Exception as exc:
            logger.debug("Profiler placeholder seed skipped: {}", exc)
            return []
        out: List[str] = []
        seen: set = set()
        for h in hits:
            raw = (getattr(h, "raw", "") or "").strip()
            if raw and raw not in seen:
                seen.add(raw)
                out.append(raw)
        return out

    def _normalize_interactive_variables(
        self, raw: Any
    ) -> List[Dict[str, Any]]:
        """Coerce the profiler's `interactive_variables` into a clean list of
        `{id, label, question, kind, fill_from_case, required}`. Drops empties,
        de-dupes by id, caps the count."""
        out: List[Dict[str, Any]] = []
        seen_ids: set = set()
        valid_kinds = set(TEMPLATE_VARIABLE_KINDS)
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            label = (item.get("label") or "").strip()
            question = (item.get("question") or "").strip()
            if not (label or question):
                continue
            vid = (item.get("id") or "").strip().lower()
            if not vid:
                vid = re.sub(r"[^a-z0-9]+", "_", (label or question).lower()).strip("_")
            vid = vid or f"var_{len(out) + 1}"
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            kind = (item.get("kind") or "").strip().lower()
            if kind not in valid_kinds:
                kind = "other"
            out.append(
                {
                    "id": vid,
                    "label": (label or question)[:80],
                    "question": (question or label)[:240],
                    "kind": kind,
                    # Default to True so an ambiguous field is auto-filled from
                    # the case rather than needlessly asked.
                    "fill_from_case": bool(item.get("fill_from_case", True)),
                    "required": bool(item.get("required", True)),
                }
            )
            if len(out) >= self._MAX_INTERACTIVE_VARIABLES:
                break
        return out

    async def _extract_template_profile(
        self,
        *,
        name: str,
        document_type: str,
        extracted_text: str,
        docx_bytes: Optional[bytes] = None,
    ) -> Optional[Dict[str, Any]]:
        """Extract the reusable "drafting blueprint" for a template: tone,
        filing_context, structure_outline, and the interactive-variable
        registry. Returns a dict on success, None on failure (callers treat
        None as "no profile" and fall back to the freeform kickoff scan —
        never block ingest on this)."""
        body = (extracted_text or "").strip()
        if not body:
            return None
        try:
            prompt = build_template_profile_prompt(
                name=name or "",
                document_type=document_type or "",
                content=body[: self._PROFILE_CONTENT_CHARS],
                deterministic_placeholders=self._deterministic_placeholders(docx_bytes),
            )
            raw = await gemini_client.generate(
                prompt,
                system_instruction=TEMPLATE_PROFILE_INSTRUCTION,
                temperature=0.1,
                max_tokens=6000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Template profile extraction LLM call failed: {}", e)
            return None

        parsed = _parse_json_object(raw) or {}
        tone = " ".join(str(parsed.get("tone") or "").split())[:160]
        filing_context = " ".join(
            str(parsed.get("filing_context") or "").split()
        )[:800]
        raw_outline = parsed.get("structure_outline") or []
        structure_outline: List[str] = []
        if isinstance(raw_outline, list):
            for s in raw_outline:
                section = " ".join(str(s or "").split())[:120]
                if section:
                    structure_outline.append(section)
                if len(structure_outline) >= 15:
                    break
        variables = self._normalize_interactive_variables(
            parsed.get("interactive_variables")
        )
        # Optional "AI Suggested Fields" — extra field NAMES the lawyer can
        # one-click add; de-duped against the registry labels, capped.
        quick_add: List[str] = []
        existing_labels = {
            (v.get("label") or "").strip().lower() for v in variables
        }
        raw_quick = parsed.get("quick_add") or []
        if isinstance(raw_quick, list):
            seen_q: set = set()
            for q in raw_quick:
                name = " ".join(str(q or "").split())[:60]
                key = name.lower()
                if name and key not in existing_labels and key not in seen_q:
                    seen_q.add(key)
                    quick_add.append(name)
                if len(quick_add) >= 8:
                    break
        if not (tone or filing_context or structure_outline or variables):
            return None
        return {
            "tone": tone,
            "filing_context": filing_context,
            "structure_outline": structure_outline,
            "interactive_variables": variables,
            "quick_add": quick_add,
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
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

    @staticmethod
    def _dedupe_template_entries(
        ready: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Collapse duplicate template entries — both backend-pushed
        duplicate ids AND the same document re-uploaded under a
        different filename.

        Dedupe key, in priority order:
          1. ``file_hash`` — identical bytes are an unambiguous duplicate
             regardless of name.
          2. Hash of the full normalised ``extracted_text`` — catches the
             same document saved under two names (e.g. "123.docx" and
             "General_Bail_Application_Template_India.docx" are the same
             bail template). Only applied when the body is substantial
             (>= 100 chars) so two short/empty stubs don't collide.
          3. Fallback: the template_id itself (never collapse — too
             little signal to be safe).

        When two entries collapse, the survivor is chosen for the BEST
        future match quality, not arbitrarily: the entry with the more
        informative name wins (more non-numeric word characters after
        normalisation — so "General Bail Application Template India"
        beats "123"), because the surviving name feeds anchor-token
        scoring on later turns. Ties break to the most-recently-ingested
        entry. Order is otherwise preserved so scoring stays stable.
        """
        if len(ready) <= 1:
            return ready

        def _key(entry: Dict[str, Any]) -> Any:
            data = entry.get("payload") or {}
            fhash = (data.get("file_hash") or "").strip()
            if fhash:
                return ("hash", fhash)
            body = (data.get("extracted_text") or "").strip()
            if len(body) >= 100:
                normalized = re.sub(r"\s+", " ", body).lower()
                digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
                return ("content", digest)
            # Too little signal to dedupe safely — keep unique.
            return ("unique", entry.get("templateId") or id(entry))

        def _name_informativeness(entry: Dict[str, Any]) -> int:
            # Count alphabetic word characters after normalisation
            # (numbers are dropped by _normalise_for_match), so a junk
            # name like "123" scores 0 and a descriptive name scores high.
            return len(DraftingAgent._normalise_for_match(entry.get("name") or ""))

        def _is_better_survivor(
            candidate: Dict[str, Any], current: Dict[str, Any]
        ) -> bool:
            cand_info = _name_informativeness(candidate)
            cur_info = _name_informativeness(current)
            if cand_info != cur_info:
                return cand_info > cur_info
            cand_ts = (candidate.get("payload") or {}).get("ingested_at") or ""
            cur_ts = (current.get("payload") or {}).get("ingested_at") or ""
            return cand_ts > cur_ts

        deduped: Dict[Any, Dict[str, Any]] = {}
        order: List[Any] = []
        for entry in ready:
            key = _key(entry)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = entry
                order.append(key)
                continue
            if _is_better_survivor(entry, existing):
                deduped[key] = entry

        result = [deduped[k] for k in order]
        if len(result) < len(ready):
            logger.info(
                "Template matcher deduped {} → {} entries "
                "(collapsed {} duplicate/same-content template(s))",
                len(ready),
                len(result),
                len(ready) - len(result),
            )
        return result

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
        from app.langgraph.template_match_graph import run_template_match

        return await run_template_match(
            self,
            user_id=user_id,
            suggestion_title=suggestion_title,
            suggestion_description=suggestion_description,
            fallback_text=fallback_text,
        )

    # ------------------------------------------------------------------
    # Template-match graph phases — orchestrated by `template_match_graph`
    # (gather → understand → decide). Kept as methods on the agent so the
    # graph nodes reuse the existing classifier / type-select / anchor /
    # LLM-tiebreak helpers; the graph adds the parallel "deep-agent"
    # understanding step and the conditional skip when the library is empty.
    # ------------------------------------------------------------------

    async def _tm_gather(
        self, *, user_id: str, suggestion_text: str
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Phase 1 — list the lawyer's templates from the in-memory store,
        keep the ready (text-bearing) ones, project scoring fields, and
        dedupe phantom duplicates. Returns ``(ready, ready_size)``."""
        try:
            from app.langgraph.template_store import template_store

            entries = await template_store.list_for_user(user_id=user_id)
        except Exception as e:
            logger.warning("Template matcher: template_store.list_for_user failed: {}", e)
            return [], 0
        library_size = len(entries) if entries else 0
        logger.info(
            "Template matcher invoked (user={}, library_size={}, suggestion_text={!r})",
            user_id[:12],
            library_size,
            suggestion_text[:160],
        )
        if not entries:
            return [], 0  # Empty library — fast-out, no LLM call.

        # Filter to ready, text-bearing templates and project the bits we
        # need for scoring. The **match blob** combines the template name
        # (+ history) with the first ~1500 chars of the extracted body —
        # opening lines are where document-type signal is densest (court
        # headers, party blocks, "BAIL APPLICATION", "MOST RESPECTFULLY
        # SHOWETH"), so this works even when filenames are uninformative
        # like `document.docx` or `Format_A.pdf`.
        ready: List[Dict[str, Any]] = []
        for ent in entries:
            # template_store.list_for_user returns payloads directly (not
            # {scope_id, data} wrappers), so each entry IS the payload.
            data = ent if isinstance(ent, dict) else None
            if not isinstance(data, dict):
                continue
            if (data.get("extraction_status") or "").lower() != "ready":
                continue
            extracted = (data.get("extracted_text") or "").strip()
            if not extracted:
                continue
            tid = (data.get("template_id") or "").strip()
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
                    "purpose": (data.get("document_purpose") or "").strip(),
                    # Fuller legal purpose captured during template profiling
                    # (2-4 sentences: purpose, governing statute, addressee,
                    # filing posture). Richer than document_purpose; used to
                    # inform suggestion generation and the applicability gate.
                    "filing_context": (data.get("filing_context") or "").strip(),
                    "document_type": (data.get("document_type") or "").strip(),
                    "match_blob": match_blob,
                    "content_head": content_head,
                    "payload": data,
                }
            )

        # Dedupe defensively. The backend has been observed pushing the
        # SAME logical template under two different template_ids (e.g.
        # "23 09 11 Affidavits EG Ezy.docx" + "23 09 11 Affidavits EG
        # Ezy", and the same legal-notice name twice with sequential
        # ids). Those phantom duplicates inflate the candidate set and
        # waste an LLM tiebreaker. Collapse them here so the matcher
        # sees each real template once. NOTE: the root cause is upstream
        # — the backend should assign one template_id per upload and
        # emit `deleted` events for removed/test files; this is a
        # client-side workaround, not the real fix.
        ready = self._dedupe_template_entries(ready)
        ready_size = len(ready)
        if not ready:
            return [], 0
        return ready, ready_size

    async def _tm_understand(
        self,
        *,
        user_id: str,
        suggestion_title: str,
        suggestion_description: str,
        fallback_text: str,
        ready: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Phase 2 — the "deep agents" understanding step.

        Runs focused sub-agents in parallel via ``asyncio.gather``: one
        classifies the suggestion's intent into a canonical document type,
        the others lazily classify any candidate template missing a
        ``document_type``. Backfilled classifications are persisted to the
        in-memory store so the next click is fast. Returns the suggestion's
        classification dict (or None)."""
        from app.langgraph.template_store import template_store

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
            updated["document_purpose"] = res.get("purpose") or ""
            updated["classified_at"] = datetime.utcnow().isoformat()
            t["payload"] = updated
            try:
                await template_store.put(
                    user_id=user_id,
                    template_id=t["templateId"],
                    payload=updated,
                )
            except Exception as e:
                logger.warning(
                    "Template matcher: lazy classification backfill put "
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
        return suggestion_class

    async def _tm_decide(
        self,
        *,
        user_id: str,
        suggestion_title: str,
        suggestion_description: str,
        suggestion_text: str,
        ready: List[Dict[str, Any]],
        ready_size: int,
        suggestion_class: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Phase 3 — pick the matching template: type-first (with LLM
        applicability verification) then anchor-overlap fallback, with an
        LLM tiebreaker on ambiguous shortlists. Returns the chosen template
        payload, or None when nothing applicable is found."""
        sugg_type = (
            (suggestion_class or {}).get("documentType") or ""
        ) if suggestion_class else ""

        type_match_payload = self._select_by_type(
            ready=ready,
            suggestion_class=suggestion_class,
        )
        if type_match_payload is not None and len(type_match_payload) == 1:
            # A shared document_type is NOT sufficient proof of a match —
            # the type buckets are coarse ("Miscellaneous Application"
            # lumps a CCTV-preservation plea with a consumer evidence
            # affidavit). Verify applicability with the LLM even for a
            # single type-match; on rejection, fall through to the
            # anchor path (which may find a genuinely-applicable template
            # of a different type) and ultimately to no-match.
            verified = await self._llm_tiebreak_template(
                user_id=user_id,
                suggestion_title=suggestion_title,
                suggestion_description=suggestion_description,
                candidates=type_match_payload,
                snippet_chars=1500,
                mode_label="type-verify",
            )
            if verified is not None:
                return verified["payload"]
            logger.info(
                "Drafting suggestion: single type-match (type={!r}) failed "
                "LLM applicability check — falling through to anchor/none",
                sugg_type,
            )
        elif type_match_payload is not None and len(type_match_payload) > 1:
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
                return chosen["payload"]
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
            return None

        if len(with_signal) == 1:
            entry, tok, phr = with_signal[0]
            # A single weak anchor overlap (e.g. just "application" or a
            # shared section number) is not proof the template fits.
            # Verify applicability with the LLM; reject → no match so the
            # draft is produced from scratch rather than forced into an
            # unrelated template.
            verified = await self._llm_tiebreak_template(
                user_id=user_id,
                suggestion_title=suggestion_title,
                suggestion_description=suggestion_description,
                candidates=[entry],
                snippet_chars=1500,
                mode_label="anchor-verify",
            )
            if verified is not None:
                logger.info(
                    "Drafting suggestion matched to template (user={}, "
                    "template={}, mode=anchor-verify, phrase_overlap={}, "
                    "token_overlap={})",
                    user_id[:12],
                    verified["templateId"],
                    phr,
                    tok,
                )
                return verified["payload"]
            logger.warning(
                "Drafting suggestion matcher: single anchor candidate "
                "(template={}, tok={}, phr={}) failed LLM applicability "
                "check — falling back to AI generation",
                entry["templateId"],
                tok,
                phr,
            )
            return None

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
            return None
        return chosen["payload"]

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
                    # Stored at ingest: a one-line "what this template is for".
                    # Gives the LLM an explicit purpose signal beyond the raw
                    # body excerpt so applicability judgments are sharper.
                    "purpose": entry.get("purpose") or "",
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
        # Read the menu (if any) the assistant offered on the previous
        # turn. When set, short user replies are interpreted against this
        # menu BEFORE generic ack / classifier paths run — this is the
        # fix for the "yes / no / bare-doc-name after a menu" loop where
        # the post-draft-ack fast-path was eating context-sensitive
        # replies and emitting "Glad the draft looks good".
        last_offered_options: List[str] = list(
            (session_state or {}).get("last_offered_options") or []
        )

        if self._is_greeting(user_message) and not has_explicit_template:
            # Greeting clears any pending menu — a "hi" is not an
            # option-pick.
            (session_state or {}).pop("last_offered_options", None)
            session_state = await self._handle_chat_question(
                user_message=user_message,
                case_context=case_context,
                session_state=session_state or {},
                conversation_history=conversation_history,
            )
        else:
            # Explicit-action kickoff is triggered by an unambiguous signal:
            # a template upload (`templateId`) or a suggested-draft click
            # (`suggestedDraftId`). `source="blank"` alone is too weak.
            # Also re-kick when the incoming id differs from the session's
            # current draft — handles clicking a different card after a
            # previous draft was already rendered for the same actionable_id.
            _incoming_suggestion = (
                request_fields.get("suggestedDraftId") or ""
            ).strip()
            _incoming_template = (
                request_fields.get("templateId") or ""
            ).strip()
            _session_suggestion = (session_state or {}).get(
                "suggested_draft_id"
            ) or ""
            _session_template = (session_state or {}).get("template_id") or ""
            _session_id_changed = (
                _incoming_suggestion
                and _incoming_suggestion != _session_suggestion
            ) or (
                _incoming_template
                and _incoming_template != _session_template
            )
            explicit_kickoff = has_explicit_template and (
                not session_state or _session_id_changed
            )

            if explicit_kickoff:
                session_state = await self._handle_kickoff(
                    user_message=user_message,
                    case_context=case_context,
                    request_fields=request_fields,
                )
            elif (session_state or {}).get("awaiting_template_choice"):
                # The matched template was deleted; we offered (use similar /
                # upload / draft from case file) and are now routing the
                # lawyer's choice. Explicit branch so the generic intent
                # classifier never overrides it.
                session_state = await self._handle_template_choice_reply(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state or {},
                    request_fields=request_fields,
                    conversation_history=conversation_history,
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
                and (session_state or {}).get("last_draft_markdown")
                and self._is_finalize_request(user_message)
            ):
                # Finalize fast-path — "Generate Final Draft" / "export
                # draft" / "finalise the draft". Re-renders the cached
                # markdown into a fresh DOCX, uploads, and returns a new
                # signed URL. No Gemini round-trip, no follow-up menu.
                session_state = await self._handle_finalize_export(
                    case_context=case_context,
                    session_state=session_state or {},
                    request_fields=request_fields,
                )
            elif last_offered_options and (
                _picked := self._match_offered_option(
                    user_message, last_offered_options
                )
            ):
                # Option-pick fast-path: the lawyer's reply names (or
                # numerically references) one of the documents the
                # assistant just offered. Treat as a fresh kickoff for
                # that document type and clear the menu.
                logger.info(
                    "Drafting menu pick: '{}' → '{}'",
                    user_message[:60],
                    _picked,
                )
                (session_state or {}).pop("last_offered_options", None)
                session_state = (session_state or {})
                session_state["draft_title"] = _picked
                session_state["source"] = "blank"
                # Reset draft-in-progress fields so the new kickoff
                # scoping does not inherit slots from the previous draft.
                for stale_key in (
                    "open_questions",
                    "gathered_facts",
                    "incompatible",
                    "_draft_just_delivered",
                ):
                    session_state.pop(stale_key, None)
                session_state["draft_status"] = "analyzing"
                session_state = await self._handle_kickoff(
                    user_message=f"Draft a {_picked}",
                    case_context=case_context,
                    request_fields={
                        **(request_fields or {}),
                        "source": "blank",
                        "suggestionTitle": _picked,
                    },
                )
            elif last_offered_options and self._is_pure_affirmation(user_message):
                # Ambiguous "yes" / "ok" with a pending menu — the lawyer
                # didn't say which one. Ask explicitly instead of
                # auto-picking or falling into the canned ack.
                opts_listing = "\n".join(
                    f"{i + 1}. **{opt}**"
                    for i, opt in enumerate(last_offered_options[:6])
                )
                session_state["last_assistant_message"] = (
                    "Which of these would you like me to draft? Reply "
                    "with the document name or the number:\n\n"
                    + opts_listing
                )
                # Keep last_offered_options set so the next-turn pick can
                # still resolve. open_questions stays as-is.
            elif last_offered_options and self._is_pure_negation(user_message):
                # "no" / "no thanks" to the menu — clear the offer and
                # route to chat for alternatives. The chat handler will
                # see the negation and either propose different options
                # or stay quiet, depending on context.
                (session_state or {}).pop("last_offered_options", None)
                session_state = await self._handle_chat_question(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state or {},
                    conversation_history=conversation_history,
                )
            else:
                # ---- LLM router (single Gemini call) ----
                # All remaining turns flow through one router decision so
                # the model — not phrase-based heuristics — picks the
                # next action. The router emits the assistant reply
                # inline for chat-shaped actions, or supplies structured
                # params for handler-shaped actions (kickoff / revise /
                # translate). This replaces the old post-draft-ack
                # canned response, the translate fast-path, and the
                # `_dispatch_classified_turn` → `_classify_drafting_turn_intent`
                # two-step.
                #
                # Fresh sessions reach this branch with `session_state ==
                # None`. The action-dispatcher below writes directly to
                # `session_state[...]`, so we materialise a mutable dict
                # here. Existing branches above use `(session_state or {})`
                # defensively; the router path needs the same safety but
                # in dict form because handlers below mutate it.
                if session_state is None:
                    session_state = {}

                # If a menu was pending but the lawyer's reply did not
                # fit any deterministic menu-aware branch above, clear
                # it before routing so the router does not double-count.
                if last_offered_options:
                    session_state.pop("last_offered_options", None)

                decision = await self._llm_route_turn(
                    user_message=user_message,
                    case_context=case_context,
                    session_state=session_state,
                    conversation_history=conversation_history,
                )
                action = decision.get("action") or "chat_reply"
                params = decision.get("params") or {}
                assistant_message = (decision.get("assistant_message") or "").strip()

                if action == "chat_reply":
                    # Plain Q&A / fallback. Use the router-emitted reply
                    # directly — no second Gemini call. Mirror the prior
                    # `_handle_chat_question` behaviour around active
                    # draft states: when a draft is in progress, do NOT
                    # clobber draft_status / open_questions / gathered
                    # facts; only swap the assistant message.
                    safe_reply = assistant_message or (
                        "I am not sure how to respond to that. Could "
                        "you rephrase?"
                    )
                    session_state["last_assistant_message"] = safe_reply
                    prior_status = (
                        (session_state.get("draft_status") or "").strip()
                    )
                    if prior_status not in self._ACTIVE_DRAFT_STATUSES:
                        session_state["draft_status"] = "chatting"
                        session_state["open_questions"] = []

                elif action == "post_draft_ack":
                    # Lawyer acknowledged the delivered draft. No state
                    # change beyond the assistant message.
                    safe_reply = assistant_message or (
                        "Glad the draft works. Let me know any time if "
                        "you would like edits."
                    )
                    session_state["last_assistant_message"] = safe_reply
                    session_state["open_questions"] = []

                elif action == "propose_alternatives":
                    # Router has produced a numbered list of alternative
                    # documents/actions. Persist the message; the
                    # end-of-turn `_extract_offered_options` call below
                    # will pick the menu up and populate
                    # `last_offered_options` so next turn's pick / yes /
                    # no resolves correctly. Preserve active draft state.
                    safe_reply = assistant_message or (
                        "Could you tell me more about what you would "
                        "like to draft next?"
                    )
                    session_state["last_assistant_message"] = safe_reply
                    prior_status = (
                        (session_state.get("draft_status") or "").strip()
                    )
                    if prior_status not in self._ACTIVE_DRAFT_STATUSES:
                        session_state["draft_status"] = "chatting"

                elif action == "kickoff_new_draft":
                    # Router identified a new-draft request. Reset stale
                    # draft-in-progress fields so the new kickoff does
                    # not inherit slots from the previous draft, then
                    # call `_handle_kickoff` exactly the same way the
                    # deterministic option-pick branch does.
                    doc_type = (
                        params.get("document_type")
                        if isinstance(params, dict)
                        else ""
                    ) or "New Draft"
                    doc_type = str(doc_type).strip() or "New Draft"
                    session_state["draft_title"] = doc_type
                    session_state["source"] = "blank"
                    for stale_key in (
                        "open_questions",
                        "gathered_facts",
                        "incompatible",
                        "_draft_just_delivered",
                        "last_offered_options",
                    ):
                        session_state.pop(stale_key, None)
                    session_state["draft_status"] = "analyzing"
                    session_state = await self._handle_kickoff(
                        user_message=f"Draft a {doc_type}",
                        case_context=case_context,
                        request_fields={
                            **(request_fields or {}),
                            "source": "blank",
                            "suggestionTitle": doc_type,
                        },
                    )

                elif action == "revise_draft":
                    # Router identified an edit-to-existing-draft
                    # request. Route to the same render-with-revision
                    # path the old classifier used.
                    revision_instruction = (
                        params.get("revision_instruction")
                        if isinstance(params, dict)
                        else None
                    )
                    revision_instruction = (
                        revision_instruction or user_message or ""
                    ).strip()
                    if not (session_state or {}).get("last_draft_markdown"):
                        # No draft yet — degrade to chat instead of
                        # mis-routing a revision against an empty draft.
                        session_state["last_assistant_message"] = (
                            "I do not yet have a draft to revise. Tell "
                            "me which document you would like me to "
                            "draft and I will get started."
                        )
                        prior_status = (
                            (session_state.get("draft_status") or "").strip()
                        )
                        if prior_status not in self._ACTIVE_DRAFT_STATUSES:
                            session_state["draft_status"] = "chatting"
                    else:
                        session_state = await self._render_and_attach(
                            case_context=case_context,
                            session_state=session_state or {},
                            lawyer_instructions=None,
                            mode_override="revision",
                            revision_instruction=revision_instruction,
                        )

                elif action == "translate_draft":
                    # Router identified a translation request. Pull the
                    # target language from params with a Hindi default
                    # so the handler always has something to work with.
                    target_language = (
                        params.get("target_language")
                        if isinstance(params, dict)
                        else None
                    )
                    target_language = (
                        str(target_language or "Hindi").strip() or "Hindi"
                    )
                    if not (session_state or {}).get("last_draft_markdown"):
                        session_state["last_assistant_message"] = (
                            "I do not yet have a draft to translate. "
                            "Tell me which document you would like me "
                            "to draft and I will get started."
                        )
                        prior_status = (
                            (session_state.get("draft_status") or "").strip()
                        )
                        if prior_status not in self._ACTIVE_DRAFT_STATUSES:
                            session_state["draft_status"] = "chatting"
                    else:
                        session_state = await self._handle_translate_draft(
                            target_language=target_language,
                            case_context=case_context,
                            session_state=session_state or {},
                        )

                else:
                    # Should be unreachable — router action vocab is
                    # validated inside `_llm_route_turn`. Defensive
                    # fallback to chat with the router's message.
                    safe_reply = assistant_message or (
                        "I am not sure how to respond to that. Could "
                        "you rephrase?"
                    )
                    session_state["last_assistant_message"] = safe_reply

        # Consume the per-turn delivery flag so it does NOT persist into the
        # next turn (BE only wants isDraft=true on the actual delivery turn).
        is_draft_delivery = bool(session_state.pop("_draft_just_delivered", False))

        # Pick up any numbered menu the assistant just emitted so the
        # NEXT turn can interpret short replies in context. We always
        # refresh this field from the new assistant message — when the
        # message has no menu, the value is cleared. Skipping this on
        # delivery turns is intentional: a delivered draft also ends
        # with markdown that may contain incidental numbered lists, but
        # those are part of the draft itself, not a menu of options the
        # lawyer is being asked to choose from.
        if not is_draft_delivery:
            new_assistant_msg = session_state.get("last_assistant_message") or ""
            extracted = self._extract_offered_options(new_assistant_msg)
            if extracted:
                session_state["last_offered_options"] = extracted
            else:
                session_state.pop("last_offered_options", None)
        else:
            # On a delivery turn, do not let a numbered list inside the
            # draft body be mistaken for an offered menu. Clear any stale
            # value so the next turn starts clean.
            session_state.pop("last_offered_options", None)

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
        allow_lazy_fetch: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Return the cached template payload, lazy-fetching if missing.

        ``allow_lazy_fetch=False`` disables the ``templateUrl`` fallback — used
        for suggested-draft clicks, where the store is already synced to the
        backend's CURRENT library, so a templateId absent from the store means
        the template was DELETED. Lazy-fetching it from the (stale) click URL
        would resurrect a deleted template, so we must not."""
        template_id = (request_fields.get("templateId") or "").strip()
        if not template_id:
            return None
        cached = await self.get_template(user_id=user_id, template_id=template_id)
        if cached and (cached.get("extracted_text") or "").strip():
            return cached
        if not allow_lazy_fetch:
            # Deleted (absent from the synced store) — do NOT resurrect it.
            return None
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

        # Recover the clicked card's title from the message when the backend
        # omits `suggestionTitle` (observed: the frontend embeds the title in
        # `content` as `Please draft a complete "<TITLE>" for this case`).
        # Without this the kickoff has no card identity — the pre-match recall
        # below can't fire and the lawyer sees "...for *this draft*...".
        if not suggestion_title and source == "suggestedDraft":
            recovered_title = self._recover_suggestion_title(user_message)
            # Don't mistake the `using the template "X"` phrasing (X is a
            # template NAME, handled separately below) for a suggestion title.
            if recovered_title and not self._recover_template_name(user_message):
                suggestion_title = recovered_title
                logger.info(
                    "Recovered suggestion title from kickoff content: {!r}",
                    recovered_title[:80],
                )

        # Bridge the suggestion-panel pre-match to this click. When the
        # panel advertised a matched template but the click request did not
        # carry `templateId` (backend/frontend dropped it), recall the
        # pre-matched id by title so the kickoff uses the SAME template the
        # lawyer saw on the card — instead of re-matching from scratch (a
        # non-deterministic LLM check that can disagree with the badge and
        # silently draft from scratch).
        if (
            source == "suggestedDraft"
            and not (request_fields.get("templateId") or "").strip()
        ):
            recalled_template_id = self._recall_prematch(user_id, suggestion_title or "")
            # Secondary bridge: the click names a template explicitly
            # (`using the template "<NAME>"`) but carries no templateId —
            # resolve the id from the store by name.
            if not recalled_template_id:
                named = self._recover_template_name(user_message)
                if named:
                    recalled_template_id = await self._resolve_template_id_by_name(
                        user_id=user_id, name=named
                    )
            if recalled_template_id:
                logger.info(
                    "Suggested-draft click without templateId — reusing "
                    "pre-matched template {} (title={!r}, user={})",
                    recalled_template_id,
                    (suggestion_title or "")[:60],
                    (user_id or "")[:12],
                )
                request_fields = {
                    **request_fields,
                    "templateId": recalled_template_id,
                }

        # Resolve template — three paths:
        #  - explicit template pick OR an *advertised* suggestion (the card
        #    already carries / recalls a templateId): honour that template
        #    STRICTLY. Never re-match it to a different one — a card that
        #    advertised template X must draft from X, even after the lawyer
        #    uploads another template. If X was deleted, draft from scratch
        #    with a note rather than silently substituting a new template.
        #  - suggested-draft click with NO advertised template (a genuinely
        #    unmatched card): scan the lawyer's library for a match so a
        #    freshly-uploaded template can still be picked up.
        #  - blank: no template lookup.
        template_payload: Optional[Dict[str, Any]] = None
        template_match_note: Optional[str] = None
        # When the suggested-draft path runs but no template matches AND
        # the library is non-empty, surface a short explanatory prefix on
        # the assistant's first reply so the lawyer learns why the upload
        # they expected to be reused wasn't picked.
        no_match_prefix: Optional[str] = None
        ready_library_size = 0

        # draftMode="custom": lawyer chose "No, Custom AI Draft" — skip ALL
        # template resolution (clearing templateId is not enough because
        # `source == "template"` would still enter the template block below).
        # Early-return directly to _scope_blank_draft so the draft is built
        # purely from case context with no template structure imposed.
        _draft_mode = (request_fields.get("draftMode") or "").strip().lower()
        if _draft_mode == "custom":
            return await self._scope_blank_draft(
                role=role,
                case_summary=case_summary,
                case_facts=case_facts,
                source="blank",
                lawyer_instructions=lawyer_instructions,
                suggestion_title=suggestion_title,
                suggested_draft_id=request_fields.get("suggestedDraftId"),
                no_match_prefix=None,
            )

        advertised_template_id = (request_fields.get("templateId") or "").strip()
        if source == "template" or advertised_template_id:
            # Suggested-draft clicks must NOT resurrect a deleted template from
            # the (stale) click URL — the store is already synced to the
            # backend's current library, so absence = deleted. Explicit library
            # opens (source="template") keep lazy-fetch.
            template_payload = await self._resolve_template(
                user_id=user_id,
                request_fields=request_fields,
                allow_lazy_fetch=(source != "suggestedDraft"),
            )
            # Advertised template is GONE (deleted) on a suggested-draft click:
            # do not silently draft from scratch — inform the lawyer it was
            # removed and offer to use a similar template, upload one, or draft
            # from the case file. Wait for their choice.
            if (
                template_payload is None
                and source == "suggestedDraft"
                and advertised_template_id
            ):
                logger.info(
                    "Advertised template {} no longer in library for user {} "
                    "— offering missing-template choice",
                    advertised_template_id,
                    (user_id or "")[:12],
                )
                self._forget_prematch(user_id, suggestion_title or "")
                return await self._build_missing_template_choice(
                    user_id=user_id,
                    request_fields=request_fields,
                    suggestion_title=suggestion_title,
                    suggestion_description=suggestion_description,
                    user_message=user_message,
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
            # Lazy backfill: templates ingested before the blueprint feature
            # (or whose profiling failed) carry no interactive_variables; and
            # templates profiled under an OLDER schema version carry a stale
            # registry. In both cases, run the profiler once now and persist it
            # so this and future clicks use the up-to-date variable registry.
            # Non-blocking — on failure the kickoff falls back to the freeform
            # placeholder scan.
            if not template_payload.get("blueprint_approved") and (
                not template_payload.get("interactive_variables")
                or template_payload.get("profile_schema_version") != PROFILE_SCHEMA_VERSION
            ):
                try:
                    profile = await self._extract_template_profile(
                        name=template_name or "",
                        document_type=template_payload.get("document_type") or "",
                        extracted_text=template_text,
                        docx_bytes=None,
                    )
                except Exception as exc:
                    logger.warning("Kickoff profile backfill raised: {}", exc)
                    profile = None
                if profile:
                    template_payload = {
                        **template_payload,
                        "interactive_variables": profile.get("interactive_variables") or [],
                        "template_tone": profile.get("tone") or "",
                        "filing_context": profile.get("filing_context") or "",
                        "structure_outline": profile.get("structure_outline") or [],
                        "profiled_at": datetime.utcnow().isoformat(),
                        "profile_schema_version": profile.get(
                            "profile_schema_version", PROFILE_SCHEMA_VERSION
                        ),
                    }
                    try:
                        from app.langgraph.template_store import template_store

                        await template_store.put(
                            user_id=user_id,
                            template_id=template_payload.get("template_id"),
                            payload=template_payload,
                        )
                        logger.info(
                            "Kickoff lazy-backfilled template blueprint "
                            "(template={}, variables={})",
                            template_payload.get("template_id"),
                            len(template_payload.get("interactive_variables") or []),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Kickoff profile backfill persist failed: {}", exc
                        )

            template_tone = template_payload.get("template_tone") or ""
            filing_context = template_payload.get("filing_context") or ""
            structure_outline = template_payload.get("structure_outline") or []
            draft_depth = template_payload.get("draft_depth") or ""
            draft_structure = template_payload.get("draft_structure") or ""
            guardrail_level = template_payload.get("guardrail_level") or ""
            registry_vars = template_payload.get("interactive_variables") or []

            # Deterministic auto-fill FIRST: mine the full case context (incl.
            # raw evidence) for every variable's value, so the kickoff treats
            # those as known and asks the lawyer ONLY for what's genuinely
            # absent — never for facts the case already supplies.
            prefilled_facts: Dict[str, Any] = {}
            if registry_vars:
                try:
                    prefilled_facts = await self._autofill_variables_from_context(
                        registry_vars, case_context
                    )
                except Exception as exc:
                    logger.warning("Kickoff auto-fill pass raised: {}", exc)
                    prefilled_facts = {}
                if prefilled_facts:
                    logger.info(
                        "Kickoff auto-filled {}/{} variable(s) from case context: {}",
                        len(prefilled_facts),
                        len(registry_vars),
                        list(prefilled_facts.keys()),
                    )

            prompt = build_template_kickoff_prompt(
                role=role,
                case_summary=case_summary,
                case_facts_brief=case_facts,
                template_name=template_name or "",
                template_text=template_text,
                lawyer_instructions=lawyer_instructions,
                draft_title_hint=suggestion_title,
                template_match_note=template_match_note,
                interactive_variables=registry_vars,
                template_tone=template_tone,
                filing_context=filing_context,
                structure_outline=structure_outline,
                prefilled_facts=prefilled_facts,
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
            open_qs = self._normalize_open_questions(
                parsed.get("openQuestions"), limit=self._MAX_INTERACTIVE_VARIABLES
            )
            gathered = parsed.get("gatheredFacts")
            if not isinstance(gathered, dict):
                gathered = {}
            # Merge the deterministic auto-fill UNDER the LLM's gatheredFacts:
            # start from the auto-filled values, then let the kickoff LLM add or
            # override with its own REAL values (never overwrite an auto-filled
            # value with an empty/placeholder one).
            if prefilled_facts:
                merged = dict(prefilled_facts)
                for k, v in gathered.items():
                    if self._is_filled_value(v):
                        merged[k] = v
                gathered = merged
            stripped_context = parsed.get("strippedContext") or []
            if not isinstance(stripped_context, list):
                stripped_context = []
            incompatible = bool(parsed.get("incompatible"))

            # Drop any open question the auto-fill/LLM already satisfied — never
            # ask for a value the case context supplied.
            if not incompatible and open_qs:
                open_qs = [
                    q for q in open_qs
                    if not self._is_filled_value(gathered.get(q.get("id")))
                ]

            # Deterministic coverage guarantee: every registry variable must be
            # auto-filled (real value in gathered) or asked. Catch any the LLM
            # silently skipped and surface them as questions so nothing is left
            # to render as a placeholder. Skipped when incompatible (no Qs then).
            if not incompatible:
                open_qs, _added_cov = self._ensure_registry_coverage(
                    template_payload.get("interactive_variables") or [],
                    gathered,
                    open_qs,
                )
                if _added_cov:
                    logger.info(
                        "Kickoff coverage added {} unaddressed registry "
                        "variable(s): {}",
                        len(_added_cov),
                        [q["id"] for q in _added_cov],
                    )
                # Present the COMPLETE question set as ONE grouped, numbered
                # Critical/Important/Optional list — so the lawyer answers
                # everything in a single round. The deterministic block (built
                # from open_qs, the source of truth) replaces the model's prose
                # question list, guaranteeing the chat matches the structured
                # inputs and that coverage-added fields are never relegated to a
                # confusing trailing addendum.
                if open_qs:
                    message = self._merge_full_questions_into_message(
                        message, open_qs, draft_title
                    )

            if not message:
                message = self._compose_questions_message(open_qs, draft_title)

            return {
                "source": "template",
                "template_id": template_payload.get("template_id"),
                "template_name": template_payload.get("name"),
                # Carry the template's tone / filing context + approved drafting
                # controls forward so the later render turn drafts in the
                # template's voice, depth, and structure.
                "template_tone": template_tone,
                "filing_context": filing_context,
                "structure_outline": structure_outline,
                "draft_depth": draft_depth,
                "draft_structure": draft_structure,
                "guardrail_level": guardrail_level,
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

    # ------------------------------------------------------------- missing template

    def _compose_missing_template_message(
        self, deleted_name: str, similar_name: Optional[str]
    ) -> str:
        """Inform the lawyer their matched template was removed and offer the
        recovery options (use similar / upload / draft from case file)."""
        name = (deleted_name or "the matched template").strip()
        lines = [
            f"The template **'{name}'** matched to this draft has been removed "
            f"from your workspace.",
            "",
            "**How would you like to proceed?**",
            "",
        ]
        opts: List[str] = []
        if similar_name:
            opts.append(
                f"**Use '{similar_name}'** from your library — I'll draft using it."
            )
        opts.append("**Upload a template** that fits this matter.")
        opts.append("**Draft from your case file** without a template.")
        for letter, opt in zip("abc", opts):
            lines.append(f"{letter}) {opt}")
        lines.append("")
        lines.append(
            "Reply with **(a)**, **(b)**, or **(c)** — or tell me in your own words."
        )
        return "\n".join(lines)

    async def _build_missing_template_choice(
        self,
        *,
        user_id: str,
        request_fields: Dict[str, Any],
        suggestion_title: Optional[str],
        suggestion_description: str,
        user_message: str,
    ) -> Dict[str, Any]:
        """Build the routing turn shown when a suggested-draft click points at a
        deleted template: find a similar template, offer the choices, and wait
        for the lawyer's reply (handled by `_handle_template_choice_reply`)."""
        deleted_name = (
            (request_fields.get("templateOriginalName") or "").strip()
            or (suggestion_title or "").strip()
            or "the matched template"
        )
        similar_id: Optional[str] = None
        similar_name: Optional[str] = None
        try:
            similar, _size = await self._find_matching_template_for_suggestion(
                user_id=user_id,
                suggestion_title=suggestion_title or "",
                suggestion_description=suggestion_description or "",
                fallback_text=(user_message or "").strip(),
            )
        except Exception as exc:
            logger.warning("missing-template similar search failed: {}", exc)
            similar = None
        if similar:
            similar_id = (similar.get("template_id") or "").strip() or None
            similar_name = (similar.get("name") or "").strip() or None

        options: List[str] = []
        if similar_name:
            options.append(f"Use '{similar_name}'")
        options.append("Upload a template")
        options.append("Draft from the case file")

        logger.info(
            "Missing-template choice offered (deleted={!r}, similar={!r}, user={})",
            deleted_name,
            similar_name or "(none)",
            (user_id or "")[:12],
        )
        return {
            "source": "suggestedDraft",
            "template_id": None,
            "template_name": None,
            "suggested_draft_id": request_fields.get("suggestedDraftId"),
            "suggestion_title": suggestion_title,
            "suggestion_description": suggestion_description,
            "draft_title": suggestion_title or "New Draft",
            "gathered_facts": {},
            "open_questions": [],
            "draft_status": "analyzing",
            "incompatible": False,
            "awaiting_template_choice": True,
            "missing_template_name": deleted_name,
            "similar_template_id": similar_id,
            "similar_template_name": similar_name,
            "last_offered_options": options,
            "current_draft_version": 0,
            "last_draft_document": None,
            "last_draft_markdown": "",
            "last_assistant_message": self._compose_missing_template_message(
                deleted_name, similar_name
            ),
            "created_at": datetime.utcnow().isoformat(),
        }

    def _classify_template_choice(self, text: str, has_similar: bool) -> str:
        """Classify the lawyer's reply to the missing-template offer →
        use_similar | upload | from_scratch | clarify. Deterministic; the
        letter map shifts when there is no 'similar' option."""
        t = (text or "").strip().lower()
        if not t:
            return "clarify"
        m = re.match(r"^\(?\s*([abc])\s*\)?$", t) or re.match(r"^option\s+([abc])\b", t)
        letter = m.group(1) if m else None
        if has_similar:
            letter_map = {"a": "use_similar", "b": "upload", "c": "from_scratch"}
        else:
            letter_map = {"a": "upload", "b": "from_scratch"}
        if letter and letter in letter_map:
            return letter_map[letter]
        if "upload" in t:
            return "upload"
        if any(
            w in t for w in (
                "scratch", "case file", "from the case", "without", "blank", "fresh",
            )
        ):
            return "from_scratch"
        if has_similar and any(
            w in t for w in ("use", "similar", "that one", "use it", "go with")
        ):
            return "use_similar"
        if has_similar and self._is_pure_affirmation(t):
            return "use_similar"
        if self._is_pure_negation(t):
            return "from_scratch"
        return "clarify"

    async def _handle_template_choice_reply(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        request_fields: Dict[str, Any],
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Route the lawyer's reply to the missing-template offer."""
        text = (user_message or "").strip()
        similar_id = (session_state.get("similar_template_id") or "").strip()
        similar_name = session_state.get("similar_template_name") or ""
        suggestion_title = (
            session_state.get("suggestion_title")
            or session_state.get("draft_title")
            or ""
        )
        user_id = (
            (request_fields.get("userId") or "").strip()
            or (session_state.get("user_id") or "").strip()
        )
        intent = self._classify_template_choice(text, has_similar=bool(similar_id))

        def _clear_choice(state: Dict[str, Any]) -> None:
            for k in (
                "awaiting_template_choice", "missing_template_name",
                "similar_template_id", "similar_template_name",
                "last_offered_options",
            ):
                state.pop(k, None)

        if intent == "use_similar" and similar_id:
            _clear_choice(session_state)
            if suggestion_title:
                self._remember_prematch(user_id, suggestion_title, similar_id)
            new_state = await self._handle_kickoff(
                user_message=user_message,
                case_context=case_context,
                request_fields={
                    **(request_fields or {}),
                    "source": "template",
                    "templateId": similar_id,
                    "suggestionTitle": suggestion_title,
                    "suggestedDraftId": session_state.get("suggested_draft_id"),
                },
            )
            for k in ("actionable_id", "user_id", "workspace_id", "created_at"):
                if session_state.get(k):
                    new_state[k] = session_state[k]
            return new_state

        if intent == "upload":
            _clear_choice(session_state)
            session_state["draft_status"] = "analyzing"
            session_state["open_questions"] = []
            session_state["last_assistant_message"] = (
                "Understood. Please upload a template that fits this matter and "
                "I will analyse it against the case context. You can also open a "
                "template directly from your library."
            )
            return session_state

        if intent == "from_scratch":
            _clear_choice(session_state)
            role = (case_context.get("role") or "ADJUDICATOR").upper()
            new_state = await self._scope_blank_draft(
                role=role,
                case_summary=case_context.get("case_summary") or "",
                case_facts=case_context.get("case_facts_brief") or "",
                source="blank",
                lawyer_instructions=None,
                suggestion_title=suggestion_title or None,
                suggested_draft_id=session_state.get("suggested_draft_id"),
            )
            for k in ("actionable_id", "user_id", "workspace_id", "created_at"):
                if session_state.get(k):
                    new_state[k] = session_state[k]
            return new_state

        # clarify / unknown — re-offer the same options.
        session_state["draft_status"] = "analyzing"
        session_state["last_assistant_message"] = self._compose_missing_template_message(
            session_state.get("missing_template_name") or "the matched template",
            similar_name or None,
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

    # LLM-driven turn router. Replaces the post-draft-ack canned response,
    # the translate fast-path, and the older `_dispatch_classified_turn`
    # → `_classify_drafting_turn_intent` two-step. Runs AFTER the cheap
    # deterministic shortcuts in `handle_turn` have missed.
    _ROUTER_ACTIONS = {
        "chat_reply",
        "post_draft_ack",
        "propose_alternatives",
        "kickoff_new_draft",
        "revise_draft",
        "translate_draft",
    }

    async def _llm_route_turn(
        self,
        *,
        user_message: str,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """One Gemini call that decides the next action for the turn.

        Returns ``{action, params, assistant_message, reasoning}``. For
        chat-shaped actions (chat_reply / post_draft_ack /
        propose_alternatives) the router emits ``assistant_message``
        directly, so the caller does not need a second Gemini round-trip
        to compose the reply. For action-shaped actions
        (kickoff_new_draft / revise_draft / translate_draft) the caller
        dispatches to the existing handler with the structured params.

        Failure mode: any LLM / JSON error returns a safe ``chat_reply``
        fallback so the turn never crashes the caller.
        """
        role = (case_context.get("role") or "ADJUDICATOR").upper()
        case_summary = (case_context.get("case_summary") or "")
        case_facts = (case_context.get("case_facts_brief") or "")

        draft_status = (session_state.get("draft_status") or "").strip()
        draft_title = (session_state.get("draft_title") or "").strip()
        last_draft_md = (session_state.get("last_draft_markdown") or "")
        last_draft_preview = last_draft_md.strip()[:400] if last_draft_md else ""
        pending_menu = list(session_state.get("last_offered_options") or [])
        gathered_keys = list((session_state.get("gathered_facts") or {}).keys())
        template_id = session_state.get("template_id")
        template_name = session_state.get("template_name")

        prompt = build_router_prompt(
            user_message=user_message,
            role=role,
            case_summary=case_summary,
            case_facts_brief=case_facts,
            draft_status=draft_status,
            draft_title=draft_title,
            last_draft_preview=last_draft_preview,
            has_pending_menu=bool(pending_menu),
            pending_menu_options=pending_menu,
            gathered_facts_keys=gathered_keys,
            template_id=template_id,
            template_name=template_name,
            conversation_history=conversation_history or [],
        )

        try:
            raw = await gemini_client.generate(
                prompt,
                system_instruction=DRAFTING_ROUTER_INSTRUCTION,
                temperature=0.2,
                max_tokens=6000,
                response_mime_type="application/json",
            )
        except Exception as exc:
            logger.error("Drafting router LLM call failed: {}", exc)
            return {
                "action": "chat_reply",
                "params": {},
                "assistant_message": (
                    "I ran into an issue replying just now. Could you "
                    "re-send your message, or rephrase it?"
                ),
                "reasoning": "llm_failure",
            }

        cleaned = _strip_code_fences(raw or "").strip()
        if not cleaned:
            return {
                "action": "chat_reply",
                "params": {},
                "assistant_message": (
                    "I am not sure how to respond to that. Could you "
                    "rephrase, or tell me which document you would like "
                    "me to help draft?"
                ),
                "reasoning": "empty_response",
            }

        try:
            data = json.loads(cleaned)
        except Exception as exc:
            logger.warning(
                "Drafting router JSON parse failed: {} (raw first 300 chars: {!r})",
                exc,
                cleaned[:300],
            )
            return {
                "action": "chat_reply",
                "params": {},
                "assistant_message": (
                    "I am not sure how to respond to that. Could you "
                    "rephrase?"
                ),
                "reasoning": "json_parse_failure",
            }

        if not isinstance(data, dict):
            return {
                "action": "chat_reply",
                "params": {},
                "assistant_message": (
                    "I am not sure how to respond to that. Could you "
                    "rephrase?"
                ),
                "reasoning": "non_object_response",
            }

        action = str(data.get("action") or "").strip().lower()
        if action not in self._ROUTER_ACTIONS:
            logger.warning(
                "Drafting router returned unknown action {!r}; "
                "falling back to chat_reply",
                action,
            )
            action = "chat_reply"

        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        assistant_message = (data.get("assistant_message") or "").strip()
        reasoning = (data.get("reasoning") or "").strip()

        logger.info(
            "Drafting router: action={}, reasoning={!r}",
            action,
            reasoning[:160],
        )
        return {
            "action": action,
            "params": params,
            "assistant_message": assistant_message,
            "reasoning": reasoning,
        }

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
        # Persist the active language so revision renders can re-apply it.
        # Cleared when the user explicitly switches back to English so that
        # subsequent revisions stay in English rather than re-translating.
        if target.lower() == "english":
            session_state.pop("draft_language", None)
        else:
            session_state["draft_language"] = target
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
            # Same long-form path as draft render (64k ceiling, thinking_level
            # LOW, auto-continuation) — a translated long rejoinder needs the
            # same room as the English original, and the same budget bias toward
            # full-length output rather than an expensive reasoning pass.
            raw = await gemini_client.generate_longform(
                prompt,
                system_instruction=DRAFT_TRANSLATE_INSTRUCTION,
                temperature=0.2,
                max_tokens=65536,
                thinking_level="LOW",
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

        session_state["draft_status"] = "ready"
        session_state["current_draft_version"] = new_version
        session_state["last_draft_markdown"] = translated
        session_state["last_draft_document"] = translated_doc
        session_state["last_assistant_message"] = translated
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
                    # Self-heal: a 404/NoSuchKey means the skeleton file was
                    # deleted on the backend — this cached template is an
                    # orphan. Purge it so it stops polluting future matches.
                    if template_id and _is_missing_object_error(exc):
                        await self._purge_orphan_template(
                            user_id=user_id, template_id=template_id
                        )

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
            # Self-heal: 404/NoSuchKey on the source means the file is
            # gone on the backend — purge this orphan from the cache so
            # it stops being matched. Transient/403 errors are left alone.
            if template_id and _is_missing_object_error(exc):
                await self._purge_orphan_template(
                    user_id=user_id, template_id=template_id
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
            from app.langgraph.template_store import template_store

            updated_payload = dict(tpl)
            updated_payload["template_docx_gcs_uri"] = new_uri
            await template_store.put(
                user_id=user_id,
                template_id=template_id,
                payload=updated_payload,
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

        # Template tone / filing context — drives the draft's voice (template
        # tone wins over the default register). Prefer the values carried in
        # session_state from kickoff; fall back to the freshly-loaded template
        # payload (e.g. after a reconnect rebuilt the session).
        template_tone = (session_state.get("template_tone") or "").strip()
        filing_context = (session_state.get("filing_context") or "").strip()
        if tpl and not template_tone:
            template_tone = (tpl.get("template_tone") or "").strip()
        if tpl and not filing_context:
            filing_context = (tpl.get("filing_context") or "").strip()

        # Lawyer-approved drafting controls (depth / structure / guardrails).
        # Prefer the values carried in session_state from kickoff; fall back to
        # the freshly-loaded template payload. Empty → render keeps its default.
        draft_depth = (session_state.get("draft_depth") or "").strip()
        draft_structure = (session_state.get("draft_structure") or "").strip()
        guardrail_level = (session_state.get("guardrail_level") or "").strip()
        if tpl:
            draft_depth = draft_depth or (tpl.get("draft_depth") or "").strip()
            draft_structure = draft_structure or (tpl.get("draft_structure") or "").strip()
            guardrail_level = guardrail_level or (tpl.get("guardrail_level") or "").strip()

        # Diagnostic: make it unambiguous whether this render is driven by a
        # template (and which one) or is a from-scratch generation. If
        # template_chars=0 here, the draft CANNOT mirror any template — the
        # issue is upstream (no templateId on the click / template not in the
        # store), not the render prompt.
        logger.info(
            "🖋️ Draft render: mode={} template_id={} template_name={!r} "
            "template_chars={} gathered_facts={}",
            mode,
            template_id or "(none)",
            template_name or "(none)",
            len(template_text or ""),
            len(gathered) if isinstance(gathered, dict) else 0,
        )

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
            template_tone=template_tone or None,
            filing_context=filing_context or None,
            draft_depth=draft_depth or None,
            structure_style=draft_structure or None,
            guardrail_level=guardrail_level or None,
        )
        try:
            # LOW thinking preserves the full 64k output budget for draft text;
            # MEDIUM/HIGH consumes ~60k on reasoning leaving only ~2-3k visible,
            # causing truncation. Matches the profile/kickoff long-form paths.
            # Falls back to legacy generate() if SDK absent.
            raw = await gemini_client.generate_longform(
                prompt,
                system_instruction=DRAFT_RENDER_INSTRUCTION,
                temperature=0.35,
                max_tokens=65536,
                thinking_level="LOW",
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

        # Re-render safety net: a revision/re-render must never re-blank a field
        # that was already filled (e.g. fill the execution date and lose the
        # enrolment number). Restore any value the new draft turned back into a
        # placeholder, using the previous draft as the source of truth.
        prior_markdown = (session_state.get("last_draft_markdown") or "").strip()
        if prior_markdown:
            markdown, _restored = _restore_regressed_values(prior_markdown, markdown)
            if _restored:
                logger.info(
                    "Re-render restored {} re-blanked field(s) from the prior "
                    "draft: {}",
                    len(_restored),
                    _restored,
                )

        # If the draft was previously translated, re-apply the same language so
        # the revised draft is delivered in the user's chosen language rather than
        # reverting to English. Non-fatal: on failure the English revision ships.
        _active_lang = (session_state.get("draft_language") or "").strip()
        if _active_lang and _active_lang.lower() not in ("", "english"):
            _tr_prompt = build_draft_translate_prompt(
                role=role,
                case_summary=case_summary,
                draft_markdown=markdown,
                target_language=_active_lang,
            )
            try:
                _tr_raw = await gemini_client.generate_longform(
                    _tr_prompt,
                    system_instruction=DRAFT_TRANSLATE_INSTRUCTION,
                    temperature=0.2,
                    max_tokens=65536,
                    thinking_level="LOW",
                )
                _tr_text = _strip_code_fences(_tr_raw).strip()
                if _tr_text:
                    markdown = _tr_text
                    logger.info(
                        "Post-revision re-translation applied (lang={})",
                        _active_lang,
                    )
                else:
                    logger.warning(
                        "Post-revision re-translation returned empty for lang={}; "
                        "keeping English revision",
                        _active_lang,
                    )
            except Exception as _tr_err:
                logger.warning(
                    "Post-revision re-translation failed (lang={}): {} — "
                    "keeping English revision",
                    _active_lang,
                    _tr_err,
                )

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
        # Log any slots that survived as unfilled placeholders — diagnostic
        # only; the draft ships regardless and the lawyer can fill gaps in a
        # follow-up turn. Not surfaced in the user-facing message.
        residual = _residual_placeholders(markdown)
        if residual:
            logger.info(
                "Render left {} residual placeholder(s) (template={}): {}",
                len(residual),
                template_id or "(none)",
                residual[:12],
            )

        session_state["draft_status"] = "ready"
        session_state["current_draft_version"] = new_version
        session_state["last_draft_markdown"] = markdown
        session_state["last_draft_document"] = draft_document
        session_state["last_assistant_message"] = markdown
        session_state["open_questions"] = []
        # Transient flag — true ONLY on the turn that just produced a draft
        # body, so BE can distinguish draft delivery from acknowledgements /
        # follow-up question turns. Consumed (popped) in handle_turn.
        session_state["_draft_just_delivered"] = True
        return session_state

    async def _handle_finalize_export(
        self,
        *,
        case_context: Dict[str, Any],
        session_state: Dict[str, Any],
        request_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Re-export the already-approved draft as a fresh DOCX (no AI call).

        Triggered when the lawyer types "Generate Final Draft", "finalize
        the draft", "export draft", etc. AFTER a draft has been rendered
        and is sitting at ``draft_status == "ready"``. We re-run the DOCX
        renderer on the cached ``last_draft_markdown``, upload the result
        to GCS, and surface a new signed URL — content is unchanged, only
        the file is regenerated. No Gemini round-trip, no follow-up menu.
        """
        markdown = (session_state.get("last_draft_markdown") or "").strip()
        if not markdown:
            session_state["last_assistant_message"] = (
                "I do not yet have a draft to finalise. Tell me which "
                "document you would like me to draft and I will get started."
            )
            return session_state

        draft_title = session_state.get("draft_title") or "Final Draft"
        template_id: Optional[str] = session_state.get("template_id")
        template_name: Optional[str] = session_state.get("template_name")
        user_id = (
            (request_fields or {}).get("userId")
            or session_state.get("user_id")
            or (case_context or {}).get("user_id")
            or ""
        )
        workspace_id = (
            (request_fields or {}).get("workspaceId")
            or session_state.get("workspace_id")
            or ""
        )
        actionable_id = (
            (request_fields or {}).get("actionableId")
            or session_state.get("actionable_id")
            or ""
        )

        tpl: Optional[Dict[str, Any]] = None
        if template_id and user_id:
            try:
                tpl = await self.get_template(
                    user_id=user_id, template_id=template_id
                )
            except Exception as exc:
                logger.warning(
                    "Finalize: could not fetch template {} for user {}: {}",
                    template_id,
                    user_id,
                    exc,
                )
                tpl = None
            if tpl:
                template_name = template_name or tpl.get("name")

        prev_version = int(session_state.get("current_draft_version") or 0)
        new_version = prev_version + 1

        download_url: Optional[str] = await self._export_draft_docx(
            markdown=markdown,
            draft_title=draft_title,
            tpl=tpl,
            user_id=user_id,
            template_id=template_id,
            workspace_id=workspace_id,
            actionable_id=actionable_id,
            version=new_version,
            role=(case_context.get("role") or "ADJUDICATOR").upper(),
            case_summary=case_context.get("case_summary") or "",
            case_facts_brief=case_context.get("case_facts_brief") or "",
            gathered_facts=session_state.get("gathered_facts") or {},
        )

        source_value = session_state.get("source") or (
            "template" if template_id else "blank"
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
        session_state["draft_status"] = "ready"
        session_state["current_draft_version"] = new_version
        session_state["last_draft_document"] = draft_document
        session_state["last_assistant_message"] = markdown
        session_state["open_questions"] = []
        session_state["_draft_just_delivered"] = True
        # Any pending menu is moot now — the lawyer chose the finalise
        # path, not one of the offered alternatives.
        session_state.pop("last_offered_options", None)
        return session_state

    # ------------------------------------------------------------------ utils

    # Missing-Information Matrix priorities (engine analysis phase). Anything
    # the model doesn't classify defaults to "Important" — askable but not a
    # hard blocker.
    _QUESTION_CLASSES = {"Critical", "Important", "Optional"}

    @staticmethod
    def _is_filled_value(v: Any) -> bool:
        """True iff a gathered value is a real, usable answer (not blank / a
        placeholder / a 'to be determined' stub)."""
        if v is None:
            return False
        s = str(v).strip()
        if not s:
            return False
        low = s.lower()
        if low in ("tbd", "to be determined", "n/a", "na", "none", "null", "-", "—"):
            return False
        if s.startswith("<") and s.endswith(">"):
            return False
        return True

    def _ensure_registry_coverage(
        self,
        interactive_variables: Any,
        gathered: Dict[str, Any],
        open_qs: List[Dict[str, str]],
    ) -> "tuple[List[Dict[str, str]], List[Dict[str, str]]]":
        """Deterministic guarantee that EVERY registry variable is either
        auto-filled (a real value in `gathered`) or surfaced as an open
        question. For any variable addressed by neither, append a question
        built from the variable's stored `question` / `label`.

        Returns ``(open_qs_complete, added)`` where ``added`` are the
        coverage-injected questions (empty when the kickoff already covered
        everything). Pure / deterministic — no LLM call."""
        variables = [v for v in (interactive_variables or []) if isinstance(v, dict)]
        if not variables:
            return open_qs, []

        def norm(s: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")

        addressed: set = set()
        for k, val in (gathered or {}).items():
            if self._is_filled_value(val):
                addressed.add(norm(k))
        existing_ids: set = set()
        for q in open_qs or []:
            if not isinstance(q, dict):
                continue
            qid = norm(q.get("id"))
            addressed.add(qid)
            existing_ids.add(qid)
            head = (q.get("label") or "").split(":", 1)[0]
            if head:
                addressed.add(norm(head))

        out = list(open_qs or [])
        added: List[Dict[str, str]] = []
        for var in variables:
            vid = norm(var.get("id"))
            vlabel = norm(var.get("label"))
            if not (vid or vlabel):
                continue
            if (vid and vid in addressed) or (vlabel and vlabel in addressed):
                continue
            label = (var.get("label") or var.get("id") or "Detail").strip()
            question = (var.get("question") or f"What is the {label}?").strip()
            qid = (var.get("id") or "").strip() or norm(label) or f"var_{len(out) + 1}"
            if norm(qid) in existing_ids:
                continue
            classification = "Critical" if var.get("required", True) else "Important"
            entry = {
                "id": qid,
                "label": f"{label}: {question}",
                "classification": classification,
            }
            out.append(entry)
            added.append(entry)
            existing_ids.add(norm(qid))
            addressed.add(norm(qid))
            if vlabel:
                addressed.add(vlabel)

        return out[: self._MAX_INTERACTIVE_VARIABLES], added

    def _normalize_open_questions(
        self, raw: Any, limit: int = 10
    ) -> List[Dict[str, str]]:
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
            # Carry the Missing-Information Matrix priority through (optional,
            # additive — frontend may ignore it). Default to "Important".
            classification = (item.get("classification") or "").strip().title()
            if classification not in self._QUESTION_CLASSES:
                classification = "Important"
            out.append(
                {"id": qid, "label": label, "classification": classification}
            )
        return out[: max(1, limit)]

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

    # Tight affirmation / negation sets used only when interpreting a
    # short reply against a previously-offered numbered menu. Narrower
    # than `_ACK_PHRASES` on purpose — we want only unambiguous yes/no
    # surface forms here, not "thanks" / "looks good" / etc.
    _AFFIRM_PHRASES = frozenset(
        {
            "yes", "yeah", "yep", "yup", "y",
            "ok", "okay", "k", "kk", "sure",
            "yes please", "yes pls", "ok please",
            "alright", "all right", "fine",
            "go ahead", "proceed", "do it", "do that",
        }
    )
    _NEGATE_PHRASES = frozenset(
        {
            "no", "nope", "nah", "n", "naa",
            "no thanks", "no thank you", "not really",
            "i dont want", "i don't want",
            "no i dont want", "no i don't want",
            "no i dont want it", "no i don't want it",
            "not interested", "skip", "skip it",
        }
    )

    @classmethod
    def _is_pure_affirmation(cls, text: str) -> bool:
        """Strict: True only for unambiguous bare-affirmation messages."""
        if not text:
            return False
        normalised = cls._normalize_ack_text(text)
        return bool(normalised) and normalised in cls._AFFIRM_PHRASES

    @classmethod
    def _is_pure_negation(cls, text: str) -> bool:
        """Strict: True only for unambiguous bare-negation messages."""
        if not text:
            return False
        normalised = cls._normalize_ack_text(text)
        return bool(normalised) and normalised in cls._NEGATE_PHRASES

    # ``Generate Final Draft`` / ``finalize the draft`` and friends — these
    # signal "produce a downloadable file from the draft I already
    # approved", NOT "regenerate the content". Routed to a no-AI export
    # path that re-renders the cached markdown into a fresh DOCX.
    _FINALIZE_NEEDLES = (
        "generate final draft",
        "generate the final draft",
        "generate final",
        "final draft",
        "finalize draft",
        "finalize the draft",
        "finalise draft",
        "finalise the draft",
        "finalize this",
        "finalise this",
        "give me the final",
        "give me final",
        "the final version",
        "export draft",
        "export the draft",
        "download draft",
        "download the draft",
        "produce final",
        "produce the final",
        "i want the final",
    )

    @classmethod
    def _is_finalize_request(cls, text: str) -> bool:
        """True when the user is asking to export the already-approved draft."""
        if not text:
            return False
        normalised = cls._normalize_ack_text(text)
        if not normalised:
            return False
        if len(normalised) > 80:
            return False
        return any(needle in normalised for needle in cls._FINALIZE_NEEDLES)

    # Regex used to extract a numbered menu from the assistant's reply.
    # Matches lines that look like ``1. **Title** — description`` or
    # ``2. **Title**``. The capture group is the bolded title only.
    _OFFERED_OPTION_RE = re.compile(
        r"^\s*\d+\.\s*\*\*([^*\n]+?)\*\*",
        flags=re.MULTILINE,
    )

    @classmethod
    def _extract_offered_options(cls, message: str) -> List[str]:
        """Pull bolded option titles from a numbered list in the AI's reply.

        Used to populate ``session_state['last_offered_options']`` so the
        next turn can interpret short replies (``yes`` / ``no`` / a bare
        document name / a number) against the menu the assistant just
        offered. Returns at most 6 unique titles in order; ``[]`` when
        the message does not contain a recognised menu.
        """
        if not message:
            return []
        options: List[str] = []
        seen: set = set()
        for match in cls._OFFERED_OPTION_RE.finditer(message):
            title = (match.group(1) or "").strip()
            # Strip trailing punctuation that sometimes leaks in
            # (``Title:`` / ``Title —`` etc.).
            title = title.rstrip(":—–-").strip()
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            options.append(title)
            if len(options) >= 6:
                break
        return options

    # Numeric / ordinal aliases for picking an offered option by index.
    _OPTION_NUMERIC_ALIASES = {
        "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5,
        "first": 0, "the first": 0, "first one": 0, "the first one": 0,
        "second": 1, "the second": 1, "second one": 1, "the second one": 1,
        "third": 2, "the third": 2, "third one": 2, "the third one": 2,
        "fourth": 3, "fifth": 4, "sixth": 5,
        "option 1": 0, "option 2": 1, "option 3": 2,
        "option 4": 3, "option 5": 4, "option 6": 5,
    }

    @classmethod
    def _match_offered_option(
        cls,
        user_message: str,
        offered_options: List[str],
    ) -> Optional[str]:
        """Match a short user reply against the previously-offered options.

        Returns the matched option title or ``None``. Matching priority:
          1. Numeric / ordinal alias (``"1"`` / ``"first"`` / ``"option 2"``).
          2. Exact (case-insensitive) match of the option title.
          3. Substring containment in either direction (e.g. user types
             ``"Supporting Affidavit"`` and the option title is
             ``"Supporting Affidavit"`` — works; also matches when the
             user prefixes with ``"I want ..."`` or ``"draft ..."``).
        """
        if not user_message or not offered_options:
            return None
        msg_norm = cls._normalize_ack_text(user_message)
        if not msg_norm:
            return None

        # 1. Numeric / ordinal alias.
        idx = cls._OPTION_NUMERIC_ALIASES.get(msg_norm)
        if idx is not None and 0 <= idx < len(offered_options):
            return offered_options[idx]

        # 2 & 3. Title match (exact or substring).
        for option in offered_options:
            opt_norm = cls._normalize_ack_text(option)
            if not opt_norm:
                continue
            if msg_norm == opt_norm:
                return option
            # Substring: works for "i want criminal complaint" → "criminal complaint".
            if opt_norm in msg_norm or msg_norm in opt_norm:
                # Require the substring to be a non-trivial fraction of one
                # of the strings, so a 2-char user message can't snag a
                # long option title by accident.
                if len(opt_norm) >= 4 and len(msg_norm) >= 4:
                    return option
        return None

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

        questions_block = self._questions_block(open_questions)
        return (
            f"I have initiated the drafting of {draft_title} based on the "
            f"present case context.\n\n"
            f"**Information Required to Complete the Draft:**\n"
            f"To finalise the draft, please provide the following details so "
            f"the document is complete and legally accurate:\n\n"
            f"{questions_block}"
        )

    def _questions_block(self, open_questions: List[Dict[str, Any]]) -> str:
        """Render open questions as ONE grouped, continuously-numbered list
        (Critical → Important → Optional), mirroring the layout enforced by
        TEMPLATE_KICKOFF_INSTRUCTION. Each label is "Title: question"; the
        title is bolded inline. Shared by the fallback composer and the
        full-question merge so every surface shows the same complete list."""
        def _fmt(n: int, q: Dict[str, Any]) -> str:
            label = (q.get("label") or "").strip()
            if ":" in label:
                title_part, question_part = label.split(":", 1)
                return f"{n}. **{title_part.strip()}:** {question_part.strip()}"
            return f"{n}. {label}"

        group_labels = [
            ("Critical", "**Critical (required to finalise the draft):**"),
            ("Important", "**Important (would materially strengthen the draft):**"),
            ("Optional", "**Optional (useful but not essential):**"),
        ]
        blocks: List[str] = []
        counter = 1
        for key, header in group_labels:
            items = [
                q for q in open_questions
                if (q.get("classification") or "Important").title() == key
            ]
            if not items:
                continue
            lines = [header]
            for q in items:
                lines.append(_fmt(counter, q))
                counter += 1
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _merge_full_questions_into_message(
        self,
        message: str,
        open_questions: List[Dict[str, Any]],
        draft_title: str,
    ) -> str:
        """Splice the COMPLETE grouped question list (from open_questions, the
        source of truth) into the model's kickoff message, so the chat shows
        every unfilled variable in ONE numbered Critical/Important/Optional list
        — never a separate round or a trailing run-on addendum.

        Preserves the model's DOCUMENT UNDERSTANDING + Key Case Details head and
        its Strategic Observations tail; replaces only the question list in
        between. Falls back to the deterministic composer when the message is
        empty or lacks the expected section header."""
        if not open_questions:
            return message
        if not (message or "").strip():
            return self._compose_questions_message(open_questions, draft_title)

        info = "**Information Required to Complete the Draft:**"
        strat = "**Strategic Observations:**"
        block = self._questions_block(open_questions)
        intro_default = (
            "To finalise the draft, please provide the following details so "
            "the document is complete and legally accurate:"
        )

        if info not in message:
            # Model didn't emit the section header — insert a clean one before
            # any Strategic Observations, else append at the end.
            if strat in message:
                head, strat_part = message.split(strat, 1)
                return (
                    f"{head.rstrip()}\n\n{info}\n{intro_default}\n\n{block}\n\n"
                    f"{strat}{strat_part}"
                ).strip()
            return f"{message.rstrip()}\n\n{info}\n{intro_default}\n\n{block}"

        head, rest = message.split(info, 1)
        strat_tail = ""
        if strat in rest:
            body, strat_part = rest.split(strat, 1)
            strat_tail = f"{strat}{strat_part}"
        else:
            body = rest
        # Keep the model's intro paragraph (what is preserved / stripped), which
        # sits between the header and the first priority sub-label.
        intro = body
        for sub in ("**Critical", "**Important", "**Optional"):
            if sub in intro:
                intro = intro.split(sub, 1)[0]
                break
        intro = intro.strip() or intro_default

        out = f"{head.rstrip()}\n\n{info}\n{intro}\n\n{block}"
        if strat_tail.strip():
            out = f"{out}\n\n{strat_tail.strip()}"
        return out.strip()

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
