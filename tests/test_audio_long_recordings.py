"""Long-recording audio transcription: windowing, stitching, cost, cancellation.

Chirp 3 BatchRecognize rejects files over 20 minutes. These tests cover the
windowed path that replaced the failing upload + per-40s fallback, with fake
Speech and Gemini clients: no network calls, no billed usage, fictional words.
"""

from __future__ import annotations

import threading
import wave
from itertools import pairwise
from types import SimpleNamespace

import pytest

import app.processing.audio_transcription as at
from app.processing.audio_transcription import (
    AudioWindow,
    RecognizedWord,
    TranscriptResult,
)


def _words(spec: list[tuple[int, str, float]]) -> list[RecognizedWord]:
    return [RecognizedWord(speaker_tag=t, word=w, start_time=s, end_time=s + 0.3) for t, w, s in spec]


# ── Window planning ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("duration", [2527.44, 60_000.0, 1141.0])
def test_windows_stay_under_batch_limit_and_tile_the_recording(duration):
    pauses = [float(p) for p in range(7, int(duration), 7)]
    windows = at._plan_batch_windows(
        duration, pauses, window_seconds=1140.0, overlap_seconds=15.0
    )
    assert windows[0].keep_start == 0.0
    assert windows[-1].keep_end == pytest.approx(duration)
    for prev, cur in pairwise(windows):
        assert cur.keep_start == prev.keep_end  # no gap, no double coverage
    for w in windows:
        assert w.end_time - w.start_time <= at.BATCH_MAX_FILE_SECONDS
        assert w.end_time - w.start_time <= 1140.0 + 1e-6
        assert w.start_time <= w.keep_start < w.keep_end <= w.end_time


def test_42_minute_call_needs_three_windows():
    windows = at._plan_batch_windows(2527.44, [], window_seconds=1140.0, overlap_seconds=15.0)
    assert len(windows) == 3


def test_cut_lands_on_a_pause():
    windows = at._plan_batch_windows(3000.0, [1000.0, 1090.5], window_seconds=1140.0, overlap_seconds=15.0)
    assert windows[0].keep_end == 1090.5


def test_window_must_leave_room_for_overlap():
    with pytest.raises(ValueError):
        at._plan_batch_windows(5000.0, [], window_seconds=100.0, overlap_seconds=30.0)


# ── Stitching ──────────────────────────────────────────────────────────────────
def test_stitch_maps_swapped_local_tags_back_to_the_same_speakers():
    windows = [
        AudioWindow(start_time=0, end_time=110, keep_start=0, keep_end=100),
        AudioWindow(start_time=90, end_time=200, keep_start=100, keep_end=200),
    ]
    first = TranscriptResult("", [], _words([
        (1, "hello", 10.0), (2, "namaste", 20.0),
        (1, "the", 92.0), (1, "contract", 93.0), (2, "was", 96.0), (2, "signed", 97.0),
        (1, "yes", 104.0),  # outside keep window: must not be emitted twice
    ]))
    # Second job numbered the speakers the other way round.
    second = TranscriptResult("", [], _words([
        (2, "the", 92.1), (2, "contract", 93.1), (1, "was", 96.1), (1, "signed", 97.1),
        (2, "yes", 104.1), (1, "okay", 150.0),
    ]))
    out = at._stitch_window_results([first, second], windows, min_speakers=2)

    by_word = {w.word: w.speaker_tag for w in out}
    assert by_word["yes"] == by_word["hello"]       # speaker 1 throughout
    assert by_word["okay"] == by_word["namaste"]    # speaker 2 throughout
    assert [w.word for w in out].count("yes") == 1  # overlap not duplicated
    assert len({w.speaker_tag for w in out}) == 2


