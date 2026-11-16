#!/usr/bin/env python3
"""
verify_multi_speaker_diarization.py — Offline checks for SIM-177.

Audio transcripts separate 1 to 8 speakers instead of forcing every recording
into a two-person call. The rules under test:

  * reliable native tags with 3+ speakers are kept; the Gemini text pass may
    only correct turns, never change the count (one to one, at most 25%);
  * two native speakers keep today's 2-speaker call pass (Caller / Receiver);
  * one native speaker, or drifting chunked tags, go through the N-speaker pass;
  * long recordings keep one label per person across Gemini blocks;
  * near-silent false speakers merge into a neighbour (3+ only, never below 2),
    and only a result above the maximum collapses to one speaker;
  * 3+ speakers are always neutral Speaker A, B, C... labels;
  * a refused speaker range is retried once at the lower maximum, unbilled;
  * the payload shape and the placeholder rules cover Speaker C to H.

Fake Chirp results, fake Speech client, fake Gemini. No network, no billed
calls, fictional text only.

USAGE
    python tests/offline/verify_multi_speaker_diarization.py            # run everything
    python tests/offline/verify_multi_speaker_diarization.py --list     # show the check names
"""

import argparse
import os
import sys
import tempfile
import threading
import wave
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

import app.processing.audio_transcription as at  # noqa: E402
from app.processing.audio_transcription import (  # noqa: E402
    RecognizedWord,
    SpeakerSegment,
    TranscriptResult,
)

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))


class Patch:
    """Set module attributes for one check and restore them afterwards."""

    def __init__(self, **attrs):
        self._attrs = attrs
        self._saved: dict = {}

    def __enter__(self):
        for name, value in self._attrs.items():
            target, attr = (at.settings, name[9:]) if name.startswith("settings_") else (at, name)
            self._saved[name] = (target, attr, getattr(target, attr))
            setattr(target, attr, value)
        return self

    def __exit__(self, *exc):
        for target, attr, value in self._saved.values():
            setattr(target, attr, value)
        return False


# ── Fixtures ──────────────────────────────────────────────────────────────────
def _turns_to_result(turns: list[tuple[int, str]], *, reliable=True, max_used=None) -> TranscriptResult:
    """One native turn per (tag, text); 2 s silence between turns keeps them apart."""
    words: list[RecognizedWord] = []
    t = 0.0
    for tag, text in turns:
        for w in text.split():
            words.append(RecognizedWord(speaker_tag=tag, word=w, start_time=t, end_time=t + 0.3))
            t += 0.4
        t += 2.0
    return TranscriptResult(
        raw_transcript=at._render_text_from_words(words),
        detected_languages=["en-in"],
        words=words,
        timed_turns=at._build_timed_turns(words),
        native_tags_reliable=reliable,
        max_speakers_used=max_used,
    )


def _voice(n: int, i: int, words: int = 6) -> str:
    """Fictional utterance carrying its true speaker as the token 'vN'."""
    return f"v{n} " + " ".join(f"item{i}w{k}" for k in range(words - 1))


def _truth_of(utterance: str) -> int:
    return int(utterance.split()[0][1:])


def _first_appearance(tags: list[int]) -> dict[int, int]:
    order: dict[int, int] = {}
    for t in tags:
        order.setdefault(t, len(order) + 1)
    return order


def fake_multi_truth(utterances, *, speaker_count=None, max_speakers=8):
    """N-speaker fake: the true speakers, numbered by first appearance."""
    truth = [_truth_of(u) for u in utterances]
    order = _first_appearance(truth)
    return {i: order[t] for i, t in enumerate(truth)}


def fake_two_truth(utterances):
    truth = [_truth_of(u) for u in utterances]
    return {i: t for i, t in enumerate(truth)}


def _no_roles(segments):
    return None


def _roles_by_first_two(segments):
    tags = sorted({s.speaker_tag for s in segments})
    return at.RoleInference(
        role_by_speaker={tags[0]: "lawyer", tags[1]: "victim_party"},
        confidence_by_speaker={tags[0]: 90, tags[1]: 90},
        evidence_by_speaker={},
        detected_roles=["lawyer", "victim_party"],
    )


_AUDIO = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
_AUDIO.close()


def _transcribe(result: TranscriptResult, *, max_speakers=8, **patches):
    """Run _sync_transcribe over a fixed native result with fakes."""
    base = dict(
        _SPEECH_AVAILABLE=True,
        _probe_duration_seconds=lambda path: None,
        _transcribe_offline_with_diarization=lambda **k: result,
        _transliterate_segments_to_hinglish=lambda segs: segs,
        _gemini_diarization_assignments=lambda u: None,
        _gemini_multi_speaker_assignments=lambda u, **k: None,
        _infer_speaker_roles_with_gemini=_no_roles,
    )
    base.update(patches)
    with Patch(**base):
        return at._sync_transcribe(
            _AUDIO.name,
            languages=["en-IN"],
            min_speakers=1,
            max_speakers=max_speakers,
            chirp_region="us",
            enable_denoiser=False,
            enable_role_inference=True,
        )


