"""SIM-219: a drafting revision starts from the backend's latest .docx.

The lawyer edits the draft in the editor; the backend stores that as a new
version and sends its link (``baseDocumentUrl``) with the next revision turn.
The agent must revise THAT file, refuse (with no model call) when the file
has open tracked changes / comments or cannot be read, and behave exactly as
before when no link is sent. The download and the model are stubbed; the
scan and the .docx -> Markdown conversion are real. Fictional data only.
"""

import io
import json
import zipfile

import httpx
import pytest
from docx import Document

import app.processing.draft_revision_base as base_module
from app.ai.gemini_client import gemini_client
from app.langgraph.drafting_agent import drafting_agent
from app.processing.docx_generator import render_markdown_to_docx
from app.processing.docx_markdown import docx_to_draft_markdown

LAWYER_SENTENCE = "The sender reserves the right to claim interest at eighteen percent per annum."
AI_ONLY_SENTENCE = "This paragraph was in the assistant's own earlier copy only."
FAKE_URL = "https://storage.example.invalid/drafts/ws_fictional/v3.docx?sig=fictional"

AI_COPY = f"""# LEGAL NOTICE

## Facts

{AI_ONLY_SENTENCE}

| Item | Amount |
| --- | --- |
| Principal | 1,00,000 |
"""

LAWYER_VERSION = f"""# LEGAL NOTICE

## Facts

The noticee bought fictional goods on credit.

{LAWYER_SENTENCE}

| Item | Amount |
| --- | --- |
| Principal | 1,00,000 |
| Interest | 18,000 |
"""


def _lawyer_docx(markdown: str = LAWYER_VERSION) -> bytes:
    return render_markdown_to_docx(markdown, template_docx_bytes=None, title="Legal Notice")


