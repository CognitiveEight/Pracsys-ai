"""SIM-204: the template_fill export route (model call faked, fictional data)."""

import asyncio
import importlib
from io import BytesIO

from docx import Document

from app.processing.template_fidelity.corpus import VARIANTS, build_template
from app.processing.template_fidelity.docx_builder import build_docx


def _agent_cls():
    from app.langgraph.drafting_agent import DraftingAgent

    return DraftingAgent


def test_template_fill_applies_model_ops(monkeypatch):
    gc = importlib.import_module("app.ai.gemini_client")  # the module: app.ai re-exports the instance under that name

    tpl = build_docx(build_template("simple_will", VARIANTS[0]))
    calls = {}

    async def fake_longform(prompt, **kwargs):
        calls["task"] = kwargs.get("task")
        calls["prompt"] = prompt
        return ('{"op":"replace","id":"p1","text":"I, ASHA EXAMPLE, make this Will on 1 May 2027."}\n'
                '{"op":"delete","id":"p6"}')

    monkeypatch.setattr(gc.gemini_client, "generate_longform", fake_longform)
    out = asyncio.run(_agent_cls()._template_map_fill("# LAST WILL AND TESTAMENT\n\nI, ASHA ...", tpl))
    assert out is not None
    assert calls["task"] == "template_fill"
    assert "TEMPLATE MAP:" in calls["prompt"] and "p1 " in calls["prompt"]
    texts = [p.text for p in Document(BytesIO(out)).paragraphs]
    assert "I, ASHA EXAMPLE, make this Will on 1 May 2027." in texts


def test_template_fill_falls_back_on_model_error(monkeypatch):
    gc = importlib.import_module("app.ai.gemini_client")  # the module: app.ai re-exports the instance under that name

    tpl = build_docx(build_template("simple_will", VARIANTS[0]))

    async def failing(prompt, **kwargs):
        return "Error generating response: quota"

    monkeypatch.setattr(gc.gemini_client, "generate_longform", failing)
    assert asyncio.run(_agent_cls()._template_map_fill("# WILL", tpl)) is None


def test_template_fill_falls_back_when_nothing_applies(monkeypatch):
    gc = importlib.import_module("app.ai.gemini_client")  # the module: app.ai re-exports the instance under that name

    tpl = build_docx(build_template("simple_will", VARIANTS[0]))

    async def unknown_ids(prompt, **kwargs):
        return '{"op":"replace","id":"p999","text":"x"}'

    monkeypatch.setattr(gc.gemini_client, "generate_longform", unknown_ids)
    assert asyncio.run(_agent_cls()._template_map_fill("# WILL", tpl)) is None
