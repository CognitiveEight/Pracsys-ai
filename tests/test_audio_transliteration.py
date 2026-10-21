"""Hinglish transliteration over a collapsed, single-segment recording.

When diarization collapses a long call into one speaker, the whole recording is
one segment that the long-turn splitter cuts into ~134 pieces. All of them used
to go into one 8000-token call; the model returned no Part (MAX_TOKENS), and
``response.text`` raised ValueError out of the worker, discarding the finished
transcript. Fake model clients only: no network calls, fictional text.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import app.processing.audio_transcription as at

# ~550 chars of fictional Devanagari per sentence, so each sentence is one piece.
_SENTENCE = ("यह एक काल्पनिक वाक्य है " * 23).strip() + "।"


def _numbered_items(prompt: str) -> list[str]:
    body = prompt.split("Utterances:", 1)[1]
    return re.findall(r"^\d+\. (.*)$", body, flags=re.MULTILINE)


class _NoPartResponse:
    def __init__(self):
        self.candidates = [SimpleNamespace(finish_reason=2)]

    @property
    def text(self):
        raise ValueError("The `response.text` quick accessor requires ... finish_reason is 2.")


def test_response_text_without_a_part_is_empty_not_an_exception():
    assert at._response_text(_NoPartResponse()) == ""
    assert at._response_text(SimpleNamespace(text="ok")) == "ok"


def test_json_call_with_no_part_returns_none(monkeypatch):
    class _Model:
        def __init__(self, name):
            pass

        def generate_content(self, *a, **k):
            return _NoPartResponse()

    fake_genai = SimpleNamespace(
        GenerativeModel=_Model,
        types=SimpleNamespace(GenerationConfig=lambda **k: k),
    )
    monkeypatch.setattr(at, "_genai", fake_genai)
    monkeypatch.setattr(at, "_gemini_configured", lambda: True)
    monkeypatch.setattr(at, "_record_audio_gemini_usage", lambda r: None)
    assert at._generate_gemini_json_raw("prompt", max_output_tokens=8000) is None
    assert at._generate_gemini_plaintext_raw("prompt") is None


def test_one_long_segment_is_sent_in_small_groups(monkeypatch):
    sizes: list[int] = []

    def fake_json_raw(prompt, *, max_output_tokens=2048):
        items = _numbered_items(prompt)
        sizes.append(len(items))
        return json.dumps({"transliterated": ["yeh ek kalpanik vakya hai"] * len(items)})

    monkeypatch.setattr(at, "_generate_gemini_json_raw", fake_json_raw)
    text = " ".join([_SENTENCE] * 134)
    out = at._transliterate_one_batch_to_hinglish([text], batch_label=" P1 1/1 (size 1)")

    assert sizes and max(sizes) <= at._HINGLISH_BATCH_SIZE
    assert sum(sizes) == len(at._split_long_utterance_for_translit(text))
    assert out is not None and not at._has_devanagari(out[0])


def test_failed_group_keeps_its_devanagari_and_others_convert(monkeypatch):
    calls = {"n": 0}

    def fake_json_raw(prompt, *, max_output_tokens=2048):
        calls["n"] += 1
        if calls["n"] <= 2:  # first group: primary and terse retry both empty
            return None
        items = _numbered_items(prompt)
        return json.dumps({"transliterated": ["roman"] * len(items)})

    monkeypatch.setattr(at, "_generate_gemini_json_raw", fake_json_raw)
    text = " ".join([_SENTENCE] * 25)
    out = at._transliterate_one_batch_to_hinglish([text])

    assert out is not None
    assert at._has_devanagari(out[0])  # the failed group stays for the next pass
    assert "roman" in out[0]


def test_already_roman_pieces_are_not_resent(monkeypatch):
    sent: list[str] = []

    def fake_json_raw(prompt, *, max_output_tokens=2048):
        items = _numbered_items(prompt)
        sent.extend(items)
        return json.dumps({"transliterated": ["roman"] * len(items)})

    monkeypatch.setattr(at, "_generate_gemini_json_raw", fake_json_raw)
    roman = ("this part is already roman text " * 18).strip() + "."
    out = at._transliterate_one_batch_to_hinglish([f"{roman} {_SENTENCE} {roman}"])

    assert out is not None and not at._has_devanagari(out[0])
    assert all(at._has_devanagari(s) for s in sent)


def test_every_group_failing_returns_none(monkeypatch):
    monkeypatch.setattr(at, "_generate_gemini_json_raw", lambda p, **k: None)
    assert at._transliterate_one_batch_to_hinglish([" ".join([_SENTENCE] * 30)]) is None