def _labels(out) -> list[str]:
    seen: list[str] = []
    for line in out.transcript.splitlines():
        label = line.split("] ", 1)[1].split(":", 1)[0]
        if label not in seen:
            seen.append(label)
    return seen


def _rotating_turns(n: int, rounds: int = 3) -> list[tuple[int, str]]:
    turns, i = [], 0
    for _ in range(rounds):
        for spk in range(1, n + 1):
            turns.append((spk, _voice(spk, i)))
            i += 1
    return turns


# ── Groups ────────────────────────────────────────────────────────────────────
def check_speakers_1():
    note = _turns_to_result([(1, _voice(1, i, 12)) for i in range(5)])
    out = _transcribe(note, _gemini_multi_speaker_assignments=lambda u, **k: {i: 1 for i in range(len(u))})
    check("speakers_1: voice note stays one speaker", out.success and _labels(out) == ["Speaker A"],
          str(_labels(out)))
    check("speakers_1: source unchanged", out.speaker_source == "diarization", out.speaker_source)

    # AC-4: native 1, N-speaker pass finds 2 real speakers -> role inference.
    turns = [(1, _voice(1 + i % 2, i, 10)) for i in range(6)]
    native_one = _turns_to_result(turns)
    out = _transcribe(native_one, _gemini_multi_speaker_assignments=fake_multi_truth,
                      _infer_speaker_roles_with_gemini=_roles_by_first_two)
    check("speakers_1: split to 2 with roles", sorted(_labels(out)) == ["Caller", "Receiver"],
          str(_labels(out)))
    check("speakers_1: split source", out.speaker_source == "gemini_multi_diarization", out.speaker_source)
    out = _transcribe(native_one, _gemini_multi_speaker_assignments=fake_multi_truth)
    check("speakers_1: split to 2, roles failed -> neutral letters",
          _labels(out) == ["Speaker A", "Speaker B"] or sorted(_labels(out)) == ["Caller", "Receiver"],
          str(_labels(out)))


def check_speakers_2():
    calls: list[int] = []

    def two(u):
        calls.append(len(u))
        return fake_two_truth(u)

    res = _turns_to_result(_rotating_turns(2))
    out = _transcribe(res, _gemini_diarization_assignments=two,
                      _gemini_multi_speaker_assignments=lambda u, **k: (_ for _ in ()).throw(
                          AssertionError("N-speaker pass must not run for 2 speakers")))
    check("speakers_2: 2-speaker pass ran", len(calls) == 1)
    check("speakers_2: Caller / Receiver", sorted(_labels(out)) == ["Caller", "Receiver"], str(_labels(out)))
    check("speakers_2: source gemini_diarization", out.speaker_source == "gemini_diarization")


def check_speakers_3_5_8():
    for n in (3, 5, 8):
        multi_calls: list[int] = []

        def multi(u, *, speaker_count=None, max_speakers=8):
            multi_calls.append(speaker_count or 0)
            return fake_multi_truth(u)

        out = _transcribe(_turns_to_result(_rotating_turns(n)),
                          _gemini_multi_speaker_assignments=multi,
                          _infer_speaker_roles_with_gemini=_roles_by_first_two)
        expected = [f"Speaker {chr(ord('A') + k)}" for k in range(n)]
        check(f"speakers_{n}: {n} neutral labels in first-appearance order",
              out.success and _labels(out) == expected, str(_labels(out)))
        check(f"speakers_{n}: no Caller / Receiver",
              "Caller" not in out.transcript and "Receiver" not in out.transcript)
        check(f"speakers_{n}: correction pass asked for exactly {n}", multi_calls == [n], str(multi_calls))
        check(f"speakers_{n}: source unchanged, no collapse", out.speaker_source == "diarization",
              out.speaker_source)
        check(f"speakers_{n}: segment labels match", {s["speaker_label"] for s in out.segments} == set(expected))


