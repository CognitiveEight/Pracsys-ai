"""Global Template Library feed (SIM-173, AI side of SIM-58).

One platform-wide template library replaces each lawyer's own; the per-lawyer
template_sync events are no longer handled. Covers the
template store's routing (published global list, retired templates, version
pins, write-backs) and the socket handlers (snapshot on connect, published /
retired / deleted pushes, global profiling, drafting from a pinned version).
Download, extraction and profiling are fakes. Fictional data only; no model,
GCS, Qdrant or socket calls.
"""

import asyncio

import pytest

from app.core.config import settings
from app.langgraph import template_store as ts
from app.langgraph.template_store import (
    GLOBAL_DRAFT_OWNER,
    GLOBAL_OWNER,
    GLOBAL_SCOPE,
    STATUS_PUBLISHED,
    STATUS_RETIRED,
    TemplateStore,
    pin_template_version,
    template_store,
    version_pins,
)
from app.socket.schemas import UserMessageRequestPayload

LAWYER = "lawyer-fictional-1"
OTHER_LAWYER = "lawyer-fictional-2"
WRIT = "6720a1b2c3d4e5f6a7b8c9d0"
NOTICE = "6720a1b2c3d4e5f6a7b8c9d1"
OWN = "66f0c0ffee0000000000beef"


def _payload(template_id, *, version=None, file_hash="h1", text="FICTIONAL TEMPLATE BODY"):
    payload = {
        "template_id": template_id,
        "name": f"Template {template_id[-2:]}",
        "file_hash": file_hash,
        "extracted_text": text,
        "extraction_status": "ready",
    }
    if version is not None:
        payload.update({"scope": GLOBAL_SCOPE, "version": version})
    return payload


async def _publish(store, template_id, version, file_hash="h1"):
    await store.put(
        user_id=GLOBAL_OWNER,
        template_id=template_id,
        payload=_payload(template_id, version=version, file_hash=file_hash),
    )
    await store.set_global_status(template_id=template_id, status=STATUS_PUBLISHED)


# ============================================================ template store


async def test_every_lawyer_is_offered_the_published_global_library_only():
    store = TemplateStore()
    assert await store.list_for_user(user_id=LAWYER) == []

    # A template lazily fetched for a draft that predates the library resolves
    # for that draft but is never offered; neither is an admin draft.
    await store.put(user_id=LAWYER, template_id=OWN, payload=_payload(OWN))
    await store.put(user_id=GLOBAL_DRAFT_OWNER, template_id=WRIT, payload=_payload(WRIT))
    assert await store.count_for_user(user_id=LAWYER) == 0
    assert not await store.global_active()
    assert (await store.get(user_id=LAWYER, template_id=OWN))["template_id"] == OWN

    await _publish(store, WRIT, 1)
    assert await store.global_active()
    for lawyer in (LAWYER, OTHER_LAWYER):
        assert [p["template_id"] for p in await store.list_for_user(user_id=lawyer)] == [WRIT]


async def test_retired_template_is_not_offered_but_still_resolves():
    store = TemplateStore()
    await _publish(store, WRIT, 4)
    await _publish(store, NOTICE, 1)
    await store.set_global_status(template_id=WRIT, status=STATUS_RETIRED)

    assert [p["template_id"] for p in await store.list_for_user(user_id=LAWYER)] == [NOTICE]
    assert not await store.is_suggestable(user_id=LAWYER, template_id=WRIT)
    assert await store.is_suggestable(user_id=LAWYER, template_id=NOTICE)
    assert (await store.get(user_id=LAWYER, template_id=WRIT))["version"] == 4


