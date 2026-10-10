"""
Workspace context management for maintaining workspace state and data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from app.utils.redact import chars

from app.socket.schemas import EvidenceFile, WorkspaceData
from app.workspace.persistence import CacheKinds, persist_state


class _DictAttrShim:
    """Lightweight attribute-access wrapper used when an evidence-file dict
    cannot be rebuilt into a Pydantic model on cache hydration. Lets call
    sites that use `getattr(ef, "id", None)` keep working."""

    __slots__ = ("_data",)

    def __init__(self, data: dict) -> None:
        self._data = dict(data)

    def __getattr__(self, name: str):
        if name == "_data":
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        # Common alias: backend uses `_id`, downstream uses `id`.
        if name == "id" and "_id" in self._data:
            return self._data.get("_id")
        if name == "_id" and "id" in self._data:
            return self._data.get("id")
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"_DictAttrShim({self._data!r})"


@dataclass
class WorkspaceContext:
    """
    Maintains the context and state for a workspace.
    """

    workspace_id: str
    user_id: str
    title: str
    workspace_role: str
    status: str
    evidence_files: List[EvidenceFile] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_workspace_data(cls, data: WorkspaceData) -> "WorkspaceContext":
        """
        Create WorkspaceContext from WorkspaceData schema.

        Args:
            data: WorkspaceData instance

        Returns:
            WorkspaceContext instance
        """
        return cls(
            workspace_id=data.id,  # Changed from data._id to data.id
            user_id=data.userId,
            title=data.title,
            workspace_role=data.workspaceRole,
            status=data.status,
            evidence_files=data.evidenceFiles,
            created_at=data.createdAt,
            updated_at=data.updatedAt,
            metadata={},
        )

    def update_from_workspace_data(self, data: WorkspaceData) -> None:
        """
        Update context from WorkspaceData.

        Args:
            data: WorkspaceData instance
        """
        self.title = data.title
        self.workspace_role = data.workspaceRole
        self.status = data.status
        self.evidence_files = data.evidenceFiles
        self.updated_at = data.updatedAt
        logger.debug("Updated context for workspace: {}", self.workspace_id)
        self._persist_meta()

    def add_message(
        self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a message to conversation history.

        Args:
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional message metadata
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        self.conversation_history.append(message)
        logger.debug("Added {} message to workspace {}", role, self.workspace_id)

    def get_conversation_history(
        self, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get conversation history.

        Args:
            limit: Optional limit on number of messages to return

        Returns:
            List of conversation messages
        """
        if limit:
            return self.conversation_history[-limit:]
        return self.conversation_history

    def get_evidence_file_urls(self) -> List[str]:
        """
        Get URLs of all evidence files.

        Returns:
            List of evidence file URLs
        """
        return [file.url for file in self.evidence_files]

    def get_evidence_file_count(self) -> int:
        """
        Get count of evidence files.

        Returns:
            Number of evidence files
        """
        return len(self.evidence_files)

    def set_metadata(self, key: str, value: Any) -> None:
        """
        Set metadata value.

        Args:
            key: Metadata key
            value: Metadata value
        """
        self.metadata[key] = value
        logger.debug("Set metadata {} for workspace {}", key, self.workspace_id)
        self._persist_meta()

    def add_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        price: Dict[str, float],
        service: Optional[Dict[str, float]] = None,
        service_units: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Increment the workspace's cumulative usage/price totals and persist.

        Stores the running totals under ``metadata["token_usage"]`` so they ride
        the existing WORKSPACE_META Qdrant cache (see ``to_cache_dict`` /
        ``apply_cache_dict``) and survive an AI-service restart. ``price`` is the
        per-response LLM cost dict from ``app.ai.pricing.price_for`` (keys
        ``inputPrice``, ``outputPrice``, ``price``).

        ``service`` is the optional paid non-LLM cost dict from
        ``app.ai.pricing.service_price_for`` (``ocrPrice``, ``sttPrice``,
        ``searchPrice``, ``groundingPrice``, ``embedPrice``, ``servicePrice``) and
        ``service_units`` the raw quantities behind it (``ocrUnits``,
        ``sttSeconds``, ``searchQueries``, ``groundingRequests``, ``embedTokens``).
        ``servicePrice`` is folded into ``totalPrice``, so ``totalPrice`` is the
        workspace's whole bill — LLM plus OCR/STT/search/grounding/embeddings.

        Returns the updated cumulative snapshot: the ``totalInputTokens`` /
        ``totalOutputTokens`` / ``totalTokens`` / ``totalInputPrice`` /
        ``totalOutputPrice`` / ``totalPrice`` fields plus the per-source
        ``total*`` service fields and ``totalServicePrice``.

        Note every read below uses ``.get(key, 0)``: ``apply_cache_dict``
        rehydrates pre-existing workspaces whose cached ``token_usage`` predates
        the service fields, so the new keys are legitimately absent there.
        """
        in_tok = max(int(input_tokens or 0), 0)
        out_tok = max(int(output_tokens or 0), 0)
        in_price = float(price.get("inputPrice", 0.0) or 0.0)
        out_price = float(price.get("outputPrice", 0.0) or 0.0)
        llm_price = float(price.get("price", in_price + out_price) or 0.0)

        service = service or {}
        service_units = service_units or {}
        service_price = float(service.get("servicePrice", 0.0) or 0.0)

        current = self.metadata.get("token_usage") or {}
        totals = {
            "totalInputTokens": int(current.get("totalInputTokens", 0) or 0) + in_tok,
            "totalOutputTokens": int(current.get("totalOutputTokens", 0) or 0)
            + out_tok,
            "totalInputPrice": round(
                float(current.get("totalInputPrice", 0.0) or 0.0) + in_price, 6
            ),
            "totalOutputPrice": round(
                float(current.get("totalOutputPrice", 0.0) or 0.0) + out_price, 6
            ),
            # Whole bill: LLM tokens + paid non-LLM APIs.
            "totalPrice": round(
                float(current.get("totalPrice", 0.0) or 0.0)
                + llm_price
                + service_price,
                6,
            ),
        }
        totals["totalTokens"] = (
            totals["totalInputTokens"] + totals["totalOutputTokens"]
        )

        # Per-source paid non-LLM totals. Units are counted alongside cost so the
        # backend can show "N pages OCR'd" next to the dollar figure.
        for total_key, source_key, caster in (
            ("totalOcrUnits", "ocrUnits", int),
            ("totalSearchQueries", "searchQueries", int),
            ("totalGroundingRequests", "groundingRequests", int),
            ("totalEmbedTokens", "embedTokens", int),
        ):
            totals[total_key] = caster(current.get(total_key, 0) or 0) + caster(
                service_units.get(source_key, 0) or 0
            )
        totals["totalSttSeconds"] = round(
            float(current.get("totalSttSeconds", 0.0) or 0.0)
            + float(service_units.get("sttSeconds", 0.0) or 0.0),
            3,
        )
        for total_key, source_key in (
            ("totalOcrPrice", "ocrPrice"),
            ("totalSttPrice", "sttPrice"),
            ("totalSearchPrice", "searchPrice"),
            ("totalGroundingPrice", "groundingPrice"),
            ("totalEmbedPrice", "embedPrice"),
            ("totalServicePrice", "servicePrice"),
        ):
            totals[total_key] = round(
                float(current.get(total_key, 0.0) or 0.0)
                + float(service.get(source_key, 0.0) or 0.0),
                6,
            )

        # set_metadata persists the whole context (fire-and-forget to Qdrant).
        self.set_metadata("token_usage", totals)
        return totals

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """
        Get metadata value.

        Args:
            key: Metadata key
            default: Default value if key not found

        Returns:
            Metadata value
        """
        return self.metadata.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert context to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "title": self.title,
            "workspace_role": self.workspace_role,
            "status": self.status,
            "evidence_files_count": len(self.evidence_files),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "conversation_count": len(self.conversation_history),
            "metadata": self.metadata,
        }

    # ------------------------------------------------------------------
    # Persistence helpers (Qdrant-backed cache so context survives restart)
    # ------------------------------------------------------------------

    def _serialize_evidence_file(self, ef: Any) -> Dict[str, Any]:
        """Convert an EvidenceFile / EvidenceFilePayload to a JSON-friendly dict."""
        if ef is None:
            return {}
        # Pydantic models support model_dump; fall back to __dict__/getattr.
        try:
            dumped = ef.model_dump(mode="json", by_alias=True)  # type: ignore[attr-defined]
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
        try:
            dumped = ef.model_dump()  # type: ignore[attr-defined]
            if isinstance(dumped, dict):
                # Normalize datetime serialization for round-tripping.
                for k, v in list(dumped.items()):
                    if isinstance(v, datetime):
                        dumped[k] = v.isoformat()
                return dumped
        except Exception:
            pass
        # Best-effort plain object.
        return {
            "id": getattr(ef, "id", None) or getattr(ef, "_id", None),
            "filename": getattr(ef, "filename", None),
            "url": getattr(ef, "url", None),
            "path": getattr(ef, "path", None),
            "uploadedAt": (
                getattr(ef, "uploadedAt", None).isoformat()
                if isinstance(getattr(ef, "uploadedAt", None), datetime)
                else getattr(ef, "uploadedAt", None)
            ),
        }

    def to_cache_dict(self) -> Dict[str, Any]:
        """JSON-friendly representation used by the persistence layer."""
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "title": self.title,
            "workspace_role": self.workspace_role,
            "status": self.status,
            "evidence_files": [
                self._serialize_evidence_file(ef) for ef in (self.evidence_files or [])
            ],
            "created_at": (
                self.created_at.isoformat()
                if isinstance(self.created_at, datetime)
                else self.created_at
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if isinstance(self.updated_at, datetime)
                else self.updated_at
            ),
            "metadata": self.metadata,
        }

    def apply_cache_dict(self, data: Dict[str, Any]) -> None:
        """Restore non-conversation state from a cached payload.

        Conversation history is intentionally NOT mirrored to Qdrant; the
        chat server replays conversation_history on every user_message_request.
        """
        if not isinstance(data, dict):
            return
        title = data.get("title")
        if isinstance(title, str) and title and not self.title:
            self.title = title
        status = data.get("status")
        if isinstance(status, str) and status and not self.status:
            self.status = status
        role = data.get("workspace_role")
        if isinstance(role, str) and role and not self.workspace_role:
            self.workspace_role = role
        evidence = data.get("evidence_files")
        if isinstance(evidence, list) and evidence and not self.evidence_files:
            self.evidence_files = self._restore_evidence_files(evidence)
        meta = data.get("metadata")
        if isinstance(meta, dict):
            for k, v in meta.items():
                # Don't overwrite metadata that was already set in this process.
                self.metadata.setdefault(k, v)
        try:
            updated_at = data.get("updated_at")
            if isinstance(updated_at, str) and updated_at:
                self.updated_at = datetime.fromisoformat(updated_at)
        except Exception:
            pass

    @staticmethod
    def _restore_evidence_files(items: List[Dict[str, Any]]) -> List[Any]:
        """Rebuild EvidenceFile-like objects from cached dicts.

        Downstream call sites (e.g. annexure label lookup in the chat handler)
        access fields via `getattr(ef, "id", None)`, which fails on plain
        dicts. We rehydrate to pydantic models when possible and fall back to
        a `_DictAttrShim` so attribute access keeps working either way.
        """
        out: List[Any] = []
        # Lazy import to avoid circulars at module load.
        try:
            from app.socket.schemas import EvidenceFilePayload
        except Exception:
            EvidenceFilePayload = None  # type: ignore[assignment]

        for raw in items:
            if not isinstance(raw, dict):
                out.append(raw)
                continue
            rebuilt = None
            if EvidenceFilePayload is not None:
                try:
                    rebuilt = EvidenceFilePayload(**raw)
                except Exception:
                    rebuilt = None
            if rebuilt is None:
                rebuilt = _DictAttrShim(raw)
            out.append(rebuilt)
        return out

    def _persist_meta(self) -> None:
        """Schedule a fire-and-forget Qdrant write of this context."""
        try:
            persist_state(
                workspace_id=self.workspace_id,
                kind=CacheKinds.WORKSPACE_META,
                data=self.to_cache_dict(),
            )
        except Exception as e:  # pragma: no cover - persistence is best-effort
            logger.debug(
                "Workspace meta persistence skipped (workspace={}): {}",
                self.workspace_id,
                e,
            )


class ContextManager:
    """Manages all workspace contexts."""

    def __init__(self):
        self._contexts: Dict[str, WorkspaceContext] = {}

    def create_context(self, data: WorkspaceData) -> WorkspaceContext:
        """
        Create a new workspace context.

        Args:
            data: WorkspaceData instance

        Returns:
            Created WorkspaceContext
        """
        context = WorkspaceContext.from_workspace_data(data)
        self._contexts[context.workspace_id] = context
        logger.info("Created context for workspace: {}", context.workspace_id)
        context._persist_meta()
        return context

    def get_context(self, workspace_id: str) -> Optional[WorkspaceContext]:
        """
        Get workspace context.

        Args:
            workspace_id: Workspace identifier

        Returns:
            WorkspaceContext if found, None otherwise
        """
        return self._contexts.get(workspace_id)

    def update_context(self, data: WorkspaceData) -> WorkspaceContext:
        """
        Update existing context or create new one.

        Args:
            data: WorkspaceData instance

        Returns:
            Updated or created WorkspaceContext
        """
        context = self._contexts.get(data.id)

        if context:
            context.update_from_workspace_data(data)
            logger.debug("Updated context for workspace: {}", data.id)
        else:
            context = self.create_context(data)
            logger.info("Created new context for workspace: {}", data.id)

        return context

    def upsert_workspace(
        self,
        workspace_id: str,
        user_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        workspace_role: Optional[str] = None,
        evidence_files: Optional[List[Any]] = None,
        updated_at: Optional[datetime] = None,
    ) -> WorkspaceContext:
        """
        Create or update workspace context from sync/update payload.
        Used when backend sends workspace_sync (e.g. role change in Edit Workspace)
        so that get_workspace(workspace_id) returns the correct role for chat and strategy.
        """
        context = self._contexts.get(workspace_id)
        now = datetime.utcnow()
        if context:
            if title is not None:
                context.title = title
            if status is not None:
                context.status = status
            if workspace_role is not None:
                context.workspace_role = (
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
                if context.workspace_role not in (
                    "ADJUDICATOR",
                    "PETITIONER",
                    "RESPONDENT",
                    "MEDIATOR",
                ):
                    context.workspace_role = "ADJUDICATOR"
            if evidence_files is not None:
                context.evidence_files = list(evidence_files)
            if updated_at is not None:
                context.updated_at = updated_at
            logger.debug(
                "Updated context for workspace: {} (role={})",
                workspace_id,
                context.workspace_role,
            )
            context._persist_meta()
            return context
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
            role = "ADJUDICATOR"
        context = WorkspaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            title=title or "",
            workspace_role=role,
            status=status or "active",
            evidence_files=list(evidence_files) if evidence_files else [],
            created_at=now,
            updated_at=updated_at or now,
        )
        self._contexts[workspace_id] = context
        logger.info("Created context for workspace: {} (role={})", workspace_id, role)
        context._persist_meta()
        return context

    def transfer_ownership(
        self, workspace_id: str, new_user_id: str
    ) -> Optional[WorkspaceContext]:
        """Re-own an in-memory workspace context after workspace_transferred.

        upsert_workspace deliberately never changes user_id for an existing
        context (chat paths call it with the message's userId); this is the
        one sanctioned path that does. Returns None when the context is cold —
        the caller's upsert_workspace already creates it with the new owner.
        """
        context = self._contexts.get(workspace_id)
        if not context:
            return None
        context.user_id = new_user_id
        context.updated_at = datetime.utcnow()
        context._persist_meta()
        logger.info(
            "Workspace {} context re-owned to user {}",
            workspace_id,
            (new_user_id or "")[:12],
        )
        return context

    async def hydrate_context_from_cache(
        self,
        workspace_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[WorkspaceContext]:
        """Restore a workspace context from Qdrant when memory is cold.

        Returns the rehydrated context (also stored in `_contexts`) or None
        when no cache row exists. Conversation history is intentionally NOT
        restored — the chat server already replays it per request.
        """
        if not workspace_id:
            return None
        # Defer import to avoid a circular dependency at module load.
        from app.workspace.persistence import CacheKinds, restore_state

        cached = await restore_state(
            workspace_id=workspace_id, kind=CacheKinds.WORKSPACE_META
        )
        if not isinstance(cached, dict):
            return None

        existing = self._contexts.get(workspace_id)
        if existing:
            existing.apply_cache_dict(cached)
            return existing

        cached_role = (
            (cached.get("workspace_role") or "ADJUDICATOR").strip().upper()
        )
        if cached_role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
            cached_role = "ADJUDICATOR"
        try:
            created_at = datetime.fromisoformat(str(cached.get("created_at")))
        except Exception:
            created_at = datetime.utcnow()
        try:
            updated_at = datetime.fromisoformat(str(cached.get("updated_at")))
        except Exception:
            updated_at = created_at

        context = WorkspaceContext(
            workspace_id=workspace_id,
            user_id=str(cached.get("user_id") or user_id or ""),
            title=str(cached.get("title") or ""),
            workspace_role=cached_role,
            status=str(cached.get("status") or "active"),
            evidence_files=list(cached.get("evidence_files") or []),
            created_at=created_at,
            updated_at=updated_at,
            metadata=dict(cached.get("metadata") or {}),
        )
        self._contexts[workspace_id] = context
        logger.info(
            "Rehydrated workspace context from Qdrant cache: {} (role={}, files={})",
            workspace_id,
            cached_role,
            len(context.evidence_files or []),
        )
        return context

    def delete_context(self, workspace_id: str) -> bool:
        """
        Delete workspace context.

        Args:
            workspace_id: Workspace identifier

        Returns:
            True if deleted, False if not found
        """
        if workspace_id in self._contexts:
            del self._contexts[workspace_id]
            logger.info("Deleted context for workspace: {}", workspace_id)
            return True
        return False

    def has_context(self, workspace_id: str) -> bool:
        """
        Check if context exists.

        Args:
            workspace_id: Workspace identifier

        Returns:
            True if context exists
        """
        return workspace_id in self._contexts

    def get_all_workspace_ids(self) -> List[str]:
        """
        Get all workspace IDs.

        Returns:
            List of workspace IDs
        """
        return list(self._contexts.keys())

    def clear_all(self) -> None:
        """Clear all contexts."""
        self._contexts.clear()
        logger.info("Cleared all workspace contexts")


# Global context manager instance
context_manager = ContextManager()
