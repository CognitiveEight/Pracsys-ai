"""SIM-220: the drafting revision path with tracked changes.

The download, the storage upload and the model are stubbed; every model
method the turn is not expected to call raises, so nothing reaches the
network. The scan, conversion, post-processing and the tracked-change writer
are real. Fictional data only.
"""

import io
import json
import zipfile

import pytest
from lxml import etree

import app.processing.docx_track_changes as track_module
import app.processing.draft_revision_base as base_module
import app.storage.draft_files as draft_files
from app.ai.gemini_client import gemini_client
from app.core.config import settings
from app.langgraph import drafting_agent as agent_module
from app.langgraph.drafting_agent import (
    TRACKED_FALLBACK_NOTICE,
    TRACKED_REVISION_VERBATIM_INSTRUCTION,
    drafting_agent,
)
from app.processing.docx_generator import render_markdown_to_docx
from app.processing.docx_markdown import docx_to_draft_markdown
from app.processing.draft_markdown import normalize_if_enabled
from app.socket.schemas import DraftDocumentPayload

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FAKE_URL = "https://storage.example.invalid/drafts/ws_fictional/v3.docx?sig=fictional"
TRACKED_LINK = "https://files.example.invalid/tracked.docx"
CLEAN_LINK = "https://files.example.invalid/clean.docx"

# The table's Total is wrong on purpose: the SIM-209 check adds a
# "<check total: ...>" marker to the delivered draft, which must not show up
# as a tracked change.
LAWYER_VERSION = """# LEGAL NOTICE

## Facts

The noticee bought fictional goods on credit from the sender.

The noticee has not paid the amount due despite reminders.

| Item | Amount |
| --- | --- |
| Principal | 1,00,000 |
| Interest | 18,000 |
| Total | 1,10,000 |

## Demand

Pay the amount within fifteen days of this notice.

Any further delay will be met with fictional proceedings.
"""

FULL_REWRITE = """# AGREEMENT FOR SALE

This fictional agreement is made between two invented companies.

Both parties agree to completely different obligations here.

The price is paid in three instalments over six months.
"""


def _base_docx():
    return render_markdown_to_docx(LAWYER_VERSION, template_docx_bytes=None, title="Legal Notice")


def _adopted(base):
    return normalize_if_enabled(docx_to_draft_markdown(base)).strip()


def _session(base_md):
    return {
        "draft_status": "ready",
        "draft_title": "Legal Notice",
        "source": "blank",
        "current_draft_version": 2,
        "last_draft_markdown": "An older AI copy.",
        "last_draft_document": {"title": "Legal Notice", "format": "markdown",
                                "content": base_md, "version": 2},
        "workspace_id": "ws_fictional",
        "actionable_id": "draft_session_fictional",
    }


class _Fakes:
    def __init__(self):
        self.render_prompts = []
        self.uploads = []
        self.clean_exports = 0
        self.revised = ""


def _unexpected(name):
    async def _raise(*_a, **_kw):
        raise AssertionError(f"unexpected model call: {name}")

    def _raise_sync(*_a, **_kw):
        raise AssertionError(f"unexpected model call: {name}")

    return _raise_sync if name == "generate_sync" else _raise


@pytest.fixture
def fakes(monkeypatch):
    f = _Fakes()
    base = _base_docx()
    f.base = base
    f.base_md = _adopted(base)
    f.revised = f.base_md.replace("fifteen days", "thirty days")
    session = _session(f.base_md)

    async def _load(**_kw):
        return session

    async def _save(*_a, **_kw):
        return None

    async def _router(prompt="", *_a, **_kw):
        return json.dumps({"action": "revise_draft",
                           "params": {"revision_instruction": "Give thirty days."},
                           "assistant_message": "", "reasoning": "edit request"})

    async def _render(prompt="", *_a, **_kw):
        f.render_prompts.append(prompt)
        return f.revised

    async def _clean_export(**_kw):
        f.clean_exports += 1
        return CLEAN_LINK

    def _upload(workspace_id, data):
        f.uploads.append(data)
        return TRACKED_LINK

    async def _fetch(url, **_kw):
        return base

    for name in ("chat", "embed_query", "embed_text", "embed_texts", "generate_grounded",
                 "generate_sync", "generate_with_images"):
        monkeypatch.setattr(gemini_client, name, _unexpected(name))
    monkeypatch.setattr(gemini_client, "generate", _router)
    monkeypatch.setattr(gemini_client, "generate_longform", _render)
    monkeypatch.setattr(drafting_agent, "_load_session_state", _load)
    monkeypatch.setattr(drafting_agent, "_save_session_state", _save)
    monkeypatch.setattr(drafting_agent, "_export_draft_docx", _clean_export)
    monkeypatch.setattr(draft_files, "upload_draft_docx", _upload)
    monkeypatch.setattr(draft_files, "draft_bucket", lambda: "bucket-fictional")
    monkeypatch.setattr(base_module, "fetch_base_docx", _fetch)
    monkeypatch.setattr(settings, "draft_revisions_as_tracked_changes", True)
    monkeypatch.setattr(settings, "draft_tracked_changes_max_changed_ratio", 0.4)
    return f