async def test_draft_keeps_the_version_it_started_from():
    store = TemplateStore()
    await _publish(store, WRIT, 3, file_hash="h3")
    await _publish(store, WRIT, 4, file_hash="h4")

    assert (await store.get(user_id=LAWYER, template_id=WRIT))["version"] == 4
    with version_pins():
        pin_template_version(WRIT, 3)
        held = await store.get(user_id=LAWYER, template_id=WRIT)
        assert held["version"] == 3 and held["file_hash"] == "h3"
    with version_pins():
        pin_template_version(WRIT, "4")
        assert (await store.get(user_id=LAWYER, template_id=WRIT))["version"] == 4
    with version_pins():
        # A version we never held: None, so the drafting turn lazy-fetches it.
        pin_template_version(WRIT, 2)
        assert await store.get(user_id=LAWYER, template_id=WRIT) is None
    # Pins never leak out of the turn.
    assert ts.pinned_version(WRIT) is None


async def test_write_back_goes_to_the_version_it_came_from():
    store = TemplateStore()
    await _publish(store, WRIT, 3, file_hash="h3")
    await _publish(store, WRIT, 4, file_hash="h4")

    with version_pins():
        pin_template_version(WRIT, 3)
        old = await store.get(user_id=LAWYER, template_id=WRIT)
        old["template_docx_gcs_uri"] = "gs://fictional/v3.docx"
        await store.put(user_id=LAWYER, template_id=WRIT, payload=old)
        assert (await store.get(user_id=LAWYER, template_id=WRIT))[
            "template_docx_gcs_uri"
        ] == "gs://fictional/v3.docx"

    current = await store.get(user_id=LAWYER, template_id=WRIT)
    assert current["version"] == 4 and "template_docx_gcs_uri" not in current
    # A write-back never changes the admin's status, and never lands in a lawyer bucket.
    current["global_status"] = STATUS_RETIRED
    await store.put(user_id=LAWYER, template_id=WRIT, payload=current)
    assert await store.is_suggestable(user_id=LAWYER, template_id=WRIT)
    assert await store.delete(user_id=LAWYER, template_id=WRIT) is False


async def test_forget_global_erases_every_copy():
    store = TemplateStore()
    await store.put(user_id=GLOBAL_DRAFT_OWNER, template_id=WRIT, payload=_payload(WRIT))
    await _publish(store, WRIT, 1, file_hash="h1")
    await _publish(store, WRIT, 2, file_hash="h2")

    assert await store.forget_global(template_id=WRIT) == 3
    assert await store.get(user_id=LAWYER, template_id=WRIT) is None
    assert await store.global_ids() == []


# ============================================================ socket handlers


class _Socket:
    def __init__(self):
        self.sent = []

    async def emit(self, event, payload):  # a test double reports delivery
        self.sent.append((event, payload))
        return True

    def of(self, event):
        return [p for e, p in self.sent if e == event]


class _Agent:
    """Fake download + profile: records what was fetched and profiled."""

    def __init__(self):
        self.ingests = []
        self.proposals = []
        self.fail = False

    async def ingest_template(self, *, user_id, template_id, file_url, file_hash,
                              previous_payload=None, scope=None, version=None,
                              formatting=None, **_kw):
        self.ingests.append({
            "user_id": user_id, "template_id": template_id, "file_hash": file_hash,
            "reused": bool(previous_payload)
            and previous_payload.get("file_hash") == file_hash,
            "scope": scope, "version": version, "formatting": formatting,
        })
        if self.fail:
            return None
        payload = _payload(template_id, file_hash=file_hash)
        payload["user_id"] = user_id
        if scope == GLOBAL_SCOPE:
            payload.update({"scope": scope, "version": version})
        await template_store.put(user_id=user_id, template_id=template_id, payload=payload)
        return payload

    async def build_blueprint_proposal(self, *, user_id, template_id, force_reprofile=False):
        self.proposals.append({"user_id": user_id, "force_reprofile": force_reprofile})
        tpl = await template_store.get(user_id=user_id, template_id=template_id)
        return {
            "userId": user_id,
            "templateId": template_id,
            "fileHash": tpl.get("file_hash"),
            "status": "ready",
            "draftCategory": "Petition",
            "blueprint": {"variables": [{"id": "v1", "label": "Petitioner"}]},
        }

    async def store_preset(self, *, user_id, template_id, name, category, formatting):
        await template_store.put(
            user_id=user_id,
            template_id=template_id,
            payload={"template_id": template_id, "kind": "preset", "name": name,
                     "category": category, "formatting": formatting or {}},
        )
        return True