def test_speaker_silent_in_overlap_is_not_minted_as_a_new_speaker():
    windows = [
        AudioWindow(start_time=0, end_time=110, keep_start=0, keep_end=100),
        AudioWindow(start_time=90, end_time=200, keep_start=100, keep_end=200),
    ]
    first = TranscriptResult("", [], _words([
        (1, "alpha", 10.0), (2, "beta", 20.0), (1, "gamma", 95.0), (1, "delta", 96.0),
    ]))
    second = TranscriptResult("", [], _words([
        (1, "gamma", 95.1), (1, "delta", 96.1), (2, "epsilon", 150.0),
    ]))
    out = at._stitch_window_results([first, second], windows, min_speakers=2)
    by_word = {w.word: w.speaker_tag for w in out}
    assert by_word["epsilon"] == by_word["beta"]
    assert len({w.speaker_tag for w in out}) == 2


# ── Batch job cost ─────────────────────────────────────────────────────────────
def _write_silent_wav(path, seconds: float = 2.0) -> None:
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(b"\x00\x00" * int(16000 * seconds))


class _FakeOperation:
    def __init__(self, response):
        self._response = response

    def result(self, timeout=None):
        return self._response


def _patch_gcs(monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(at, "_upload_to_gcs_temp", lambda bucket, path, project: "gs://b/chirp_audio_temp/x.wav")
    monkeypatch.setattr(at, "_delete_from_gcs", lambda uri, project: deleted.append(uri))
    return deleted


@pytest.mark.skipif(not at._SPEECH_AVAILABLE, reason="google-cloud-speech not installed")
def test_rejected_batch_job_is_not_billed_and_gcs_object_is_deleted(tmp_path, monkeypatch):
    wav = tmp_path / "clip.wav"
    _write_silent_wav(wav)
    deleted = _patch_gcs(monkeypatch)
    billed: list[float] = []
    monkeypatch.setattr(at, "record_stt_seconds", billed.append)

    rejected = SimpleNamespace(results={
        "gs://b/chirp_audio_temp/x.wav": SimpleNamespace(
            error=SimpleNamespace(code=3, message="file is too long"), transcript=None
        )
    })
    client = SimpleNamespace(batch_recognize=lambda request: _FakeOperation(rejected))

    res = at._batch_recognize_wav(
        client, "projects/p/locations/us/recognizers/_", at.cloud_speech.RecognitionConfig(),
        str(wav), "p", "b",
    )
    assert res is None
    assert billed == []
    assert deleted == ["gs://b/chirp_audio_temp/x.wav"]


@pytest.mark.skipif(not at._SPEECH_AVAILABLE, reason="google-cloud-speech not installed")
def test_successful_batch_job_is_billed_once_with_time_offset(tmp_path, monkeypatch):
    wav = tmp_path / "clip.wav"
    _write_silent_wav(wav, seconds=2.0)
    _patch_gcs(monkeypatch)
    billed: list[float] = []
    monkeypatch.setattr(at, "record_stt_seconds", billed.append)

    word = SimpleNamespace(
        word="hello", speaker_label="1",
        start_offset=SimpleNamespace(seconds=1, nanos=0),
        end_offset=SimpleNamespace(seconds=1, nanos=500_000_000),
    )
    transcript = SimpleNamespace(results=[SimpleNamespace(
        language_code="en-in",
        alternatives=[SimpleNamespace(transcript="hello", words=[word])],
    )])
    ok = SimpleNamespace(results={"u": SimpleNamespace(error=None, transcript=transcript)})
    client = SimpleNamespace(batch_recognize=lambda request: _FakeOperation(ok))

    res = at._batch_recognize_wav(
        client, "r", at.cloud_speech.RecognitionConfig(), str(wav), "p", "b", time_offset=600.0,
    )
    assert res is not None and res.words[0].start_time == pytest.approx(601.0)
    assert billed == [pytest.approx(2.0)]


# ── Windowed path end to end (fake recognizer) ─────────────────────────────────
def test_windowed_path_transcribes_every_window_once(monkeypatch):
    monkeypatch.setattr(at, "_detect_silence_split_points", lambda path: [])
    monkeypatch.setattr(at, "_extract_wav_clip", lambda src, out, s, e: open(out, "wb").close())
    calls: list[float] = []
    lock = threading.Lock()

    def fake_batch(client, recognizer, config, wav_path, project_id, bucket, *, time_offset=0.0, label=""):
        with lock:
            calls.append(time_offset)
        # One word every 10 s of this window, alternating speakers by absolute time.
        words = [
            RecognizedWord(speaker_tag=1 + (int(t) // 10) % 2, word=f"w{int(t)}", start_time=float(t), end_time=t + 0.3)
            for t in range(int(time_offset), int(time_offset) + 1140, 10)
        ]
        return TranscriptResult("x", ["en-in"], words)

    monkeypatch.setattr(at, "_batch_recognize_wav", fake_batch)
    out = at._recognize_long_audio_windowed(
        None, "r", None, "/nonexistent/source.mp3", 2527.44, "p", "b", min_speakers=2
    )
    assert len(calls) == 3
    starts = [w.start_time for w in out.words if w.start_time is not None and w.start_time < 2527.44]
    assert len(starts) == len(set(starts))  # no duplicated words from overlaps
    assert out.raw_transcript


def test_cancel_event_stops_windows(monkeypatch):
    monkeypatch.setattr(at, "_detect_silence_split_points", lambda path: [])
    monkeypatch.setattr(at, "_extract_wav_clip", lambda src, out, s, e: open(out, "wb").close())
    monkeypatch.setattr(at, "_batch_recognize_wav", lambda *a, **k: pytest.fail("must not run"))
    event = threading.Event()
    event.set()
    token = at._CANCEL_EVENT.set(event)
    try:
        with pytest.raises(at.TranscriptionCancelled):
            at._recognize_long_audio_windowed(
                None, "r", None, "/nonexistent/source.mp3", 5000.0, "p", "b", min_speakers=2
            )
    finally:
        at._CANCEL_EVENT.reset(token)


# ── Duration cap ───────────────────────────────────────────────────────────────
def test_duration_cap(monkeypatch):
    monkeypatch.setattr(at.settings, "audio_max_duration_minutes", 600.0)
    at._enforce_duration_cap(600 * 60.0)
    with pytest.raises(at.AudioTooLongError):
        at._enforce_duration_cap(601 * 60.0)
    monkeypatch.setattr(at.settings, "audio_max_duration_minutes", 0)
    at._enforce_duration_cap(1_000_000.0)  # 0 disables the cap


def test_probe_duration_reads_wav(tmp_path):
    wav = tmp_path / "clip.wav"
    _write_silent_wav(wav, seconds=3.0)
    assert at._probe_duration_seconds(str(wav)) == pytest.approx(3.0)


# ── Gemini re-diarization beyond one call ──────────────────────────────────────
def test_long_transcript_is_not_truncated_and_block_flips_are_reconciled(monkeypatch):
    total = 12_000
    truth = [1 + (i // 3) % 2 for i in range(total)]
    calls: list[int] = []

    def fake_assign(utterances):
        offset = int(utterances[0].split()[1])
        calls.append(offset)
        flip = len(calls) % 2 == 0  # every second block comes back with labels swapped
        return {j: (3 - truth[offset + j]) if flip else truth[offset + j] for j in range(len(utterances))}

    monkeypatch.setattr(at, "_gemini_diarization_assignments", fake_assign)
    turns = [{"text": f"utt {i}", "start_time": float(i), "end_time": i + 0.5} for i in range(total)]
    segments = at._gemini_full_diarization("", timed_turns=turns)

    assert segments is not None
    assert len(calls) > 1
    rebuilt: list[int] = []
    for seg in segments:
        rebuilt.extend([seg.speaker_tag] * len(seg.text.split("utt")[1:]))
    assert len(rebuilt) == total  # nothing after utterance 5000 is dropped
    assert rebuilt == truth


def test_failed_block_falls_back_to_native_tags(monkeypatch):
    monkeypatch.setattr(at, "_gemini_diarization_assignments", lambda u: None)
    turns = [{"text": f"utt {i}", "start_time": float(i), "end_time": i + 0.5} for i in range(6000)]
    assert at._gemini_full_diarization("", timed_turns=turns) is None
