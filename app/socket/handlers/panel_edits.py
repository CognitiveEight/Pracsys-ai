"""User add/edit/delete of case-law and relevant-party panel items.

Feature mixin extracted from events.py — methods moved verbatim.
"""

from typing import Any, Dict

from loguru import logger

from app.ai.usage import current_usage, meter_usage
from app.search.case_law_finder import (
    find_case_law_sources,
)
from app.core.roles import DEFAULT_WORKSPACE_ROLE



class PanelEditsMixin:
    """User add/edit/delete of case-law and relevant-party panel items."""

    async def _reemit_case_laws_unchanged(self, workspace_id: str, user_id: str) -> None:
        """Re-emit the cached case_laws payload (clears a FE loader) without changes."""
        if not self._socket_client:
            return
        cached = self._workspace_case_laws_payload.get(workspace_id) or {"caseLaws": []}
        await self._emit_document_feature(
            feature_type="case_laws",
            workspace_id=workspace_id,
            user_id=user_id,
            payload=cached,
            usage=None,
        )

    @meter_usage
    async def on_add_case_law(self, data: Dict[str, Any]) -> None:
        """
        Handle add_case_law: user manually adds a precedent via the "Add Case
        Law" button, supplying ANY one of citation / url / free-text.

        Flow (strict source-verified):
        - Resolve real source row(s): a supplied URL is used directly; a
          citation/description is run through ``find_case_law_sources``.
        - If nothing resolves to a real source URL, the cached panel is
          re-emitted unchanged (clears the FE loader) and we stop — we never
          add an unverifiable case.
        - Otherwise the resolved source(s) go through the same LLM
          select/rank/verify pass as auto-generation, the result is merged into
          the cached panel (deduped by URL), and case_laws is re-emitted.
        """
        payload = data if isinstance(data, dict) else {}
        workspace_id = (
            payload.get("workspaceId") or payload.get("workspace_id") or ""
        ).strip()
        user_id = (payload.get("userId") or payload.get("user_id") or "").strip()
        workspace_role = (
            payload.get("workspaceRole") or payload.get("workspace_role") or ""
        )
        citation = (payload.get("citation") or "").strip()
        url = (payload.get("url") or "").strip()
        text = (payload.get("text") or "").strip()

        if not workspace_id:
            logger.warning("add_case_law: missing workspaceId – ignoring")
            return
        if not (citation or url or text):
            logger.warning(
                "add_case_law: no citation/url/text – ignoring (workspace={})",
                workspace_id,
            )
            return

        try:
            await self._hydrate_workspace_caches(workspace_id, user_id=user_id)
        except Exception as e:  # pragma: no cover - best-effort
            logger.debug("add_case_law: hydration failed (workspace={}): {}", workspace_id, e)

        role = (workspace_role or DEFAULT_WORKSPACE_ROLE).strip().upper()

        # Resolve candidate source rows.
        research_notes = ""
        if url:
            source_name = "Indian Kanoon" if "indiankanoon.org" in url else "Web"
            sources = [
                {
                    "title": citation or text or url,
                    "url": url,
                    "snippet": text,
                    "sourceName": source_name,
                }
            ]
        else:
            sources, research_notes = await find_case_law_sources(
                citation or text, limit=5
            )

        if not sources:
            logger.info(
                "add_case_law: no real source resolved – re-emitting unchanged (workspace={})",
                workspace_id,
            )
            await self._reemit_case_laws_unchanged(workspace_id, user_id)
            return

        signals = await self._extract_case_signals_from_documents(workspace_id, user_id)
        generated = await self._generate_case_laws_from_sources(
            workspace_id=workspace_id,
            workspace_role=role,
            signals=signals,
            search_results=sources,
            research_notes=research_notes,
        )
        new_items = generated.get("caseLaws", [])
        if not new_items:
            logger.info(
                "add_case_law: source not verifiable/relevant – re-emitting unchanged (workspace={})",
                workspace_id,
            )
            await self._reemit_case_laws_unchanged(workspace_id, user_id)
            return

        merged = self._merge_case_laws_with_previous(
            workspace_id, {"caseLaws": new_items}
        )
        self._workspace_case_laws_payload[workspace_id] = merged
        if self._socket_client:
            await self._emit_document_feature(
                feature_type="case_laws",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=merged,
                usage=current_usage(),
            )
            logger.success(
                "add_case_law: appended {} case(s); panel now has {} (workspace={})",
                len(new_items),
                len(merged.get("caseLaws", [])),
                workspace_id,
            )

    async def _emit_relevant_parties(self, workspace_id: str, user_id: str) -> None:
        """Persist + emit the current relevant_parties payload for a workspace."""
        if not self._socket_client:
            return
        payload = self._workspace_relevant_parties_payload.get(workspace_id) or {
            "parties": []
        }
        await self._emit_document_feature(
            feature_type="relevant_parties",
            workspace_id=workspace_id,
            user_id=user_id,
            payload=payload,
            usage=None,
        )

    async def on_add_relevant_party(self, data: Dict[str, Any]) -> None:
        """Handle add_relevant_party: user manually adds a party to the panel."""
        payload = data if isinstance(data, dict) else {}
        workspace_id = (
            payload.get("workspaceId") or payload.get("workspace_id") or ""
        ).strip()
        user_id = (payload.get("userId") or payload.get("user_id") or "").strip()
        name = (payload.get("name") or "").strip()
        if not workspace_id or not name:
            logger.warning("add_relevant_party: missing workspaceId/name – ignoring")
            return
        await self._hydrate_workspace_caches(workspace_id, user_id=user_id)

        new_party = {
            "name": name,
            "role": self._normalize_party_role(payload.get("role")),
            "details": (payload.get("details") or payload.get("description") or "").strip(),
            "contactNumber": (payload.get("contactNumber") or "").strip(),
            "email": (payload.get("email") or "").strip(),
            "officialIdUrl": (payload.get("officialIdUrl") or "").strip(),
            # No document provenance — flag it so per-document purging on a
            # document delete never removes a manually-added party.
            "sourceDocIds": [],
            "userAdded": True,
        }
        current = self._workspace_relevant_parties_payload.get(workspace_id) or {
            "parties": []
        }
        parties = [p for p in (current.get("parties") or []) if isinstance(p, dict)]
        key = self._party_name_key(name)
        if any(
            self._party_names_match(key, self._party_name_key(p.get("name")))
            for p in parties
        ):
            logger.info("add_relevant_party: duplicate name – re-emitting unchanged")
            await self._emit_relevant_parties(workspace_id, user_id)
            return
        # A re-added party should no longer be suppressed by the deleted-set.
        deleted = [
            d
            for d in (self._workspace_deleted_parties.get(workspace_id) or [])
            if d != key
        ]
        self._workspace_deleted_parties[workspace_id] = deleted
        parties.append(new_party)
        for i, p in enumerate(parties):
            p["id"] = f"party_{i + 1}"
        self._workspace_relevant_parties_payload[workspace_id] = {"parties": parties}
        await self._emit_relevant_parties(workspace_id, user_id)
        logger.info("add_relevant_party: party added ({} total)", len(parties))

    async def on_edit_relevant_party(self, data: Dict[str, Any]) -> None:
        """Handle edit_relevant_party: update a party's fields by id."""
        payload = data if isinstance(data, dict) else {}
        workspace_id = (
            payload.get("workspaceId") or payload.get("workspace_id") or ""
        ).strip()
        user_id = (payload.get("userId") or payload.get("user_id") or "").strip()
        party_id = (
            payload.get("partyId") or payload.get("party_id") or payload.get("id") or ""
        ).strip()
        if not workspace_id or not party_id:
            logger.warning("edit_relevant_party: missing workspaceId/partyId – ignoring")
            return
        await self._hydrate_workspace_caches(workspace_id, user_id=user_id)

        current = self._workspace_relevant_parties_payload.get(workspace_id) or {
            "parties": []
        }
        parties = [p for p in (current.get("parties") or []) if isinstance(p, dict)]
        found = False
        for p in parties:
            if p.get("id") == party_id:
                if (payload.get("name") or "").strip():
                    p["name"] = payload["name"].strip()
                if payload.get("role") is not None:
                    p["role"] = self._normalize_party_role(payload.get("role"))
                if payload.get("details") is not None or payload.get("description") is not None:
                    p["details"] = (
                        payload.get("details")
                        if payload.get("details") is not None
                        else payload.get("description") or ""
                    ).strip()
                if payload.get("contactNumber") is not None:
                    p["contactNumber"] = (payload.get("contactNumber") or "").strip()
                if payload.get("email") is not None:
                    p["email"] = (payload.get("email") or "").strip()
                if payload.get("officialIdUrl") is not None:
                    p["officialIdUrl"] = (payload.get("officialIdUrl") or "").strip()
                found = True
                break
        if not found:
            logger.warning(
                "edit_relevant_party: id {} not found – re-emitting unchanged", party_id
            )
            await self._emit_relevant_parties(workspace_id, user_id)
            return
        self._workspace_relevant_parties_payload[workspace_id] = {"parties": parties}
        await self._emit_relevant_parties(workspace_id, user_id)
        logger.success("edit_relevant_party: {} updated", party_id)

    async def on_delete_relevant_party(self, data: Dict[str, Any]) -> None:
        """Handle delete_relevant_party: remove a party by id and remember it."""
        payload = data if isinstance(data, dict) else {}
        workspace_id = (
            payload.get("workspaceId") or payload.get("workspace_id") or ""
        ).strip()
        user_id = (payload.get("userId") or payload.get("user_id") or "").strip()
        party_id = (
            payload.get("partyId") or payload.get("party_id") or payload.get("id") or ""
        ).strip()
        if not workspace_id or not party_id:
            logger.warning(
                "delete_relevant_party: missing workspaceId/partyId – ignoring"
            )
            return
        await self._hydrate_workspace_caches(workspace_id, user_id=user_id)

        current = self._workspace_relevant_parties_payload.get(workspace_id) or {
            "parties": []
        }
        parties = [p for p in (current.get("parties") or []) if isinstance(p, dict)]
        removed = next((p for p in parties if p.get("id") == party_id), None)
        if not removed:
            logger.warning(
                "delete_relevant_party: id {} not found – re-emitting unchanged",
                party_id,
            )
            await self._emit_relevant_parties(workspace_id, user_id)
            return
        parties = [p for p in parties if p.get("id") != party_id]
        for i, p in enumerate(parties):
            p["id"] = f"party_{i + 1}"
        self._workspace_relevant_parties_payload[workspace_id] = {"parties": parties}
        # Remember the deletion so a later regeneration does not re-add it.
        key = self._party_name_key(removed.get("name"))
        if key:
            deleted = list(self._workspace_deleted_parties.get(workspace_id) or [])
            if key not in deleted:
                deleted.append(key)
            self._workspace_deleted_parties[workspace_id] = deleted
        await self._emit_relevant_parties(workspace_id, user_id)
        logger.info("delete_relevant_party: party removed ({} remain)", len(parties))