def _item(template_id, version, file_hash="h1", **extra):
    item = {
        "id": template_id,
        "version": version,
        "name": "High Court Writ Petition",
        "originalName": "writ-petition.docx",
        "category": "Document Draft",
        "kind": "file",
        "extension": "docx",
        "fileHash": file_hash,
        "fileUrl": "https://storage.example.invalid/signed",
        "formatting": {"page": {}, "blueprint": {}},
    }
    item.update(extra)
    return item


@pytest.fixture
def env(monkeypatch):
    from app.langgraph import drafting_agent as da
    from app.socket.events import event_handler as h

    monkeypatch.setattr(template_store, "_store", {})
    monkeypatch.setattr(template_store, "_global_versions", {})
    monkeypatch.setattr(settings, "template_sync_timeout_seconds", 2.0)
    socket = _Socket()
    monkeypatch.setattr(h, "_socket_client", socket)
    monkeypatch.setattr(h, "_pending_template_sync", {})
    monkeypatch.setattr(h, "_drafting_meta_by_workspace", {})
    monkeypatch.setattr(h, "_global_templates_synced", False)
    agent = _Agent()
    for name in ("ingest_template", "build_blueprint_proposal", "store_preset"):
        monkeypatch.setattr(da.drafting_agent, name, getattr(agent, name))
    return h, socket, agent


async def _snapshot(h, socket, templates):
    """Run the connect-time request against a backend that answers with ``templates``."""
    before = len(socket.of("request_global_template_sync"))
    task = asyncio.create_task(h.request_global_template_sync())
    for _ in range(100):
        if len(socket.of("request_global_template_sync")) > before:
            break
        await asyncio.sleep(0.01)
    request_id = socket.of("request_global_template_sync")[-1]["requestId"]
    await h.on_global_template_sync_response(
        {"requestId": request_id, "count": len(templates), "templates": templates,
         "timestamp": "2026-11-05T09:30:00.000Z"}
    )
    await asyncio.wait_for(task, 2)
    return request_id


async def test_connect_snapshot_replaces_the_set_and_accepts_an_empty_list(env):
    h, socket, agent = env
    request_id = await _snapshot(h, socket, [])
    assert request_id.startswith("gts-")
    assert not await template_store.global_active()
    assert h._pending_template_sync == {}

    await _snapshot(h, socket, [_item(WRIT, 3), _item(NOTICE, 1, file_hash="hn")])
    assert sorted(await template_store.global_ids()) == sorted([WRIT, NOTICE])
    assert all(i["scope"] == GLOBAL_SCOPE for i in agent.ingests)
    # The empty "blueprint": {} never reaches ingest as an approved blueprint.
    assert all("blueprint" not in (i["formatting"] or {}) for i in agent.ingests)

    # NOTICE was retired while we were away: absent from the next snapshot.
    await _snapshot(h, socket, [_item(WRIT, 3)])
    listed = [p["template_id"] for p in await template_store.list_for_user(user_id=LAWYER)]
    assert listed == [WRIT]
    assert len(agent.ingests) == 2  # the unchanged WRIT v3 was not fetched again


async def test_published_resend_is_a_no_op_and_versions_reuse_the_same_file(env):
    h, _socket, agent = env
    await h.on_global_template_sync({"action": "published", "templates": [_item(WRIT, 1)]})
    await h.on_global_template_sync({"action": "published", "templates": [_item(WRIT, 1)]})
    assert len(agent.ingests) == 1

    # v2 with the same file (name / formatting change): no fresh download.
    await h.on_global_template_sync({"action": "published", "templates": [_item(WRIT, 2)]})
    assert agent.ingests[-1]["reused"] and agent.ingests[-1]["version"] == 2

    # v3 with a new file: fetched afresh; v2 is kept for drafts started on it.
    await h.on_global_template_sync(
        {"action": "published", "templates": [_item(WRIT, 3, file_hash="h3")]}
    )
    assert not agent.ingests[-1]["reused"]
    assert (await template_store.get(user_id=LAWYER, template_id=WRIT))["version"] == 3
    with version_pins():
        pin_template_version(WRIT, 2)
        assert (await template_store.get(user_id=LAWYER, template_id=WRIT))["version"] == 2