def check_correction():
    turns = _rotating_turns(4)
    # AC-5: Gemini answers 2 for a reliable 4 -> native 4 kept.
    out = _transcribe(_turns_to_result(turns),
                      _gemini_multi_speaker_assignments=lambda u, **k: {i: 1 + i % 2 for i in range(len(u))})
    check("correction: Gemini 2 for native 4 keeps 4", len(_labels(out)) == 4, str(_labels(out)))

    # AC-6: native has one slip (turn 5 is really voice 2, tagged 3); Gemini
    # numbers speakers differently (permuted), fixes the slip.
    slipped = list(turns)
    slipped[5] = (3, _voice(2, 5))  # native tag 3, true voice 2
    perm = {1: 3, 2: 1, 3: 4, 4: 2}

    def permuted(u, *, speaker_count=None, max_speakers=8):
        return {i: perm[_truth_of(x)] for i, x in enumerate(u)}

    res = _turns_to_result(slipped)
    native_segments = at._merge_consecutive_same_speaker(at._build_speaker_segments(res))
    with Patch(_gemini_multi_speaker_assignments=permuted):
        corrected = at._correct_native_tags(native_segments, 4, 8)
    ok_tags = corrected is not None and all(
        s.speaker_tag == _truth_of(s.text) for s in corrected
    )
    check("correction: slip fixed onto native tags", ok_tags)
    check("correction: count stays 4", corrected is not None and len({s.speaker_tag for s in corrected}) == 4)
    out = _transcribe(res, _gemini_multi_speaker_assignments=permuted)
    check("correction: letters keep native first-appearance order",
          _labels(out) == ["Speaker A", "Speaker B", "Speaker C", "Speaker D"], str(_labels(out)))
    line6 = out.transcript.splitlines()[5]
    check("correction: slipped turn now labelled as voice 2", "Speaker B:" in line6)

    # AC-7: more than 25% reassigned -> native stands.
    def reshuffle(u, *, speaker_count=None, max_speakers=8):
        # Truth for the first half, then a rotated mapping: half the turns change.
        half = len(u) // 2
        out_map = {}
        for i, x in enumerate(u):
            t = _truth_of(x)
            out_map[i] = t if i < half else (t % 4) + 1
        return out_map

    with Patch(_gemini_multi_speaker_assignments=reshuffle):
        kept = at._correct_native_tags(native_segments, 4, 8)
    check("correction: >25% change rejected", kept is None)


def check_gemini_failure():
    from app.ai.gemini_client import gemini_client

    sentinel = "Error generating response: quota exhausted"
    base = dict(
        _SPEECH_AVAILABLE=True,
        _probe_duration_seconds=lambda path: None,
        _transliterate_segments_to_hinglish=lambda segs: segs,
        _infer_speaker_roles_with_gemini=_no_roles,
        _gemini_configured=lambda: True,
    )
    for n in (1, 2, 4):
        res = _turns_to_result(_rotating_turns(n))
        orig = gemini_client.generate_sync
        gemini_client.generate_sync = lambda prompt, **k: sentinel
        try:
            with Patch(**base, _transcribe_offline_with_diarization=lambda **k: res):
                out = at._sync_transcribe(_AUDIO.name, languages=["en-IN"], min_speakers=1,
                                          max_speakers=8, chirp_region="us", enable_denoiser=False,
                                          enable_role_inference=True)
        finally:
            gemini_client.generate_sync = orig
        check(f"gemini_failure: sentinel keeps native {n}", out.success and len(_labels(out)) == n,
              str(_labels(out)))
        check(f"gemini_failure: source unchanged ({n})", out.speaker_source == "diarization",
              out.speaker_source)

    for reply, name in (({}, "empty reply"), ({"assignments": "1293"}, "digit out of range"),
                        ({"assignments": "12x3"}, "foreign character")):
        with Patch(_generate_gemini_json=lambda prompt, **k: reply):
            got = at._gemini_multi_speaker_assignments(["a", "b", "c", "d"], max_speakers=8 if name != "digit out of range" else 3)
        check(f"gemini_failure: {name} rejects the block", got is None, str(got))
    with Patch(_generate_gemini_json=lambda prompt, **k: {"assignments": "1, 2 ,3"}):
        got = at._gemini_multi_speaker_assignments(["a", "b", "c", "d"], max_speakers=3)
    check("gemini_failure: short reply tolerated, separators stripped", got == {0: 1, 1: 2, 2: 3}, str(got))
    with Patch(_generate_gemini_json=lambda prompt, **k: {"assignments": "1293"}):
        got = at._gemini_multi_speaker_assignments(["a", "b", "c", "d"], speaker_count=4)
    check("gemini_failure: exact-mode digit above K rejects", got is None)
    res = _turns_to_result(_rotating_turns(4))
    out = _transcribe(res, _generate_gemini_json=lambda prompt, **k: {"assignments": "9" * 40},
                      _gemini_multi_speaker_assignments=at._gemini_multi_speaker_assignments)
    check("gemini_failure: out-of-range reply keeps native 4", len(_labels(out)) == 4, str(_labels(out)))


