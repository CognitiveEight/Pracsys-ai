"""Shared JSON-parsing helpers used across features.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Set

from loguru import logger


from app.socket.event_utils import (
    _mint_item_id,
    _normalize_match_text,
    drafting_payload_to_wire,
)
from app.core.roles import DEFAULT_WORKSPACE_ROLE


class JsonHelpersMixin:
    """Shared JSON-parsing helpers used across features."""

    def _strip_markdown_json_fences(self, text: str) -> str:
        cleaned = (text or "").strip()
        if "```" not in cleaned:
            return cleaned
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, flags=re.I)
        if m:
            return (m.group(1) or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        return cleaned.strip()

    def _extract_balanced_json_fragment(
        self, text: str, open_char: str
    ) -> Optional[str]:
        if not text:
            return None
        close_char = "}" if open_char == "{" else "]"
        start = text.find(open_char)
        if start < 0:
            return None
        depth = 0
        i = start
        in_string = False
        escape = False
        quote = '"'
        while i < len(text):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == quote:
                    in_string = False
                i += 1
                continue
            if c == '"' or c == "'":
                in_string = True
                quote = c
                i += 1
                continue
            if c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        return text[start:].strip() if start >= 0 else None

    # ------------------------------------------------------------------
    # Background task bookkeeping
    # ------------------------------------------------------------------

    def _track_background_task(
        self, task: "asyncio.Task", *, label: str = ""
    ) -> "asyncio.Task":
        """Hold a strong reference to a detached task and drain its exception.

        Two problems this solves. asyncio keeps only a WEAK reference to a task,
        so a bare ``create_task(...)`` whose handle is discarded can be
        garbage-collected mid-flight. And a task that dies with an exception
        nobody retrieves reports it, at best, as an unhelpful
        "Task exception was never retrieved" at interpreter shutdown — which is
        how a whole feature cascade can disappear leaving only a stuck loader.

        Also ties the task to the workspace run that spawned it, so a
        ``workspace_stop_request`` can cancel it (see handlers/run_control.py).
        """
        self._background_tasks.add(task)
        try:
            self._adopt_background_task(task)
        except Exception as adopt_err:  # pragma: no cover - bookkeeping only
            logger.debug("Could not tie background task to its run: {}", adopt_err)

        def _done(finished: "asyncio.Task") -> None:
            self._background_tasks.discard(finished)
            try:
                if finished.cancelled():
                    logger.warning(
                        "Background task cancelled{}",
                        f" ({label})" if label else "",
                    )
                    return
                exc = finished.exception()
            except Exception:  # pragma: no cover - defensive
                return
            if exc is not None:
                logger.opt(exception=exc).error(
                    "Background task failed{}: {}",
                    f" ({label})" if label else "",
                    exc,
                )

        task.add_done_callback(_done)
        return task

    # ------------------------------------------------------------------
    # Knowledge-graph evidence for panels (augment mode)
    # ------------------------------------------------------------------

    async def _graph_evidence_block(self, panel: str, workspace_id: str) -> str:
        """Ranked graph + hybrid evidence to ADD to a panel's prompt context.

        Empty unless the graph is connected and ``panel`` is in
        ``RETRIEVAL_PANELS`` — the existing context is never removed (augment
        mode). Never raises: retrieval improves a panel, it never gates it.
        """
        graph_store = getattr(self, "_graph_store", None)
        if graph_store is None or not workspace_id:
            return ""
        try:
            from app.retrieval.panel_bridge import is_enabled, retrieve_for_panel

            if not is_enabled(panel):
                return ""
            evidence = await retrieve_for_panel(panel, workspace_id, graph_store=graph_store)
            return evidence.prompt_block
        except Exception as graph_err:
            logger.warning("Graph evidence for {} skipped: {}", panel, graph_err)
            return ""

    # ------------------------------------------------------------------
    # Undelivered feature payloads
    #
    # SocketClient.emit returns False instead of raising when the socket is down,
    # so a payload generated during a disconnect is discarded with nothing to
    # retry it. Since a panel loader is cleared only by the arrival of that
    # payload, the panel then spins for the rest of the session. These keep the
    # envelope (persisted, because the disconnect often coincides with a
    # restart) so the REAL generated output is delivered on reconnect rather
    # than a stale cached snapshot.
    # ------------------------------------------------------------------

    def _note_pending_feature_delivery(
        self, workspace_id: str, feature_type: str, envelope: Dict[str, Any]
    ) -> None:
        """Buffer an undelivered ``document_feature_response`` envelope."""
        if not workspace_id or not feature_type or not isinstance(envelope, dict):
            return
        if self._is_stale_run(workspace_id):
            # The run was stopped: its output was fenced, not lost. Buffering it
            # would re-deliver a stopped panel on the next reconnect or restart.
            logger.info(
                "Not buffering {} for workspace {} — its run was stopped",
                feature_type,
                str(workspace_id)[:12],
            )
            return
        try:
            existing = self._pending_feature_deliveries.get(workspace_id) or {}
            if not isinstance(existing, dict):
                existing = {}
            # Reassign rather than mutate: the cache is a write-through
            # PersistentWorkspaceDict and only __setitem__ persists.
            self._pending_feature_deliveries[workspace_id] = {
                **existing,
                feature_type: envelope,
            }
        except Exception as note_err:
            logger.warning(
                "Could not buffer undelivered {} for workspace {}: {}",
                feature_type,
                str(workspace_id)[:12],
                note_err,
            )

    def _clear_pending_feature_delivery(
        self, workspace_id: str, feature_type: str
    ) -> None:
        """Drop a buffered envelope once a fresher payload was delivered."""
        if not workspace_id or not feature_type:
            return
        try:
            existing = self._pending_feature_deliveries.get(workspace_id)
            if not isinstance(existing, dict) or feature_type not in existing:
                return
            remaining = {
                k: v for k, v in existing.items() if k != feature_type
            }
            self._pending_feature_deliveries[workspace_id] = remaining
        except Exception:
            # Never let bookkeeping break a successful emit.
            pass

    async def _flush_pending_feature_deliveries(
        self, workspace_id: str = ""
    ) -> Dict[str, List[str]]:
        """Re-send every buffered feature envelope.

        Returns ``{workspace_id: [featureType, ...]}`` for what was delivered —
        the caller needs to know WHICH features landed, because a delivered
        strategy_objective means that regeneration actually completed and only
        its delivery failed, so it must not also be re-run.

        Runs BEFORE the cached-feature reconnect replay so the frontend gets the
        genuinely-generated payload (e.g. the new strategic objective) rather
        than the pre-change snapshot the replay would ship.
        """
        if not self._socket_client:
            return {}
        workspace_ids = (
            [workspace_id]
            if workspace_id
            else [k for k in list(self._pending_feature_deliveries.keys()) if k]
        )
        delivered: Dict[str, List[str]] = {}
        for ws_id in workspace_ids:
            buffered = self._pending_feature_deliveries.get(ws_id)
            if not isinstance(buffered, dict) or not buffered:
                continue
            remaining: Dict[str, Any] = {}
            for feature_type, envelope in list(buffered.items()):
                if not isinstance(envelope, dict):
                    continue
                try:
                    ok = await self._socket_client.emit(
                        "document_feature_response", envelope
                    )
                except Exception as emit_err:
                    logger.warning(
                        "Pending {} re-delivery raised for workspace {}: {}",
                        feature_type,
                        str(ws_id)[:12],
                        emit_err,
                    )
                    ok = False
                if ok:
                    delivered.setdefault(ws_id, []).append(feature_type)
                    logger.success(
                        "Pending {} re-delivered after reconnect (workspace={})",
                        feature_type,
                        str(ws_id)[:12],
                    )
                else:
                    # Still down — keep it for the next reconnect.
                    remaining[feature_type] = envelope
            self._pending_feature_deliveries[ws_id] = remaining
        return delivered

    # ------------------------------------------------------------------
    # Loader release
    #
    # A frontend panel loader is armed by ``workspace:refresh`` (which names the
    # featureTypes to spin) and disarmed ONLY by the arrival of a
    # ``document_feature_response`` carrying the same featureType. There is no
    # "refresh finished" event and no client-side timeout, so any path that arms
    # a loader and then returns without emitting leaves that panel spinning for
    # the rest of the session.
    #
    # This is the last-resort pairing for every such path: it emits the panel's
    # terminal state (its preserved cached payload, or the feature's own minimal
    # fallback) for each featureType that produced no emit. It is NOT a way to
    # paper over a failure — callers must still report the failure to the user —
    # it only guarantees the spinner cannot outlive the work that armed it.
    # ------------------------------------------------------------------

    async def _release_refresh_loaders(
        self,
        workspace_id: str,
        user_id: str,
        feature_types: List[str],
        *,
        skip: Optional[Set[str]] = None,
        reason: str = "",
        workspace_role: str = "",
    ) -> List[str]:
        """Emit a terminal state for every armed loader that never got one.

        ``skip`` names the featureTypes that DID emit during the refresh window.
        Returns the featureTypes actually released. Each feature is released
        independently so one failure cannot strand the rest.
        """
        if not self._socket_client or not workspace_id or not feature_types:
            return []
        already = {str(f) for f in (skip or set())}
        released: List[str] = []
        role = (workspace_role or DEFAULT_WORKSPACE_ROLE).strip().upper() or DEFAULT_WORKSPACE_ROLE

        for feature in feature_types:
            if not feature or feature in already:
                continue
            try:
                if feature == "strategy_objective":
                    cached = self._workspace_strategy_payload.get(workspace_id)
                    payload = (
                        cached
                        if isinstance(cached, dict) and cached
                        else self._strategy_objective_fallback(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=role,
                            is_update=True,
                        )
                    )
                elif feature == "analysis_report":
                    cached = self._workspace_analysis_payload.get(workspace_id)
                    payload = (
                        cached
                        if isinstance(cached, dict) and cached
                        else self._analysis_report_fallback(role)
                    )
                elif feature == "list_of_dates":
                    cached_dates = self._workspace_list_of_dates_payload.get(
                        workspace_id
                    )
                    payload = {
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "listOfDates": (
                            cached_dates if isinstance(cached_dates, list) else []
                        ),
                    }
                elif feature == "applicable_laws":
                    cached_laws = self._workspace_applicable_laws_payload.get(
                        workspace_id
                    )
                    payload = cached_laws if isinstance(cached_laws, dict) else {}
                elif feature == "procedural_timeline":
                    cached_timeline = (
                        self._workspace_procedural_timeline_payload.get(workspace_id)
                    )
                    payload = {
                        "proceduralTimeline": (
                            cached_timeline
                            if isinstance(cached_timeline, list)
                            else []
                        )
                    }
                elif feature == "relevant_parties":
                    # Delegate: the wire shape for these three lives in one place
                    # each, and duplicating it here would drift.
                    await self._emit_relevant_parties(workspace_id, user_id)
                    released.append(feature)
                    continue
                elif feature == "case_laws":
                    await self._reemit_case_laws_unchanged(workspace_id, user_id)
                    released.append(feature)
                    continue
                elif feature in ("document_assessments", "workspace_metadata"):
                    # Declared by a resume that stopped before producing them.
                    # The cache holds the whole envelope; its inner payload is
                    # the terminal state.
                    cache = (
                        self._workspace_document_assessments_payload
                        if feature == "document_assessments"
                        else self._workspace_metadata_payload
                    )
                    cached_env = cache.get(workspace_id)
                    inner = (
                        cached_env.get("payload")
                        if isinstance(cached_env, dict)
                        else None
                    )
                    if not (isinstance(inner, dict) and inner):
                        # No invented fallback: an empty manifest or a "General"
                        # category would overwrite the backend's real state,
                        # which is worse than one panel left lit.
                        logger.warning(
                            "Loader release: no cached {} to settle with "
                            "(workspace={}) — panel may stay lit",
                            feature,
                            workspace_id[:12] if workspace_id else "",
                        )
                        continue
                    payload = inner
                elif feature == "drafting_assistant":
                    # NOT delegated to _reemit_drafting_unchanged: that helper
                    # returns early when there is no cached payload, which is
                    # exactly the case where the loader most needs releasing.
                    cached_drafts = self._workspace_drafting_payload.get(workspace_id)
                    payload = (
                        drafting_payload_to_wire(cached_drafts)
                        if isinstance(cached_drafts, dict) and cached_drafts
                        else {"suggestedDrafts": []}
                    )
                else:
                    logger.warning(
                        "Loader release: no terminal state known for featureType {} "
                        "(workspace={}) — panel may stay lit",
                        feature,
                        workspace_id[:12] if workspace_id else "",
                    )
                    continue

                await self._emit_document_feature(
                    feature_type=feature,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=payload,
                    usage=None,
                )
                released.append(feature)
            except Exception as release_err:
                logger.warning(
                    "Loader release failed for {} (workspace={}): {}",
                    feature,
                    workspace_id[:12] if workspace_id else "",
                    release_err,
                )

        if released:
            logger.info(
                "Loader release ({}): re-emitted terminal state for {} (workspace={})",
                reason or "unspecified",
                released,
                workspace_id[:12] if workspace_id else "",
            )
        return released

    def _prepare_json_candidate(
        self, raw_text: str, prefer: str = "object"
    ) -> Optional[str]:
        text = self._strip_markdown_json_fences(raw_text)
        if not text:
            return None

        open_char = "{" if prefer == "object" else "["
        candidate = self._extract_balanced_json_fragment(text, open_char)
        if not candidate:
            fallback_char = "[" if open_char == "{" else "{"
            candidate = self._extract_balanced_json_fragment(text, fallback_char)
        if not candidate:
            return None

        # Repair common truncation by balancing quotes/brackets/braces.
        unescaped_quotes = len(re.findall(r'(?<!\\)"', candidate))
        if unescaped_quotes % 2 != 0:
            candidate += '"'
        open_brackets = candidate.count("[") - candidate.count("]")
        open_braces = candidate.count("{") - candidate.count("}")
        if open_brackets > 0:
            candidate += "]" * open_brackets
        if open_braces > 0:
            candidate += "}" * open_braces
        candidate = re.sub(r",\s*([\]}])", r"\1", candidate)
        return candidate.strip()

    # ------------------------------------------------------------------
    # currentState reconciliation
    #
    # The frontend echoes the previously-emitted, user-curated panel state
    # back on workspace_sync (see schemas.WorkspaceCurrentState). When a panel
    # regenerates, each fresh item is tagged so the frontend knows what carried
    # over vs what is new:
    #   - a fresh item whose CONTENT matches a previous item -> isMatched=True,
    #     and it CARRIES the previous item's id;
    #   - a brand-new fresh item -> isMatched=False, server mints a new id;
    #   - previous-only items the fresh run did not regenerate are DROPPED.
    # Matching is by text/content only; the curation ``status`` is ignored.
    # ------------------------------------------------------------------

    async def _match_items_to_previous(
        self,
        fresh_texts: List[str],
        prev_pool: List[Dict[str, Any]],
        *,
        item_type: str,
        enable_llm: bool = True,
    ) -> Dict[int, str]:
        """Map fresh item indexes to a previous item's id, by meaning.

        ``fresh_texts`` — the match text for each fresh item (index == position).
        ``prev_pool``   — previous items, each ``{"id": str, "text": str}``.

        Returns ``{fresh_index: prev_id}`` for the fresh items that matched a
        previous one (each previous id used at most once). A deterministic
        normalized-text pass runs first; any still-unmatched fresh items go
        through one fail-open Gemini semantic pass when ``enable_llm``.
        """
        matches: Dict[int, str] = {}
        if not fresh_texts or not prev_pool:
            return matches

        consumed: Set[str] = set()  # prev ids already claimed
        # 1) Deterministic normalized-text pass.
        prev_norm = [
            (str(p.get("id") or ""), _normalize_match_text(p.get("text")))
            for p in prev_pool
        ]
        for fi, ftext in enumerate(fresh_texts):
            fkey = _normalize_match_text(ftext)
            if not fkey:
                continue
            for pid, pkey in prev_norm:
                if not pid or pid in consumed or not pkey:
                    continue
                if pkey == fkey:
                    matches[fi] = pid
                    consumed.add(pid)
                    break

        # 2) Fail-open LLM semantic pass over the leftovers.
        remaining_fresh = [
            (fi, ftext)
            for fi, ftext in enumerate(fresh_texts)
            if fi not in matches and str(ftext or "").strip()
        ]
        remaining_prev = [
            p for p in prev_pool if str(p.get("id") or "") not in consumed
        ]
        if not enable_llm or not remaining_fresh or not remaining_prev:
            return matches

        try:
            from app.ai.gemini_client import gemini_client
            from app.prompts.objective_change_prompts import (
                llm_match_fresh_against_previous_prompt,
            )

            numbered_fresh = "\n".join(
                f"{local + 1}. {ftext}" for local, (_fi, ftext) in enumerate(remaining_fresh)
            )
            numbered_prev = "\n".join(
                f"{i + 1}. [{p.get('id')}] {p.get('text')}"
                for i, p in enumerate(remaining_prev)
            )
            prompt = llm_match_fresh_against_previous_prompt(
                item_type=item_type,
                numbered_fresh=numbered_fresh,
                numbered_prev=numbered_prev,
            )
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=4000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            pairs = parsed.get("matches") if isinstance(parsed, dict) else None
            valid_prev_ids = {str(p.get("id") or "") for p in remaining_prev}
            for pair in pairs or []:
                try:
                    local = int(pair.get("freshIndex")) - 1
                    pid = str(pair.get("prevId") or "")
                except (TypeError, ValueError):
                    continue
                if local < 0 or local >= len(remaining_fresh):
                    continue
                if not pid or pid in consumed or pid not in valid_prev_ids:
                    continue
                fi = remaining_fresh[local][0]
                if fi in matches:
                    continue
                matches[fi] = pid
                consumed.add(pid)
        except Exception as e:
            logger.debug(
                "currentState semantic match ({}) failed; using deterministic only: {}",
                item_type, e,
            )
        return matches

    async def _reconcile_against_current_state(
        self,
        *,
        fresh: List[str],
        prev_items: Optional[List[Dict[str, Any]]],
        section: str,
        text_key: str = "text",
        enable_llm: bool = True,
    ) -> List[Dict[str, Any]]:
        """Tag a fresh text list against previously-curated items.

        Returns ``[{id, text, isMatched}]`` in fresh order. Matched items carry
        the previous id (``isMatched=True``); new items get a freshly minted id
        (``isMatched=False``). Previous-only items are dropped. When
        ``prev_items`` is empty/None (e.g. first workspace_created, or no
        currentState) every item is emitted new with a minted id — so the shape
        is uniform from the very first emit.
        """
        fresh_clean = [
            s.strip() for s in (fresh or []) if isinstance(s, str) and s.strip()
        ]
        prev_pool: List[Dict[str, Any]] = []
        for p in prev_items or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            ptext = p.get(text_key) or p.get("text") or ""
            if pid and str(ptext).strip():
                prev_pool.append({"id": pid, "text": str(ptext).strip()})

        matches = await self._match_items_to_previous(
            fresh_clean, prev_pool, item_type=section, enable_llm=enable_llm
        )
        out: List[Dict[str, Any]] = []
        for fi, text in enumerate(fresh_clean):
            prev_id = matches.get(fi)
            if prev_id:
                out.append({"id": prev_id, "text": text, "isMatched": True})
            else:
                out.append(
                    {"id": _mint_item_id(section), "text": text, "isMatched": False}
                )
        return out
