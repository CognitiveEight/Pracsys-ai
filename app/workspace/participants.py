"""
Per-workspace collaborator roster (workspace sharing).

The backend owns sharing. It tells this service about it in two places:

- ``workspace_sync.participants`` — the full roster of people the case owner
  has shared the workspace with, each with a ``permission`` of ``edit`` or
  ``view``. The field is **absent entirely** (not an empty array) when the case
  is not shared.
- ``user_message_request.askedBy`` — present only when the message was typed by
  someone *other* than the case owner. ``userId`` on that payload stays the
  OWNER's id, so every owner-scoped cache (role, token usage, panels) keeps its
  existing key; ``askedBy`` is purely additive.

This module is the join between the two: it stores the roster and resolves an
``askedBy`` against it into an :class:`AskerContext`, which answers the only
question the rest of the service needs — *may this person change the case, or
may they only read it?*

Deliberately **not** wired into any legal prompt. Participants are application
users, not parties to the dispute; leaking them into the analysis/parties
prompts would corrupt `relevant_parties`, which is extracted from the evidence.
The one place the roster reaches a prompt is a short attribution line naming
who asked the current chat question (see ``chat_context_builder_node``).

Mirrors ``app/workspace/custom_instructions.py``: a module-level dict with
write-through persistence to Qdrant, hydration on a cold workspace, and an
explicit ``clear`` for the erasure path (module-level state is NOT reached by
the event handler's ``self.__dict__`` purge sweep).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

# workspace_id -> ordered {user_id: {"userId", "name", "permission"}}
_participants: Dict[str, Dict[str, Dict[str, str]]] = {}

PERMISSION_EDIT = "edit"
PERMISSION_VIEW = "view"

# Permissions that may change the case. The backend currently sends only
# ``edit`` / ``view``; the synonyms are accepted defensively so a future rename
# on the backend does not silently demote every editor to read-only. Anything
# not in this set — including an unknown or missing value — is read-only, which
# is the safe direction to fail.
WRITE_PERMISSIONS = frozenset({"edit", "write", "manage", "admin", "owner"})


@dataclass(frozen=True)
class AskerContext:
    """Who actually typed the current chat message, and what they may do.

    ``is_owner`` is true both for the case owner and whenever the payload
    carried no ``askedBy`` at all (an unshared case, or the owner typing).
    """

    user_id: str
    name: str
    is_owner: bool
    permission: str
    can_edit: bool
    # False when the asker is not the owner AND is not on the cached roster.
    # Kept separate from ``can_edit`` so the caller can tell "explicitly a
    # viewer" apart from "we have never been told about this person".
    known: bool

    @property
    def is_read_only(self) -> bool:
        return not self.can_edit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "userId": self.user_id,
            "name": self.name,
            "isOwner": self.is_owner,
            "permission": self.permission,
            "canEdit": self.can_edit,
        }

    def log_label(self) -> str:
        """Short, id-free label for logs (participant names are user data)."""
        if self.is_owner:
            return "owner"
        return f"collaborator/{self.permission}"


def _persist(workspace_id: str) -> None:
    """Mirror the current roster for `workspace_id` to Qdrant."""
    try:
        from app.workspace.persistence import CacheKinds, persist_state

        persist_state(
            workspace_id=workspace_id,
            kind=CacheKinds.PARTICIPANTS,
            data=get_participants(workspace_id),
        )
    except Exception:
        # Persistence is best-effort; never block the caller.
        pass


def normalize_permission(value: Any) -> str:
    """Lower-case a permission string. Unknown/missing becomes ``view``."""
    permission = str(value or "").strip().lower()
    return permission or PERMISSION_VIEW


def permission_can_edit(value: Any) -> bool:
    """True when `value` is a permission that may change the workspace."""
    return normalize_permission(value) in WRITE_PERMISSIONS


def _coerce_participant(raw: Any) -> Optional[Dict[str, str]]:
    """Normalize one participant (pydantic model or dict) to a plain dict."""
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        try:
            raw = raw.model_dump()
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    user_id = str(raw.get("userId") or raw.get("user_id") or raw.get("id") or "").strip()
    if not user_id:
        return None
    return {
        "userId": user_id,
        "name": str(raw.get("name") or "").strip(),
        "permission": normalize_permission(raw.get("permission")),
    }


def set_participants(
    workspace_id: str,
    participants: Optional[List[Any]],
    *,
    authoritative: bool = True,
) -> None:
    """Replace the roster for a workspace.

    Args:
        workspace_id: Workspace the roster belongs to.
        participants: The payload's ``participants`` list, or ``None`` when the
            field was absent.
        authoritative: Whether ``None`` means "the case is not shared" (clear
            the roster) or "this payload simply does not carry sharing state"
            (leave the roster alone).

            ``workspace_sync`` is authoritative — the backend documents the
            field as absent *because* the case is not shared, so an absent
            field is how an un-share reaches us, and keeping a stale roster
            would leave a removed collaborator holding edit rights.
            ``workspace_version_restore`` is not: it is a document-version
            operation that says nothing about sharing.
    """
    if not workspace_id:
        return

    if participants is None:
        if not authoritative:
            return
        previous = _participants.pop(workspace_id, None)
        if previous:
            logger.info(
                "Workspace {} no longer shared — dropped {} participant(s)",
                workspace_id,
                len(previous),
            )
            _persist(workspace_id)
        return

    roster: Dict[str, Dict[str, str]] = {}
    for raw in participants:
        entry = _coerce_participant(raw)
        if entry:
            # Last write wins on a duplicated userId.
            roster[entry["userId"]] = entry

    previous = _participants.get(workspace_id) or {}
    _participants[workspace_id] = roster
    if roster != previous:
        logger.info(
            "Workspace {} participants updated: {} collaborator(s) ({} with edit)",
            workspace_id,
            len(roster),
            sum(1 for p in roster.values() if permission_can_edit(p["permission"])),
        )
    _persist(workspace_id)


def get_participants(workspace_id: str) -> List[Dict[str, str]]:
    """Roster for a workspace as a list. Empty when not shared/unknown."""
    return [dict(p) for p in (_participants.get(workspace_id) or {}).values()]


def get_participant(workspace_id: str, user_id: str) -> Optional[Dict[str, str]]:
    """One participant by user id, or None when they are not on the roster."""
    if not workspace_id or not user_id:
        return None
    entry = (_participants.get(workspace_id) or {}).get(str(user_id).strip())
    return dict(entry) if entry else None


def has_roster(workspace_id: str) -> bool:
    """True when we have been told this workspace is shared with someone."""
    return bool(_participants.get(workspace_id))


def resolve_asker(
    workspace_id: str,
    owner_user_id: Optional[str],
    asked_by: Any = None,
) -> AskerContext:
    """Work out who is asking and whether they may change the case.

    Rules, in order:

    1. No ``askedBy``, or an ``askedBy`` whose id equals the owner's → the
       owner. Full rights; this is the unshared/normal path.
    2. On the roster → their ``permission`` decides (``edit`` writes, anything
       else reads).
    3. Not on a roster we *do* have → read-only. They are not a known
       collaborator, so least privilege applies.
    4. Not on a roster we do NOT have (nothing cached for this workspace — the
       sharing sync has not reached this process yet) → allowed to edit, and
       logged. Failing closed here would break every collaborator on a case
       whose roster we simply have not seen, which is the more likely fault.
    """
    owner_id = str(owner_user_id or "").strip()

    if hasattr(asked_by, "model_dump"):
        try:
            asked_by = asked_by.model_dump()
        except Exception:
            asked_by = None
    asker_id = ""
    asker_name = ""
    if isinstance(asked_by, dict):
        asker_id = str(asked_by.get("userId") or asked_by.get("user_id") or "").strip()
        asker_name = str(asked_by.get("name") or "").strip()

    if not asker_id or (owner_id and asker_id == owner_id):
        return AskerContext(
            user_id=asker_id or owner_id,
            name=asker_name,
            is_owner=True,
            permission=PERMISSION_EDIT,
            can_edit=True,
            known=True,
        )

    participant = get_participant(workspace_id, asker_id)
    if participant:
        permission = normalize_permission(participant.get("permission"))
        return AskerContext(
            user_id=asker_id,
            name=asker_name or participant.get("name") or "",
            is_owner=False,
            permission=permission,
            can_edit=permission_can_edit(permission),
            known=True,
        )

    if has_roster(workspace_id):
        logger.warning(
            "Chat from a user who is not on workspace {}'s participant roster "
            "— treating as read-only",
            workspace_id,
        )
        return AskerContext(
            user_id=asker_id,
            name=asker_name,
            is_owner=False,
            permission=PERMISSION_VIEW,
            can_edit=False,
            known=False,
        )

    logger.warning(
        "Chat from a non-owner on workspace {} but no participant roster is "
        "cached — allowing edits (roster has not synced to this process)",
        workspace_id,
    )
    return AskerContext(
        user_id=asker_id,
        name=asker_name,
        is_owner=False,
        permission=PERMISSION_EDIT,
        can_edit=True,
        known=False,
    )


def hydrate_participants_from_cache(workspace_id: str, data: Any) -> None:
    """Populate the in-memory roster from a previously persisted payload.

    No-ops when the live process already holds a roster for the workspace (it
    is the source of truth) or when the cached payload is missing/empty.
    """
    if not workspace_id or not data:
        return
    if _participants.get(workspace_id):
        return
    if isinstance(data, dict):
        # Tolerate a wrapped {"participants": [...]} shape.
        data = data.get("participants")
    if not isinstance(data, list):
        return
    roster: Dict[str, Dict[str, str]] = {}
    for raw in data:
        entry = _coerce_participant(raw)
        if entry:
            roster[entry["userId"]] = entry
    if roster:
        _participants[workspace_id] = roster


def clear_participants(workspace_id: str) -> None:
    """Forget a workspace's roster.

    Called on workspace deletion. ``_participants`` is a module-level dict, so
    it is NOT reached by the event handler's ``self.__dict__`` purge sweep —
    without this, collaborator names would survive an erasure request for the
    life of the process.
    """
    _participants.pop(workspace_id, None)