def check_chunked():
    # 7 drifting tags (unreliable) for what is really 3 voices.
    turns = []
    for i in range(14):
        true = 1 + i % 3
        turns.append((1 + i % 7, _voice(true, i)))
    res = _turns_to_result(turns, reliable=False)
    out = _transcribe(res, _gemini_multi_speaker_assignments=fake_multi_truth)
    check("chunked: N-speaker result accepted", out.speaker_source == "gemini_multi_diarization",
          out.speaker_source)
    check("chunked: 3 neutral labels", _labels(out) == ["Speaker A", "Speaker B", "Speaker C"], str(_labels(out)))

    # AC-13: above-max drifting tags and Gemini fails -> collapse, still delivered.
    many = [(1 + i, _voice(1 + i, i, 20)) for i in range(10)]
    res = _turns_to_result(many, reliable=False)
    out = _transcribe(res)
    check("chunked: failure above max collapses", out.speaker_source == "collapsed_no_diarization",
          out.speaker_source)
    check("chunked: collapsed transcript still delivered", out.success and _labels(out) == ["Speaker A"],
          str(_labels(out)))

    # Rollback setting (max 2): chunked uses the 2-speaker call pass.
    two_calls: list[int] = []

    def two(u):
        two_calls.append(len(u))
        return {i: 1 + i % 2 for i in range(len(u))}

    res = _turns_to_result([(1 + i % 5, _voice(1, i)) for i in range(6)], reliable=False, max_used=2)
    out = _transcribe(res, max_speakers=2, _gemini_diarization_assignments=two)
    check("chunked: max 2 uses the 2-speaker pass", two_calls and out.speaker_source == "gemini_diarization",
          out.speaker_source)


def check_near_silent():
    def seg(tag, n_words, i):
        return SpeakerSegment(speaker_tag=tag, text=" ".join(f"w{i}x{k}" for k in range(n_words)),
                              start_time=float(i), end_time=float(i) + 0.5)

    # AC-11: 4 speakers, tag 4 has 1 turn at ~1% of words.
    segs = []
    for i in range(30):
        segs.append(seg(1 + i % 3, 10, i))
    segs.insert(10, seg(4, 3, 99))
    merged, count = at._merge_near_silent_speakers(segs)
    check("near_silent: 1% speaker merged", count == 1 and len({s.speaker_tag for s in merged}) == 3,
          f"merged={count}")
    check("near_silent: merged into the previous speaker",
          not any(s.speaker_tag == 4 for s in merged))
    check("near_silent: no words lost",
          sum(len(s.text.split()) for s in merged) == sum(len(s.text.split()) for s in segs))

    # AC-10: 4 real speakers -> none merged.
    real = [seg(1 + i % 4, 10, i) for i in range(20)]
    merged, count = at._merge_near_silent_speakers(real)
    check("near_silent: real contributions kept", count == 0 and len({s.speaker_tag for s in merged}) == 4)

    # AC-12: two-person call with one near-silent side -> not merged.
    two = [seg(1, 200, 0), seg(2, 1, 1), seg(1, 200, 2)]
    merged, count = at._merge_near_silent_speakers(two)
    check("near_silent: two-person call never merged", count == 0 and len({s.speaker_tag for s in merged}) == 2)

    # Short but real speaker above the threshold (3 turns) is kept.
    short = [seg(1 + i % 2, 20, i) for i in range(12)] + [seg(3, 2, 50), seg(1, 20, 51), seg(3, 2, 52),
                                                          seg(2, 20, 53), seg(3, 2, 54)]
    merged, count = at._merge_near_silent_speakers(short)
    check("near_silent: short speaker with 3 turns kept", count == 0 and len({s.speaker_tag for s in merged}) == 3)

    # Never below 2: three speakers, two of them near-silent.
    tiny = [seg(1, 500, 0), seg(2, 1, 1), seg(1, 500, 2), seg(3, 1, 3)]
    merged, count = at._merge_near_silent_speakers(tiny)
    check("near_silent: stops at 2 speakers", len({s.speaker_tag for s in merged}) == 2 and count == 1)

    # End to end: reliable 4 with a near-silent speaker -> 3 labels.
    turns = _rotating_turns(3, rounds=12)
    turns.insert(7, (4, "v4 okay"))
    out = _transcribe(_turns_to_result(turns))
    check("near_silent: end to end 3 labels", _labels(out) == ["Speaker A", "Speaker B", "Speaker C"],
          str(_labels(out)))