def _with_tracked_change(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parts = {n: zf.read(n) for n in zf.namelist()}
    doc = parts["word/document.xml"].decode("utf-8")
    idx = doc.index("<w:sectPr")
    change = (
        '<w:p><w:ins w:id="1" w:author="Fictional Editor" w:date="2026-11-01T10:00:00Z">'
        "<w:r><w:t>pending words</w:t></w:r></w:ins></w:p>"
    )
    parts["word/document.xml"] = (doc[:idx] + change + doc[idx:]).encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            zf.writestr(name, body)
    return out.getvalue()


def _session():
    return {
        "draft_status": "ready",
        "draft_title": "Legal Notice",
        "source": "blank",
        "current_draft_version": 2,
        "last_draft_markdown": AI_COPY,
        "last_draft_document": {
            "title": "Legal Notice",
            "format": "markdown",
            "content": AI_COPY,
            "version": 2,
        },
    }


class _Fakes:
    def __init__(self):
        self.router_prompts = []
        self.render_prompts = []
        self.downloads = []
        self.saved = []


@pytest.fixture
def fakes(monkeypatch):
    f = _Fakes()
    session = _session()

    async def _load(**_kw):
        return session

    async def _save(*_a, **kw):
        f.saved.append(kw.get("session_state"))

    async def _router(prompt="", *_a, **_kw):
        f.router_prompts.append(prompt)
        return json.dumps(
            {
                "action": "revise_draft",
                "params": {"revision_instruction": "Add a demand paragraph."},
                "assistant_message": "",
                "reasoning": "edit request",
            }
        )

    async def _render(prompt="", *_a, **_kw):
        f.render_prompts.append(prompt)
        # The fake model keeps whatever previous draft it was given.
        kept = LAWYER_SENTENCE if LAWYER_SENTENCE in prompt else AI_ONLY_SENTENCE
        return f"# LEGAL NOTICE\n\n## Facts\n\n{kept}\n\n## Demand\n\nPay within fifteen days.\n"

    async def _no_export(**_kw):
        return None

    monkeypatch.setattr(drafting_agent, "_load_session_state", _load)
    monkeypatch.setattr(drafting_agent, "_save_session_state", _save)
    monkeypatch.setattr(drafting_agent, "_export_draft_docx", _no_export)
    monkeypatch.setattr(gemini_client, "generate", _router)
    monkeypatch.setattr(gemini_client, "generate_longform", _render)
    f.session = session
    return f


def _stub_download(monkeypatch, fakes, *, data=None, error=None):
    async def _fetch(url, **_kw):
        fakes.downloads.append(url)
        if error is not None:
            raise error
        return data

    monkeypatch.setattr(base_module, "fetch_base_docx", _fetch)


async def _turn(**extra):
    request = {
        "actionableId": "draft_session_fictional",
        "workspaceId": "ws_fictional",
        "userId": "user_fictional",
    }
    request.update(extra)
    return await drafting_agent.handle_turn(
        user_message="Add a demand paragraph asking for payment in fifteen days.",
        case_context={"role": "PETITIONER"},
        request_fields=request,
        conversation_history=[],
    )


# --------------------------------------------------------------- conversion


def test_conversion_keeps_headings_tables_and_text():
    md = docx_to_draft_markdown(_lawyer_docx())
    assert "# LEGAL NOTICE" in md
    assert "## Facts" in md
    assert LAWYER_SENTENCE in md
    assert "| Item | Amount |" in md
    assert "| --- | --- |" in md
    assert "| Interest | 18,000 |" in md


def test_conversion_keeps_lists_and_emphasis():
    md = docx_to_draft_markdown(
        _lawyer_docx("# NOTICE\n\nThe **noticee** must pay.\n\n1. First ground.\n2. Second ground.\n\n- one\n- two\n")
    )
    assert "The **noticee** must pay." in md
    assert "1. First ground." in md and "2. Second ground." in md
    assert "- one\n- two" in md


def test_conversion_of_a_word_saved_file():
    doc = Document()
    doc.add_heading("Reply to Notice", level=1)
    para = doc.add_paragraph("Received on ")
    para.add_run("a fictional date").bold = True
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Party", "Role"
    table.cell(1, 0).text, table.cell(1, 1).text = "Fictional Traders | Pvt", "Sender"
    out = io.BytesIO()
    doc.save(out)
    md = docx_to_draft_markdown(out.getvalue())
    assert "# Reply to Notice" in md
    assert "Received on **a fictional date**" in md
    assert "| Party | Role |" in md
    assert "| Fictional Traders \\| Pvt | Sender |" in md


# ------------------------------------------------------------ drafting path


async def test_clean_base_is_revised_from_the_lawyers_version(monkeypatch, fakes):
    _stub_download(monkeypatch, fakes, data=_lawyer_docx())
    turn = await _turn(draftId="draft_fictional_1", baseVersion=3, baseDocumentUrl=FAKE_URL)

    assert fakes.downloads == [FAKE_URL]
    assert len(fakes.render_prompts) == 1
    assert LAWYER_SENTENCE in fakes.render_prompts[0]
    assert AI_ONLY_SENTENCE not in fakes.render_prompts[0]

    doc = turn["draftDocument"]
    assert turn["isDraft"] is True
    assert "draftRefusal" not in turn
    assert LAWYER_SENTENCE in doc["content"]
    assert doc["draftId"] == "draft_fictional_1"
    assert doc["baseVersion"] == 3
    assert doc["version"] == 4  # vN+1 of the backend's version
    # The URL is never kept in the session.
    assert fakes.saved and FAKE_URL not in json.dumps(fakes.saved[-1], default=str)


async def test_open_changes_refuse_without_a_model_call(monkeypatch, fakes):
    _stub_download(monkeypatch, fakes, data=_with_tracked_change(_lawyer_docx()))
    turn = await _turn(draftId="draft_fictional_1", baseVersion=3, baseDocumentUrl=FAKE_URL)

    assert fakes.router_prompts == [] and fakes.render_prompts == []
    assert turn["draftRefusal"] == {"code": "open_changes", "trackedChanges": 1, "comments": 0}
    assert turn["draftDocument"] is None
    assert turn["draftStatus"] == "ready"
    assert turn["isDraft"] is False
    assert "tracked changes or comments" in turn["content"]
    assert fakes.saved == []
    assert fakes.session["last_draft_markdown"] == AI_COPY


async def test_unreadable_base_refuses_without_a_model_call(monkeypatch, fakes):
    _stub_download(monkeypatch, fakes, data=b"not a word file")
    turn = await _turn(baseVersion=3, baseDocumentUrl=FAKE_URL)

    assert fakes.router_prompts == [] and fakes.render_prompts == []
    assert turn["draftRefusal"] == {"code": "base_unreadable"}
    assert turn["draftDocument"] is None
    assert turn["draftStatus"] == "ready"


async def test_failed_download_refuses_as_unreadable(monkeypatch, fakes):
    _stub_download(monkeypatch, fakes, error=base_module.BaseUnreadable("http_403"))
    turn = await _turn(baseVersion=3, baseDocumentUrl=FAKE_URL)
    assert turn["draftRefusal"] == {"code": "base_unreadable"}
    assert fakes.render_prompts == []


async def test_without_base_url_the_turn_is_unchanged(monkeypatch, fakes):
    _stub_download(monkeypatch, fakes, data=_lawyer_docx())
    turn = await _turn()

    assert fakes.downloads == []
    assert AI_ONLY_SENTENCE in fakes.render_prompts[0]
    doc = turn["draftDocument"]
    assert "draftId" not in doc and "baseVersion" not in doc
    assert doc["version"] == 3
    assert "draftRefusal" not in turn


async def test_draft_id_is_echoed_without_a_base(monkeypatch, fakes):
    _stub_download(monkeypatch, fakes, data=_lawyer_docx())
    turn = await _turn(draftId="draft_fictional_1")
    assert turn["draftDocument"]["draftId"] == "draft_fictional_1"
    assert "baseVersion" not in turn["draftDocument"]
    # The persisted record is the AI's own; the echo is on a copy.
    assert "draftId" not in (fakes.session.get("last_draft_document") or {})


# ------------------------------------------------------------------ download


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


async def test_download_returns_bytes(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(200, content=b"PK-fictional"))
    data = await base_module.fetch_base_docx(FAKE_URL, timeout_seconds=5, max_bytes=1024)
    assert data == b"PK-fictional"


async def test_download_error_status_is_unreadable(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(403, content=b"expired"))
    with pytest.raises(base_module.BaseUnreadable) as err:
        await base_module.fetch_base_docx(FAKE_URL, timeout_seconds=5, max_bytes=1024)
    assert err.value.reason == "http_403"


async def test_download_over_the_size_cap_is_unreadable(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(base_module.BaseUnreadable) as err:
        await base_module.fetch_base_docx(FAKE_URL, timeout_seconds=5, max_bytes=1024)
    assert err.value.reason == "too_large"


async def test_download_rejects_a_non_http_link():
    with pytest.raises(base_module.BaseUnreadable) as err:
        await base_module.fetch_base_docx("file:///etc/hosts", timeout_seconds=5, max_bytes=1024)
    assert err.value.reason == "bad_url"