async def test_retired_and_deleted_pushes(env):
    h, _socket, agent = env
    await h.on_global_template_sync({"action": "published", "templates": [_item(WRIT, 4)]})
    await h.on_global_template_sync(
        {"action": "retired", "templates": [{"id": WRIT, "version": 4, "kind": "file"}]}
    )
    assert not await template_store.is_suggestable(user_id=LAWYER, template_id=WRIT)
    assert await template_store.get(user_id=LAWYER, template_id=WRIT) is not None

    # Published again: back on offer.
    await h.on_global_template_sync({"action": "published", "templates": [_item(WRIT, 4)]})
    assert await template_store.is_suggestable(user_id=LAWYER, template_id=WRIT)

    await template_store.put(
        user_id=GLOBAL_DRAFT_OWNER, template_id=NOTICE, payload=_payload(NOTICE)
    )
    await h.on_global_template_sync(
        {"action": "deleted", "templates": [{"id": NOTICE, "version": 1}]}
    )
    assert await template_store.get(user_id=GLOBAL_DRAFT_OWNER, template_id=NOTICE) is None


async def test_failed_new_version_keeps_the_previous_one(env):
    h, _socket, agent = env
    await h.on_global_template_sync({"action": "published", "templates": [_item(WRIT, 1)]})
    agent.fail = True
    await h.on_global_template_sync(
        {"action": "published", "templates": [_item(WRIT, 2, file_hash="h2")]}
    )
    current = await template_store.get(user_id=LAWYER, template_id=WRIT)
    assert current["version"] == 1
    assert await template_store.is_suggestable(user_id=LAWYER, template_id=WRIT)


async def test_preset_is_stored_globally(env):
    h, _socket, agent = env
    preset = {"id": NOTICE, "version": 1, "name": "Delhi High Court format",
              "category": "High Court", "kind": "preset", "extension": "",
              "formatting": {"page": {}, "court": {"courtName": "Fictional High Court"}}}
    await h.on_global_template_sync({"action": "published", "templates": [preset]})
    held = await template_store.get(user_id=LAWYER, template_id=NOTICE)
    assert held["kind"] == "preset" and held["scope"] == GLOBAL_SCOPE and held["version"] == 1
    assert agent.ingests == []


async def test_global_profiling_reply(env):
    h, socket, agent = env
    request = {
        "scope": "global", "templateId": WRIT, "fileHash": "hd",
        "fileUrl": "https://storage.example.invalid/signed",
        "originalName": "writ-petition-v4.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "timestamp": "2026-11-05T10:00:00.000Z",
    }
    await h.on_request_blueprint_proposal(dict(request))
    reply = socket.of("template_blueprint_proposal")[-1]
    assert reply["scope"] == "global" and "userId" not in reply
    assert reply["fileHash"] == "hd" and reply["status"] == "ready"
    assert agent.ingests[-1]["user_id"] == GLOBAL_DRAFT_OWNER
    assert agent.proposals[-1] == {"user_id": GLOBAL_DRAFT_OWNER, "force_reprofile": False}
    # A draft is never offered to lawyers.
    assert await template_store.count_for_user(user_id=LAWYER) == 0

    # "Re-profile" on the same file: no new download, a forced profile.
    await h.on_request_blueprint_proposal(dict(request))
    assert len(agent.ingests) == 1
    assert agent.proposals[-1]["force_reprofile"] is True

    # Publishing that file reuses the draft's extraction.
    await h.on_global_template_sync(
        {"action": "published", "templates": [_item(WRIT, 1, file_hash="hd")]}
    )
    assert agent.ingests[-1]["reused"]

    agent.fail = True
    await h.on_request_blueprint_proposal(dict(request, templateId=NOTICE, fileHash="hx"))
    failed = socket.of("template_blueprint_proposal")[-1]
    assert failed["status"] == "failed" and failed["scope"] == "global"
    assert failed["fileHash"] == "hx" and "userId" not in failed