def check_blocks():
    total = 12_000
    truth = [1 + (i // 3) % 4 for i in range(total)]
    utterances = [f"v{truth[i]} utt {i}" for i in range(total)]
    calls: list[int] = []

    def permuting(u, *, speaker_count=None, max_speakers=8):
        offset = int(u[0].split()[2])
        calls.append(offset)
        shift = len(calls) % 2 == 0  # every second block numbers speakers differently
        local = [truth[offset + j] for j in range(len(u))]
        if shift:
            return {j: (t % 4) + 1 for j, t in enumerate(local)}
        return {j: t for j, t in enumerate(local)}

    with Patch(_gemini_multi_speaker_assignments=permuting):
        got = at._gemini_diarization_assignments_blocked(utterances, block_size=5000, max_speakers=8)
    check("blocks: ran in several blocks", len(calls) > 2, str(len(calls)))
    check("blocks: no utterance dropped", got is not None and len(got) == total)
    consistent = got is not None and all(
        len({got[i] for i in range(total) if truth[i] == t}) == 1 for t in range(1, 5)
    ) and len(set(got.values())) == 4
    check("blocks: each person one label across blocks", consistent)

    calls.clear()
    with Patch(_gemini_multi_speaker_assignments=permuting):
        exact = at._gemini_diarization_assignments_blocked(utterances, block_size=5000, speaker_count=4,
                                                           max_speakers=8)
    check("blocks: exact mode consistent", exact == got)

    # Full path: _gemini_full_diarization keeps every utterance in N-speaker mode.
    turns = [{"text": u, "start_time": float(i), "end_time": i + 0.5} for i, u in enumerate(utterances)]
    calls.clear()
    with Patch(_gemini_multi_speaker_assignments=permuting):
        segs = at._gemini_full_diarization("", timed_turns=turns, max_speakers=8)
    n_utts = sum(s.text.count(" utt ") for s in segs or [])
    check("blocks: full diarization keeps all utterances", n_utts == total, str(n_utts))

    # AC-9: one failed block -> None.
    state = {"n": 0}

    def fails_second(u, **k):
        state["n"] += 1
        return None if state["n"] == 2 else permuting(u, **k)

    calls.clear()
    with Patch(_gemini_multi_speaker_assignments=fails_second):
        got = at._gemini_diarization_assignments_blocked(utterances, block_size=5000, max_speakers=8)
    check("blocks: a failed block returns None", got is None)

    # AC-9: a block that would push the total above the maximum -> None.
    def grows(u, *, speaker_count=None, max_speakers=8):
        offset = int(u[0].split()[2])
        if offset == 0:
            return {j: 1 + (j % 3) for j in range(len(u))}
        # Overlap matches speakers 1-3, the rest introduces 3 new voices.
        return {j: (1 + j % 3) if j < 200 else (4 + j % 3) for j in range(len(u))}

    with Patch(_gemini_multi_speaker_assignments=grows):
        got = at._gemini_diarization_assignments_blocked(utterances, block_size=5000, max_speakers=4)
    check("blocks: block above maximum returns None", got is None)

    # _map_block_tags reduces to flip / no flip for 2 speakers.
    prev = {0: 1, 1: 2, 2: 1, 3: 2}
    flipped = {0: 2, 1: 1, 2: 2, 3: 1, 4: 2}
    check("blocks: 2-speaker flip", at._map_block_tags(prev, flipped, range(0, 4), 2, 2) == {2: 1, 1: 2})
    same = {0: 1, 1: 2, 2: 1, 3: 2}
    check("blocks: 2-speaker no flip", at._map_block_tags(prev, same, range(0, 4), 2, 2) == {1: 1, 2: 2})


def check_range_retry():
    if not at._SPEECH_AVAILABLE:
        check("range_retry: google-cloud-speech installed", False, "skipped: not installed")
        return
    tmp = tempfile.mkdtemp(prefix="sim177_")
    wav = os.path.join(tmp, "clip.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)

    def ok_response():
        words = [SimpleNamespace(word=f"word{i}", speaker_label=str(1 + i % 3),
                                 start_offset=SimpleNamespace(seconds=i, nanos=0),
                                 end_offset=SimpleNamespace(seconds=i, nanos=500_000_000))
                 for i in range(9)]
        transcript = SimpleNamespace(results=[SimpleNamespace(
            language_code="en-in", alternatives=[SimpleNamespace(transcript="x", words=words)])])
        return SimpleNamespace(results={"u": SimpleNamespace(error=None, transcript=transcript)})

    def rejected(message):
        return SimpleNamespace(results={"u": SimpleNamespace(
            error=SimpleNamespace(code=3, message=message), transcript=None)})

    class Op:
        def __init__(self, r):
            self.r = r

        def result(self, timeout=None):
            return self.r

    def make_client(reject_above: int, message: str, seen: list):
        def batch_recognize(request):
            mx = request.config.features.diarization_config.max_speaker_count
            seen.append(mx)
            return Op(rejected(message) if mx > reject_above else ok_response())
        return SimpleNamespace(batch_recognize=batch_recognize)

    def run(client, *, cancel=None, chunked=None):
        billed, deleted = [], []
        patches = dict(
            SpeechV2Client=lambda **k: client,
            _resolve_project_id=lambda: "p",
            _get_gcs_bucket=lambda: "b",
            _prepare_audio_for_recognition=lambda path: at.PreparedAudio(wav, 16000, 1),
            _load_service_account_credentials=lambda: None,
            _upload_to_gcs_temp=lambda bucket, path, project: "gs://b/chirp_audio_temp/x.wav",
            _delete_from_gcs=lambda uri, project: deleted.append(uri),
            record_stt_seconds=billed.append,
            settings_audio_max_speakers_retry=6,
        )
        if chunked is not None:
            patches["_recognize_chunked_with_chirp3_v2"] = chunked
        token = at._CANCEL_EVENT.set(cancel)
        try:
            with Patch(**patches):
                try:
                    res = at._transcribe_offline_with_diarization(
                        audio_file=wav, project_id=None, language_codes=["en-IN"],
                        min_speakers=1, max_speakers=8, chirp_region="us",
                        enable_denoiser=False, duration_seconds=1.0)
                except BaseException as exc:  # noqa: BLE001 - the check inspects it
                    res = exc
        finally:
            at._CANCEL_EVENT.reset(token)
        return res, billed, deleted

    seen: list = []
    res, billed, deleted = run(make_client(6, "Requested max speaker count is not supported", seen))
    check("range_retry: retried once at max 6", seen == [8, 6], str(seen))
    check("range_retry: recording transcribed", isinstance(res, TranscriptResult) and res.words)
    check("range_retry: max used recorded", getattr(res, "max_speakers_used", None) == 6)
    check("range_retry: rejected job not billed", len(billed) == 1, str(billed))
    check("range_retry: temp object deleted for both jobs", len(deleted) == 2, str(deleted))

    seen = []
    res, billed, deleted = run(make_client(6, "Speaker diarization config invalid", seen))
    check("range_retry: 'diarization' keyword also detected", seen == [8, 6])

    # AC-17: unrelated INVALID_ARGUMENT -> no range retry, existing fallback.
    seen, chunk_calls = [], []

    def fake_chunked(**k):
        chunk_calls.append(1)
        return at.TranscriptResult("x", ["en-in"],
                                   [RecognizedWord(1, "word", 0.0, 0.3)], native_tags_reliable=False)

    res, billed, _ = run(make_client(0, "file is too long", seen), chunked=fake_chunked)
    check("range_retry: unrelated code 3 not retried", seen == [8], str(seen))
    check("range_retry: unrelated code 3 falls back as before", chunk_calls == [1] and not billed)

    # AC-18: retry rejected too -> RuntimeError, then success=False upstream.
    seen = []
    res, billed, deleted = run(make_client(0, "speaker count out of range", seen),
                               chunked=lambda **k: (_ for _ in ()).throw(AssertionError("no chunked")))
    check("range_retry: both rejected raises", isinstance(res, RuntimeError)
          and not isinstance(res, at.SpeakerRangeRejected), repr(type(res)))
    check("range_retry: both rejected never billed", billed == [] and len(deleted) == 2)
    with Patch(_SPEECH_AVAILABLE=True, _probe_duration_seconds=lambda p: None,
               _transcribe_offline_with_diarization=lambda **k: (_ for _ in ()).throw(
                   RuntimeError("Speech V2 Chirp 3 rejected the speaker range (max 6)"))):
        out = at._sync_transcribe(_AUDIO.name, languages=["en-IN"], min_speakers=1, max_speakers=8,
                                  chirp_region="us", enable_denoiser=False, enable_role_inference=True)
    check("range_retry: failure surfaces as success=False",
          not out.success and (out.error or "").startswith("transcription_failed"))

    # Detector unit checks.
    check("range_retry: detector matches code 3 + speaker",
          at._is_speaker_range_rejection(RuntimeError("BatchRecognize file error (code 3): max speaker")))
    check("range_retry: detector ignores code 13",
          not at._is_speaker_range_rejection(RuntimeError("file error (code 13): speaker")))
    check("range_retry: detector ignores cancellation",
          not at._is_speaker_range_rejection(at.TranscriptionCancelled("speaker")))

    # Chunked path re-raises a range rejection instead of skipping every chunk.
    chunk = os.path.join(tmp, "c0.wav")
    open(chunk, "wb").close()

    class InvalidArg(Exception):
        grpc_status_code = SimpleNamespace(name="INVALID_ARGUMENT")

    rec_client = SimpleNamespace(recognize=lambda request: (_ for _ in ()).throw(
        InvalidArg("400 invalid diarization speaker count")))
    with Patch(_split_pcm_wav_by_silence=lambda path: ([at.AudioChunk(chunk, 0, 1, 0, 1)], tempfile.mkdtemp()),
               record_stt_seconds=lambda s: None):
        try:
            at._recognize_chunked_with_chirp3_v2(rec_client, "r", at.cloud_speech.RecognitionConfig(),
                                                 at.PreparedAudio(wav, 16000, 1))
            raised = False
        except at.SpeakerRangeRejected:
            raised = True
    check("range_retry: chunked Recognize re-raises the rejection", raised)

    # Cancel before the retry: no second Speech job.
    event = threading.Event()
    seen = []
    base_client = make_client(6, "max speaker count unsupported", seen)

    def cancelling(request):
        r = base_client.batch_recognize(request)
        event.set()
        return r

    res, _, _ = run(SimpleNamespace(batch_recognize=cancelling), cancel=event)
    check("cancel: checked before the speaker-range retry",
          isinstance(res, at.TranscriptionCancelled) and seen == [8], f"{type(res).__name__} {seen}")


def check_labels_guard():
    segs = [SpeakerSegment(1, "a", 0, 1, inferred_role="lawyer"),
            SpeakerSegment(2, "b", 1, 2, inferred_role="victim_party"),
            SpeakerSegment(3, "c", 2, 3, inferred_role="lawyer")]
    letters = at._build_speaker_letter_map(segs)
    labels = at._build_role_label_map(segs, letters)
    check("labels_guard: 3 tags with roles are neutral",
          labels == {1: "Speaker A", 2: "Speaker B", 3: "Speaker C"}, str(labels))
    two = segs[:2]
    labels = at._build_role_label_map(two, at._build_speaker_letter_map(two))
    check("labels_guard: 2 tags keep roles", labels == {1: "Receiver", 2: "Caller"}, str(labels))
    eight = [SpeakerSegment(8 - k, "x", k, k + 1) for k in range(8)]
    letters = at._build_speaker_letter_map(eight)
    check("labels_guard: 8 speakers get A..H by first appearance",
          [letters[8 - k] for k in range(8)] == list("ABCDEFGH"))


def check_placeholders():
    from app.prompts.extraction_prompts import extract_parties_subagent_prompt
    from app.socket.event_utils import SPEAKER_LABEL_GUARDRAIL
    from app.socket.handlers.extraction import ExtractionMixin

    parties = [{"name": f"Speaker {c}", "role": "speaker"} for c in "CDEFGH"]
    parties.append({"name": "Fictional Traders Pvt Ltd", "role": "respondent"})
    kept = ExtractionMixin._drop_non_party_entities(parties)
    check("placeholders: Speaker C..H dropped", [p["name"] for p in kept] == ["Fictional Traders Pvt Ltd"],
          str([p["name"] for p in kept]))
    check("placeholders: guardrail names letters beyond B", '"Speaker C"' in SPEAKER_LABEL_GUARDRAIL
          and "any letter" in SPEAKER_LABEL_GUARDRAIL)
    prompt = extract_parties_subagent_prompt(combined_document_content="(fictional)")
    check("placeholders: parties prompt names letters beyond B", "'Speaker C'" in prompt and "any letter" in prompt)


def check_payload_shape():
    from app.socket.handlers.documents import DocumentsMixin

    def entry(transcript):
        batch = {"results": [{
            "document_id": "doc-1", "filename": "fictional-meeting", "success": True,
            "readability_score": 80, "readability_rating": "Good", "readable": True,
            "extracted_text_length": len(transcript), "stored_in_vector_db": True,
            "summary": "s", "evidentiary_strength": "Medium", "document_type": "audio_recording",
            "extracted_fields": {}, "sensitive_fields": {}, "is_audio": True,
            "transcript_status": "ready", "transcript": transcript,
        }], "total_files": 1, "successful": 1, "failed": 0}
        env = DocumentsMixin._build_document_assessment_envelope("ws", "user", batch)
        return env["payload"]["documentAssessments"][0]

    two = entry("[00:00] Caller: hello\n[00:05] Receiver: yes")
    many = entry("\n".join(f"[00:0{k}] Speaker {c}: line" for k, c in enumerate("ABCDE")))
    check("payload_shape: same keys", set(two) == set(many), str(set(two) ^ set(many)))
    check("payload_shape: same types", all(type(two[k]) is type(many[k]) for k in two))
    check("payload_shape: transcript ready", many["transcriptStatus"] == "ready" and "Speaker E" in many["transcript"])


def check_cancel():
    event = threading.Event()
    event.set()
    calls: list[int] = []
    token = at._CANCEL_EVENT.set(event)
    try:
        with Patch(_gemini_multi_speaker_assignments=lambda u, **k: calls.append(1) or {}):
            try:
                at._gemini_diarization_assignments_blocked([f"v1 utt {i}" for i in range(6000)],
                                                           block_size=5000, max_speakers=8)
                raised = False
            except at.TranscriptionCancelled:
                raised = True
    finally:
        at._CANCEL_EVENT.reset(token)
    check("cancel: block loop raises before the next model call", raised and calls == [])


def check_no_gcs():
    if not at._SPEECH_AVAILABLE:
        check("no_gcs: google-cloud-speech installed", False, "skipped: not installed")
        return
    tmp = tempfile.mkdtemp(prefix="sim177_nogcs_")
    wav = os.path.join(tmp, "clip.wav")
    open(wav, "wb").close()
    chunk_paths = []
    for k in range(2):
        p = os.path.join(tmp, f"c{k}.wav")
        open(p, "wb").close()
        chunk_paths.append(p)
    chunks = [at.AudioChunk(chunk_paths[0], 0.0, 30.0, 0.0, 29.0),
              at.AudioChunk(chunk_paths[1], 29.0, 60.0, 29.0, 60.0)]
    state = {"n": 0}

    def recognize(request):
        k = state["n"]
        state["n"] += 1
        words = []
        for i in range(6):
            true = 1 + (i + k) % 3
            # Drifting local tags: each chunk numbers speakers on its own.
            words.append(SimpleNamespace(word=f"v{true}", speaker_label=str(1 + (i * 2 + k) % 4),
                                         start_offset=SimpleNamespace(seconds=i * 4, nanos=0),
                                         end_offset=SimpleNamespace(seconds=i * 4, nanos=400_000_000)))
            words.append(SimpleNamespace(word=f"chunk{k}said{i}", speaker_label=str(1 + (i * 2 + k) % 4),
                                         start_offset=SimpleNamespace(seconds=i * 4, nanos=500_000_000),
                                         end_offset=SimpleNamespace(seconds=i * 4 + 1, nanos=0)))
        return SimpleNamespace(results=[SimpleNamespace(
            language_code="en-in", alternatives=[SimpleNamespace(transcript="x", words=words)])])

    client = SimpleNamespace(recognize=recognize)
    billed: list = []
    with Patch(
        _SPEECH_AVAILABLE=True,
        SpeechV2Client=lambda **k: client,
        _resolve_project_id=lambda: "p",
        _get_gcs_bucket=lambda: None,
        _probe_duration_seconds=lambda path: 60.0,
        _prepare_audio_for_recognition=lambda path: at.PreparedAudio(wav, 16000, 1),
        _load_service_account_credentials=lambda: None,
        _split_pcm_wav_by_silence=lambda path: (chunks, tempfile.mkdtemp()),
        record_stt_seconds=billed.append,
        _transliterate_segments_to_hinglish=lambda segs: segs,
        _gemini_multi_speaker_assignments=fake_multi_truth,
        _infer_speaker_roles_with_gemini=_no_roles,
    ):
        out = at._sync_transcribe(wav, languages=["en-IN"], min_speakers=1, max_speakers=8,
                                  chirp_region="us", enable_denoiser=False, enable_role_inference=True)
    check("no_gcs: chunked path succeeds", out.success, out.error or "")
    check("no_gcs: N-speaker result used", out.speaker_source == "gemini_multi_diarization", out.speaker_source)
    check("no_gcs: chunks billed", len(billed) == 2)


CHECKS = [
    ("speakers_1", check_speakers_1),
    ("speakers_2", check_speakers_2),
    ("speakers_3_5_8", check_speakers_3_5_8),
    ("correction", check_correction),
    ("gemini_failure", check_gemini_failure),
    ("chunked", check_chunked),
    ("near_silent", check_near_silent),
    ("blocks", check_blocks),
    ("range_retry", check_range_retry),
    ("labels_guard", check_labels_guard),
    ("placeholders", check_placeholders),
    ("payload_shape", check_payload_shape),
    ("cancel", check_cancel),
    ("no_gcs", check_no_gcs),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the check names")
    ap.add_argument("--only", nargs="*", default=None, help="run only these groups")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        return 0
    selected = set(args.only) if args.only else None
    try:
        for name, fn in CHECKS:
            if selected and name not in selected:
                continue
            print(f"[{name}]")
            fn()
    finally:
        os.unlink(_AUDIO.name)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