async def _turn(with_base=True):
    request = {"actionableId": "draft_session_fictional", "workspaceId": "ws_fictional",
               "userId": "user_fictional", "draftId": "draft_fictional"}
    if with_base:
        request.update({"baseDocumentUrl": FAKE_URL, "baseVersion": 3})
    return await drafting_agent.handle_turn(
        user_message="Give thirty days instead of fifteen.",
        case_context={"role": "PETITIONER"},
        request_fields=request,
        conversation_history=[],
    )


def _changes(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    return [el for el in root.iter(f"{{{W_NS}}}ins", f"{{{W_NS}}}del")]


def _text(el):
    return "".join(t.text or "" for t in el.iter(f"{{{W_NS}}}t", f"{{{W_NS}}}delText"))


async def test_setting_on_uploads_tracked_changes(fakes):
    result = await _turn()
    doc = result["draftDocument"]
    assert doc["downloadUrl"] == TRACKED_LINK and doc["trackedChanges"] is True
    assert fakes.clean_exports == 0 and len(fakes.uploads) == 1
    # Only the one word: the totals marker the delivery path adds is in the
    # base used for the diff too, so it is not an AI change.
    assert "<check total:" in doc["content"]
    assert sorted(_text(c) for c in _changes(fakes.uploads[0])) == ["fifteen", "thirty"]
    assert doc["baseVersion"] == 3 and doc["version"] == 4
    assert TRACKED_FALLBACK_NOTICE not in result["content"]
    DraftDocumentPayload(**doc)  # the payload validates with the new field
    assert agent_module._TURN_REVISION_BASE.get() is None  # turn-local only


async def test_verbatim_instruction_only_on_the_tracked_path(fakes, monkeypatch):
    await _turn()
    assert TRACKED_REVISION_VERBATIM_INSTRUCTION in fakes.render_prompts[-1]

    monkeypatch.setattr(settings, "draft_revisions_as_tracked_changes", False)
    await _turn()
    assert TRACKED_REVISION_VERBATIM_INSTRUCTION not in fakes.render_prompts[-1]

    monkeypatch.setattr(settings, "draft_revisions_as_tracked_changes", True)
    await _turn(with_base=False)
    assert TRACKED_REVISION_VERBATIM_INSTRUCTION not in fakes.render_prompts[-1]


async def test_setting_off_is_the_sim219_clean_file(fakes, monkeypatch):
    monkeypatch.setattr(settings, "draft_revisions_as_tracked_changes", False)
    result = await _turn()
    doc = result["draftDocument"]
    assert doc["downloadUrl"] == CLEAN_LINK and "trackedChanges" not in doc
    assert fakes.clean_exports == 1 and fakes.uploads == []
    assert TRACKED_FALLBACK_NOTICE not in result["content"]


async def test_tracked_step_raising_falls_back_to_the_clean_file(fakes, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("fictional failure")

    monkeypatch.setattr(track_module, "build_tracked_revision", _boom)
    result = await _turn()
    doc = result["draftDocument"]
    assert doc["downloadUrl"] == CLEAN_LINK and not doc.get("trackedChanges")
    assert fakes.uploads == [] and fakes.clean_exports == 1
    assert TRACKED_FALLBACK_NOTICE not in result["content"]  # technical: log only


async def test_tracked_step_returning_no_file_falls_back(fakes, monkeypatch):
    monkeypatch.setattr(
        track_module, "build_tracked_revision",
        lambda *_a, **_kw: track_module.TrackedResult(None, "unsupported_structure"),
    )
    result = await _turn()
    assert result["draftDocument"]["downloadUrl"] == CLEAN_LINK
    assert not result["draftDocument"].get("trackedChanges")
    assert TRACKED_FALLBACK_NOTICE not in result["content"]


async def test_full_rewrite_comes_as_a_clean_file_with_a_notice(fakes):
    fakes.revised = FULL_REWRITE
    result = await _turn()
    doc = result["draftDocument"]
    assert doc["downloadUrl"] == CLEAN_LINK and not doc.get("trackedChanges")
    assert fakes.uploads == [] and fakes.clean_exports == 1
    assert result["content"].rstrip().endswith(TRACKED_FALLBACK_NOTICE)
    assert TRACKED_FALLBACK_NOTICE not in doc["content"]


async def test_open_changes_message_mentions_the_ais_own_edits():
    assert "including any changes suggested by the AI" in base_module.OPEN_CHANGES_MESSAGE


# ------------------------------------------------- stricter checks (tester)


def _ratio_of(fakes, revised):
    """The changed ratio the turn will compute for ``revised`` (both sides
    through the same post-processing the turn applies)."""
    def post(md):
        return agent_module._check_totals(normalize_if_enabled(md))

    res = track_module.build_tracked_revision(
        fakes.base, post(fakes.base_md), post(revised), max_changed_ratio=1.0
    )
    assert res.applied and res.changed_ratio > 0
    return res.changed_ratio


async def test_ratio_setting_on_both_sides_of_the_threshold_on_the_turn(fakes, monkeypatch):
    # AC-12 through the turn: the setting (not the writer's default) decides.
    fakes.revised = fakes.base_md.replace("fifteen days", "thirty days").replace(
        "fictional proceedings", "fictional civil proceedings"
    )
    ratio = _ratio_of(fakes, fakes.revised)

    monkeypatch.setattr(settings, "draft_tracked_changes_max_changed_ratio", ratio)
    at = await _turn()
    assert at["draftDocument"]["downloadUrl"] == TRACKED_LINK
    assert at["draftDocument"]["trackedChanges"] is True
    assert TRACKED_FALLBACK_NOTICE not in at["content"]

    monkeypatch.setattr(settings, "draft_tracked_changes_max_changed_ratio", ratio - 0.01)
    over = await _turn()
    assert over["draftDocument"]["downloadUrl"] == CLEAN_LINK
    assert not over["draftDocument"].get("trackedChanges")
    assert over["content"].rstrip().endswith(TRACKED_FALLBACK_NOTICE)
    assert len(fakes.uploads) == 1 and fakes.clean_exports == 1


async def test_no_bucket_and_upload_failure_fall_back_without_notice(fakes, monkeypatch):
    # AC-17 (technical fallbacks): clean file, no trackedChanges, no notice.
    monkeypatch.setattr(draft_files, "draft_bucket", lambda: "")
    result = await _turn()
    assert result["draftDocument"]["downloadUrl"] == CLEAN_LINK
    assert not result["draftDocument"].get("trackedChanges")
    assert TRACKED_FALLBACK_NOTICE not in result["content"]
    assert fakes.uploads == []

    monkeypatch.setattr(draft_files, "draft_bucket", lambda: "bucket-fictional")

    def _upload_fails(*_a, **_kw):
        raise RuntimeError("fictional upload failure")

    monkeypatch.setattr(draft_files, "upload_draft_docx", _upload_fails)
    result = await _turn()
    assert result["draftDocument"]["downloadUrl"] == CLEAN_LINK
    assert not result["draftDocument"].get("trackedChanges")
    assert TRACKED_FALLBACK_NOTICE not in result["content"]
    assert fakes.clean_exports == 2


def _holds_bytes_or(value, needle):
    if isinstance(value, (bytes, bytearray)):
        return True
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_holds_bytes_or(v, needle) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_holds_bytes_or(v, needle) for v in value)
    return False


async def test_saved_session_never_holds_the_base_file(fakes, monkeypatch):
    # AC-14 ("base not kept after the turn"): nothing the turn saves carries
    # the file's bytes or the backend's base URL.
    saved = []

    async def _record(*args, **kwargs):
        saved.append((args, kwargs))

    monkeypatch.setattr(drafting_agent, "_save_session_state", _record)
    result = await _turn()
    assert result["draftDocument"]["trackedChanges"] is True
    assert saved, "the turn saves its session"
    assert not _holds_bytes_or(saved, FAKE_URL)
    assert not _holds_bytes_or(result, FAKE_URL)


async def test_next_turn_on_the_tracked_file_is_refused_without_a_model_call(fakes, monkeypatch):
    # AC-9 through the turn: the lawyer's next request, sent with the file
    # this service wrote as its base, is refused as open changes before any
    # model call.
    first = await _turn()
    assert first["draftDocument"]["trackedChanges"] is True
    tracked_file = fakes.uploads[0]

    async def _fetch_tracked(url, **_kw):
        return tracked_file

    monkeypatch.setattr(base_module, "fetch_base_docx", _fetch_tracked)
    monkeypatch.setattr(gemini_client, "generate", _unexpected("generate"))
    monkeypatch.setattr(gemini_client, "generate_longform", _unexpected("generate_longform"))
    second = await _turn()
    assert second["draftRefusal"]["code"] == base_module.OUTCOME_OPEN_CHANGES
    assert second["draftRefusal"]["trackedChanges"] == 2
    assert second["draftDocument"] is None
    assert second["content"] == base_module.OPEN_CHANGES_MESSAGE