async def test_profiling_request_without_scope_is_global(env):
    h, socket, agent = env
    await h.on_request_blueprint_proposal(
        {"templateId": WRIT, "fileHash": "h1",
         "fileUrl": "https://storage.example.invalid/signed",
         "originalName": "writ-petition.docx"}
    )
    reply = socket.of("template_blueprint_proposal")[-1]
    assert reply["scope"] == "global" and "userId" not in reply
    assert agent.ingests[-1]["user_id"] == GLOBAL_DRAFT_OWNER


async def test_template_sync_is_requested_once_per_connection(env):
    h, socket, _agent = env
    await _snapshot(h, socket, [])
    assert h._global_templates_synced
    await h.ensure_templates_synced(timeout=0.5)
    assert len(socket.of("request_global_template_sync")) == 1
    assert h._prewarm_templates() is None

    # A forced sync asks again; a per-lawyer request is never sent.
    task = asyncio.create_task(h.ensure_templates_synced(timeout=2, force=True))
    for _ in range(100):
        if len(socket.of("request_global_template_sync")) == 2:
            break
        await asyncio.sleep(0.01)
    request_id = socket.of("request_global_template_sync")[-1]["requestId"]
    await h.on_global_template_sync_response({"requestId": request_id, "templates": []})
    await asyncio.wait_for(task, 2)
    assert len(socket.of("request_global_template_sync")) == 2
    assert socket.of("request_template_sync") == []


def test_per_lawyer_events_are_no_longer_handled():
    from app.socket.events import event_handler as h

    assert "template_sync" not in h._handlers
    assert "template_sync_response" not in h._handlers
    assert "global_template_sync" in h._handlers
    assert "global_template_sync_response" in h._handlers


def test_user_message_request_carries_scope_and_version():
    payload = UserMessageRequestPayload(**{
        "chat_type": "DRAFTING_ASSISTANT", "workspaceId": "ws-fictional",
        "userId": LAWYER, "chatId": "chat-fictional", "content": "Draft it",
        "source": "template", "templateId": WRIT, "templateScope": "global",
        "templateVersion": 3, "templateUrl": "https://storage.example.invalid/signed",
        "timestamp": "2026-11-05T11:00:00.000Z",
    })
    assert payload.templateScope == "global" and payload.templateVersion == 3


async def test_drafting_fetches_an_unheld_global_version(env, monkeypatch):
    from app.langgraph.drafting_agent import drafting_agent

    _h, _socket, agent = env
    await _publish(template_store, WRIT, 4, file_hash="h4")
    fields = {"templateId": WRIT, "templateScope": "global", "templateVersion": 3,
              "templateUrl": "https://storage.example.invalid/signed",
              "templateOriginalName": "writ-petition.docx"}
    with version_pins():
        pin_template_version(WRIT, 3)
        # A suggested-draft click (no lazy fetch for lawyer templates) still
        # fetches a global version we do not hold: it is not a deleted template.
        resolved = await drafting_agent._resolve_template(
            user_id=LAWYER, request_fields=fields, allow_lazy_fetch=False
        )
        assert resolved["version"] == 3
        assert agent.ingests[-1]["scope"] == GLOBAL_SCOPE
        assert (await template_store.get(user_id=LAWYER, template_id=WRIT))["version"] == 3
    # v3 never displaced the current v4.
    assert (await template_store.get(user_id=LAWYER, template_id=WRIT))["version"] == 4


async def test_lawyer_template_click_still_never_resurrects_a_deleted_one(env):
    from app.langgraph.drafting_agent import drafting_agent

    _h, _socket, agent = env
    fields = {"templateId": OWN, "templateUrl": "https://storage.example.invalid/stale"}
    assert await drafting_agent._resolve_template(
        user_id=LAWYER, request_fields=fields, allow_lazy_fetch=False
    ) is None
    assert agent.ingests == []
