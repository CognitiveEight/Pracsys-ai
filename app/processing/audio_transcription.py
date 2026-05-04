"""
Audio transcription with speaker diarization.

Wraps Google Cloud Speech-to-Text V2 (Chirp 3) for transcription + native
speaker diarization, with Gemini-based content re-diarization as a refinement
step. Returns a single formatted transcript suitable for the
`document_feature_response` `transcript` field — e.g.::

    [00:00] Speaker A: Hello, can you hear me?
    [00:03] Speaker B: Yes, what do you want now?

Public entry point: ``await transcribe_audio(file_path)``.
The sync GCP/ffmpeg pipeline is run via ``asyncio.to_thread`` so it does not
block the event loop.

Adapted from the reference Streamlit project at /project/chirp_audio.py.
ffmpeg must be installed on PATH; pyannote / torch are optional and used
only as a diarization fallback when GCS-backed BatchRecognize is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import warnings
import wave
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from loguru import logger

from app.core.config import settings

try:
    import google.generativeai as _genai
    from google.generativeai.types import HarmBlockThreshold, HarmCategory

    _GENAI_AVAILABLE = True
except Exception:  # pragma: no cover - optional dep, also configured elsewhere
    _genai = None
    HarmBlockThreshold = None  # type: ignore[assignment]
    HarmCategory = None  # type: ignore[assignment]
    _GENAI_AVAILABLE = False

try:
    from google.api_core.client_options import ClientOptions
    from google.api_core.exceptions import (
        GoogleAPICallError,
        ServiceUnavailable,
        Unauthenticated,
    )
    from google.cloud.speech_v2 import SpeechClient as SpeechV2Client
    from google.cloud.speech_v2.types import cloud_speech

    _SPEECH_AVAILABLE = True
except Exception as _e:  # pragma: no cover - import-time guard only
    SpeechV2Client = None  # type: ignore[assignment]
    cloud_speech = None  # type: ignore[assignment]
    ClientOptions = None  # type: ignore[assignment]
    GoogleAPICallError = Exception  # type: ignore[assignment]
    ServiceUnavailable = Exception  # type: ignore[assignment]
    Unauthenticated = Exception  # type: ignore[assignment]
    _SPEECH_AVAILABLE = False
    _SPEECH_IMPORT_ERROR = str(_e)
else:
    _SPEECH_IMPORT_ERROR = ""

# Optional voice diarization fallback (only loaded if HUGGINGFACE_TOKEN is set).
try:
    import torch  # type: ignore

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*torchcodec is not installed correctly.*",
            category=UserWarning,
        )
        from pyannote.audio import Pipeline as PyannotePipeline  # type: ignore
except Exception:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    PyannotePipeline = None  # type: ignore[assignment]


# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_LANGUAGE_CODES = [
    "en-IN", "hi-IN", "as-IN", "bn-IN", "gu-IN", "kn-IN",
    "ml-IN", "mr-IN", "or-IN", "pa-Guru-IN", "ta-IN", "te-IN",
]
DIARIZATION_SUPPORTED_LANGUAGES: frozenset[str] = frozenset({
    "cmn-Hans-CN", "de-DE", "en-GB", "en-IN", "en-US",
    "es-ES", "es-US", "fr-CA", "fr-FR", "hi-IN",
    "it-IT", "ja-JP", "ko-KR", "pt-BR",
})
CHUNK_TARGET_SECONDS = 40.0
CHUNK_OVERLAP_SECONDS = 1.5
MIN_SILENCE_SECONDS = 0.6
MIN_CHUNK_SECONDS = 12.0
SILENCE_NOISE_THRESHOLD_DB = -35
DEFAULT_CHIRP_REGION = "us"
_PYANNOTE_PIPELINE = None

# Gemini safety settings (legal call recordings often trip default filters).
if HarmBlockThreshold is not None and HarmCategory is not None:
    _PERMISSIVE_SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }
else:
    _PERMISSIVE_SAFETY_SETTINGS = {}


# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class PreparedAudio:
    path: str
    sample_rate_hertz: int
    channel_count: int
    cleanup_path: Optional[str] = None


@dataclass
class SpeakerSegment:
    speaker_tag: int
    text: str
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    speaker_label: Optional[str] = None  # "Speaker A", "Speaker B", ...
    inferred_role: Optional[str] = None


@dataclass
class RoleInference:
    role_by_speaker: dict[int, str]
    confidence_by_speaker: dict[int, int]
    evidence_by_speaker: dict[int, list[str]]
    detected_roles: list[str]


@dataclass
class RecognizedWord:
    speaker_tag: int
    word: str
    start_time: Optional[float]
    end_time: Optional[float]


@dataclass
class TranscriptResult:
    raw_transcript: str
    detected_languages: list[str]
    words: list[RecognizedWord]
    timed_turns: Optional[list[dict]] = None
    speaker_source: str = "diarization"


@dataclass
class AudioChunk:
    path: str
    start_time: float
    end_time: float
    keep_start: float
    keep_end: float


@dataclass
class AudioTranscriptionResult:
    """Return value of the public ``transcribe_audio`` API."""

    success: bool
    transcript: str = ""  # Formatted: "[MM:SS] Speaker A: text\n..."
    raw_transcript: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    detected_languages: list[str] = field(default_factory=list)
    speaker_source: str = ""
    error: Optional[str] = None
    duration_seconds: Optional[float] = None


# ── Gemini helpers (use existing google-generativeai SDK) ─────────────────────
def _gemini_configured() -> bool:
    if not _GENAI_AVAILABLE or not settings.google_api_key:
        return False
    try:
        _genai.configure(api_key=settings.google_api_key)
        return True
    except Exception:
        return False


def _gemini_model_name() -> str:
    return (settings.gemini_model or "gemini-2.5-flash").strip()


def _extract_json_object(text: str) -> Optional[dict | list]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_valid_json_text(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
        if match:
            text = match.group(1).strip()
    candidate = _extract_json_object(text)
    if candidate is not None:
        return json.dumps(candidate)
    text = re.sub(r",\s*([\]}])", r"\1", text)
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        return None


def _generate_gemini_json(prompt: str, *, max_output_tokens: int = 2048) -> Optional[dict]:
    """Run a JSON-mode Gemini call. Returns None on any failure."""
    if not _gemini_configured():
        logger.warning("Audio Gemini JSON call skipped: GOOGLE_API_KEY not configured")
        return None
    try:
        model = _genai.GenerativeModel(_gemini_model_name())
        config = _genai.types.GenerationConfig(
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
        )
        response = model.generate_content(
            prompt,
            generation_config=config,
            safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
        )
    except Exception as e:
        logger.warning("Audio Gemini JSON call failed: {}", e)
        return None
    text = getattr(response, "text", "") or ""
    if not text:
        logger.warning("Audio Gemini JSON call returned empty text")
        return None
    valid = _coerce_valid_json_text(text)
    if not valid:
        logger.warning(
            "Audio Gemini JSON response not valid JSON (preview={!r})", text[:200]
        )
        return None
    obj = _extract_json_object(valid)
    if not isinstance(obj, dict):
        logger.warning(
            "Audio Gemini JSON response was not an object (got {})", type(obj).__name__
        )
        return None
    return obj


def _generate_gemini_text(prompt: str, *, max_output_tokens: int = 128) -> Optional[str]:
    if not _gemini_configured():
        return None
    try:
        model = _genai.GenerativeModel(_gemini_model_name())
        config = _genai.types.GenerationConfig(
            temperature=0.0, max_output_tokens=max_output_tokens
        )
        response = model.generate_content(
            prompt,
            generation_config=config,
            safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
        )
    except Exception as e:
        logger.debug("Audio Gemini text call failed: {}", e)
        return None
    text = (getattr(response, "text", "") or "").strip()
    return text or None


# ── Project + credentials resolution ──────────────────────────────────────────
# Cached so we resolve only once per process.
_RESOLVED_CREDENTIALS_PATH: Optional[str] = None
_CREDENTIALS_RESOLVED: bool = False


def _candidate_base_dirs() -> list[str]:
    bases: list[str] = []
    cwd = os.getcwd()
    if cwd:
        bases.append(cwd)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if repo_root not in bases:
        bases.append(repo_root)
    return bases


def _is_service_account_json(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("type") == "service_account"


def _resolve_credentials_path() -> Optional[str]:
    """Locate the Speech-specific service-account JSON for Chirp 3 + GCS.

    Resolution order — first non-empty wins:
      1. ``settings.speech_google_application_credentials`` /
         ``SPEECH_GOOGLE_APPLICATION_CREDENTIALS`` env var.
      2. ``settings.google_application_credentials`` /
         ``GOOGLE_APPLICATION_CREDENTIALS`` env var (the OCR fallback —
         only useful if that SA also has Speech permissions).

    Relative paths are resolved against cwd then the repo root.
    The resolved path is **not** written back to ``os.environ`` — credentials
    are passed explicitly to ``SpeechV2Client`` / GCS ``Client`` so the OCR
    path keeps using its own ``GOOGLE_APPLICATION_CREDENTIALS``.
    """
    global _RESOLVED_CREDENTIALS_PATH, _CREDENTIALS_RESOLVED
    if _CREDENTIALS_RESOLVED:
        return _RESOLVED_CREDENTIALS_PATH

    raw_candidates = [
        getattr(settings, "speech_google_application_credentials", None),
        os.getenv("SPEECH_GOOGLE_APPLICATION_CREDENTIALS"),
        settings.google_application_credentials,
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    ]
    bases = _candidate_base_dirs()

    resolved: Optional[str] = None
    for raw in raw_candidates:
        if not raw:
            continue
        candidates: list[str] = []
        if os.path.isabs(raw):
            candidates.append(raw)
        else:
            candidates.extend(os.path.join(base, raw) for base in bases)
        for cand in candidates:
            if os.path.exists(cand) and _is_service_account_json(cand):
                resolved = os.path.abspath(cand)
                break
        if resolved:
            break

    if resolved:
        logger.info("Audio: using Speech service account at {}", resolved)
    else:
        logger.warning(
            "Audio: no Speech service-account JSON resolved. Set "
            "SPEECH_GOOGLE_APPLICATION_CREDENTIALS in .env or pass an absolute "
            "path. Falling back to Application Default Credentials, which will "
            "fail if gcloud user creds are expired."
        )

    _RESOLVED_CREDENTIALS_PATH = resolved
    _CREDENTIALS_RESOLVED = True
    return resolved


def _load_service_account_credentials():
    """Return a ``service_account.Credentials`` for the resolved path, or None."""
    path = _resolve_credentials_path()
    if not path:
        return None
    try:
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        return None
    try:
        return service_account.Credentials.from_service_account_file(path)
    except Exception as exc:  # pragma: no cover - propagates via client error
        logger.warning("Audio: failed to load service-account credentials: {}", exc)
        return None


def _resolve_project_id() -> Optional[str]:
    candidate = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or settings.gcp_project_id
        or os.getenv("GCP_PROJECT_ID")
    )
    if candidate:
        return candidate
    creds_path = _resolve_credentials_path()
    if not creds_path:
        return None
    try:
        with open(creds_path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("project_id")
    except (OSError, ValueError, TypeError):
        return None


def _get_gcs_bucket() -> Optional[str]:
    """Return the GCS bucket name for temporary audio storage, or None."""
    bucket = (
        getattr(settings, "gcs_bucket_name", None)
        or os.getenv("GCS_BUCKET_NAME")
        or os.getenv("GOOGLE_CLOUD_BUCKET")
        or ""
    )
    return bucket.strip() or None


# ── ffmpeg + WAV preparation ──────────────────────────────────────────────────
def _ffmpeg_env() -> dict:
    env = os.environ.copy()
    # Keep system ffmpeg from being shadowed by Conda-shipped libs.
    for key in ("LD_LIBRARY_PATH", "CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    return env


def _convert_audio_to_pcm_wav(input_audio_file: str) -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "ffmpeg is not installed on PATH. Install ffmpeg or supply a 16-bit PCM WAV."
        )

    with tempfile.NamedTemporaryFile(
        prefix="chirp_pcm16_", suffix=".wav", delete=False
    ) as temp_file:
        output_path = temp_file.name

    cmd = [
        ffmpeg_path, "-y", "-i", input_audio_file,
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=_ffmpeg_env())
    if result.returncode != 0:
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise RuntimeError(
            "ffmpeg conversion failed. stderr:\n"
            f"{result.stderr.strip() or 'No stderr output from ffmpeg.'}"
        )
    return output_path


def _prepare_audio_for_recognition(audio_file: str) -> PreparedAudio:
    cleanup_path: Optional[str] = None
    prepared_path = audio_file

    needs_conversion = not audio_file.lower().endswith(".wav")
    if not needs_conversion:
        try:
            with wave.open(audio_file, "rb") as wav_file:
                needs_conversion = (
                    wav_file.getcomptype() != "NONE"
                    or wav_file.getsampwidth() != 2
                    or wav_file.getframerate() != 16000
                    or wav_file.getnchannels() != 1
                )
        except wave.Error:
            needs_conversion = True

    if needs_conversion:
        logger.info("Audio: normalising input with ffmpeg → PCM16 mono 16kHz")
        prepared_path = _convert_audio_to_pcm_wav(audio_file)
        cleanup_path = prepared_path

    with wave.open(prepared_path, "rb") as wav_file:
        return PreparedAudio(
            path=prepared_path,
            sample_rate_hertz=wav_file.getframerate(),
            channel_count=wav_file.getnchannels(),
            cleanup_path=cleanup_path,
        )


def _wav_duration_seconds(audio_file: str) -> float:
    with wave.open(audio_file, "rb") as wav_file:
        return wav_file.getnframes() / float(wav_file.getframerate())


def _detect_silence_split_points(audio_file: str) -> list[float]:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return []
    cmd = [
        ffmpeg_path, "-i", audio_file,
        "-af",
        f"silencedetect=noise={SILENCE_NOISE_THRESHOLD_DB}dB:d={MIN_SILENCE_SECONDS}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=_ffmpeg_env())
    output = f"{result.stdout}\n{result.stderr}"
    silence_starts = [float(v) for v in re.findall(r"silence_start: ([0-9.]+)", output)]
    silence_ends = [float(v) for v in re.findall(r"silence_end: ([0-9.]+)", output)]
    points: list[float] = []
    for start, end in zip(silence_starts, silence_ends):
        if end > start:
            points.append((start + end) / 2.0)
    return sorted(points)


def _build_chunk_ranges(
    duration_seconds: float, split_points: list[float]
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration_seconds:
        target_end = min(duration_seconds, cursor + CHUNK_TARGET_SECONDS)
        min_end = min(duration_seconds, cursor + MIN_CHUNK_SECONDS)
        candidates = [p for p in split_points if min_end <= p <= target_end]
        if candidates:
            chunk_end = candidates[-1]
        elif target_end < duration_seconds:
            future = [p for p in split_points if target_end < p <= target_end + 8.0]
            chunk_end = future[0] if future else target_end
        else:
            chunk_end = duration_seconds
        if chunk_end <= cursor:
            chunk_end = min(duration_seconds, cursor + CHUNK_TARGET_SECONDS)
        ranges.append((cursor, chunk_end))
        cursor = chunk_end
    return ranges


def _extract_wav_clip(source_audio: str, output_path: str, start_time: float, end_time: float) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg is required to extract chunked audio clips.")
    cmd = [
        ffmpeg_path, "-y",
        "-ss", f"{start_time:.3f}",
        "-to", f"{end_time:.3f}",
        "-i", source_audio,
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=_ffmpeg_env())
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg chunk extraction failed. stderr:\n"
            f"{result.stderr.strip() or 'No stderr output from ffmpeg.'}"
        )


def _split_pcm_wav_by_silence(audio_file: str) -> tuple[list[AudioChunk], str]:
    duration_seconds = _wav_duration_seconds(audio_file)
    split_points = _detect_silence_split_points(audio_file)
    base_ranges = _build_chunk_ranges(duration_seconds, split_points)

    output_dir = tempfile.mkdtemp(prefix="chirp_chunks_")
    chunks: list[AudioChunk] = []
    for index, (base_start, base_end) in enumerate(base_ranges):
        chunk_start = max(0.0, base_start - (0.0 if index == 0 else CHUNK_OVERLAP_SECONDS))
        chunk_end = min(
            duration_seconds,
            base_end + (0.0 if index == len(base_ranges) - 1 else CHUNK_OVERLAP_SECONDS),
        )
        chunk_path = os.path.join(output_dir, f"chunk_{index:04d}.wav")
        _extract_wav_clip(audio_file, chunk_path, chunk_start, chunk_end)
        chunks.append(
            AudioChunk(
                path=chunk_path,
                start_time=chunk_start,
                end_time=chunk_end,
                keep_start=base_start,
                keep_end=base_end,
            )
        )
    return chunks, output_dir


# ── Speech-to-Text V2 plumbing ────────────────────────────────────────────────
def _duration_to_seconds(duration: object) -> Optional[float]:
    if duration is None:
        return None
    seconds = getattr(duration, "seconds", 0)
    nanos = getattr(duration, "nanos", 0)
    try:
        return float(seconds) + float(nanos) / 1_000_000_000
    except (TypeError, ValueError):
        return None


def _parse_speaker_label(label: str) -> int:
    if not label:
        return 0
    try:
        val = int(label)
        return max(0, val)
    except ValueError:
        pass
    match = re.search(r"(\d+)$", label)
    if match:
        n = int(match.group(1))
        if re.match(r"(?i)speaker[_-]?\d+$", label):
            return n + 1
        return n if n > 0 else 1
    return 0


def _v2_response_to_transcript_result(response, time_offset: float = 0.0) -> TranscriptResult:
    detected_languages: list[str] = []
    raw_lines: list[str] = []
    diarized_words: list[RecognizedWord] = []

    for result in getattr(response, "results", []):
        language_code = getattr(result, "language_code", "") or ""
        if language_code and language_code not in detected_languages:
            detected_languages.append(language_code)
        alternatives = getattr(result, "alternatives", [])
        if not alternatives:
            continue
        transcript = (getattr(alternatives[0], "transcript", "") or "").strip()
        if transcript:
            raw_lines.append(transcript)

    for result in getattr(response, "results", []):
        alternatives = getattr(result, "alternatives", [])
        if not alternatives:
            continue
        for word_info in getattr(alternatives[0], "words", []):
            token = (getattr(word_info, "word", "") or "").strip()
            if not token:
                continue
            speaker_label = str(getattr(word_info, "speaker_label", "") or "").strip()
            speaker_tag_raw = getattr(word_info, "speaker_tag", 0)
            speaker_tag = (
                _parse_speaker_label(speaker_label)
                if speaker_label
                else int(speaker_tag_raw or 0)
            )
            start_time = _duration_to_seconds(getattr(word_info, "start_offset", None))
            end_time = _duration_to_seconds(getattr(word_info, "end_offset", None))
            diarized_words.append(
                RecognizedWord(
                    speaker_tag=speaker_tag,
                    word=token,
                    start_time=None if start_time is None else start_time + time_offset,
                    end_time=None if end_time is None else end_time + time_offset,
                )
            )

    # Chirp 3 BatchRecognize emits 0-indexed speaker IDs; shift to 1-indexed
    # (the rest of the pipeline treats tag 0 as "untagged").
    if diarized_words and min(w.speaker_tag for w in diarized_words) == 0:
        diarized_words = [
            RecognizedWord(
                speaker_tag=w.speaker_tag + 1,
                word=w.word,
                start_time=w.start_time,
                end_time=w.end_time,
            )
            for w in diarized_words
        ]

    return TranscriptResult(
        raw_transcript="\n".join(raw_lines),
        detected_languages=detected_languages,
        words=diarized_words,
    )


def _v2_batch_response_to_transcript_result(
    batch_response, time_offset: float = 0.0
) -> TranscriptResult:
    for file_result in getattr(batch_response, "results", {}).values():
        if file_result is None:
            continue
        error = getattr(file_result, "error", None)
        if error and getattr(error, "code", 0) != 0:
            raise RuntimeError(
                f"BatchRecognize file error (code {error.code}): "
                f"{getattr(error, 'message', str(error))}"
            )
        transcript_response = getattr(file_result, "transcript", None)
        if transcript_response is not None:
            return _v2_response_to_transcript_result(transcript_response, time_offset=time_offset)
    return TranscriptResult(raw_transcript="", detected_languages=[], words=[])


def _render_text_from_words(words: list[RecognizedWord]) -> str:
    if not words:
        return ""
    parts: list[str] = []
    previous_end: Optional[float] = None
    sentence_break = re.compile(r"[.!?]['\"]?$")
    for word in sorted(words, key=lambda w: ((w.start_time or 0.0), (w.end_time or 0.0))):
        token = word.word.strip()
        if not token:
            continue
        if parts:
            if previous_end is not None and word.start_time is not None and (word.start_time - previous_end) > 1.0:
                parts.append("\n")
            elif sentence_break.search(parts[-1]):
                parts.append(" ")
            elif re.match(r"^[,.;:!?)]", token):
                pass
            else:
                parts.append(" ")
        parts.append(token)
        previous_end = word.end_time if word.end_time is not None else previous_end
    return "".join(parts).strip()


def _build_timed_turns(words: list[RecognizedWord]) -> list[dict]:
    turns: list[dict] = []
    for word in sorted(words, key=lambda w: ((w.start_time or 0.0), (w.end_time or 0.0))):
        token = word.word.strip()
        speaker_tag = int(word.speaker_tag or 0)
        if not token:
            continue
        start_time = word.start_time if word.start_time is not None else 0.0
        end_time = word.end_time if word.end_time is not None else start_time
        if (
            turns
            and turns[-1]["speaker_tag"] == speaker_tag
            and start_time - turns[-1]["end_time"] <= 0.9
        ):
            if re.match(r"^[,.;:!?)]", token):
                turns[-1]["text"] = f"{turns[-1]['text']}{token}"
            else:
                turns[-1]["text"] = f"{turns[-1]['text']} {token}".strip()
            turns[-1]["end_time"] = max(turns[-1]["end_time"], end_time)
        else:
            turns.append(
                {
                    "speaker_tag": speaker_tag if speaker_tag > 0 else 1,
                    "start_time": start_time,
                    "end_time": end_time,
                    "text": token,
                }
            )
    return turns


def _trim_words_to_window(
    words: list[RecognizedWord], keep_start: float, keep_end: float
) -> list[RecognizedWord]:
    trimmed: list[RecognizedWord] = []
    for word in words:
        start_time = word.start_time if word.start_time is not None else keep_start
        end_time = word.end_time if word.end_time is not None else start_time
        if end_time < keep_start or start_time > keep_end:
            continue
        trimmed.append(word)
    return trimmed


def _merge_text_with_overlap(existing_text: str, new_text: str) -> str:
    if not existing_text:
        return new_text
    if not new_text:
        return existing_text
    e = existing_text.strip()
    n = new_text.strip()
    if not n:
        return existing_text
    if n in e or e.endswith(n):
        return existing_text
    if n.startswith(e):
        return new_text
    sep = "\n" if not existing_text.endswith("\n") else ""
    return f"{existing_text}{sep}{n}"


def _merge_transcript_results(results: list[TranscriptResult]) -> TranscriptResult:
    detected_languages: list[str] = []
    merged_raw = ""
    words: list[RecognizedWord] = []
    for r in results:
        if r.raw_transcript:
            merged_raw = _merge_text_with_overlap(merged_raw, r.raw_transcript)
        for code in r.detected_languages:
            if code not in detected_languages:
                detected_languages.append(code)
        words.extend(r.words)
    timed_turns = _build_timed_turns(words)
    rebuilt = _render_text_from_words(words)
    if rebuilt:
        merged_raw = rebuilt
    return TranscriptResult(
        raw_transcript=merged_raw.strip(),
        detected_languages=detected_languages,
        words=words,
        timed_turns=timed_turns,
    )


def _align_speakers_across_chunks(
    chunk_word_lists: list[list[RecognizedWord]],
    keep_windows: list[tuple[float, float]],
) -> list[RecognizedWord]:
    """Re-map per-chunk speaker tags to globally consistent ones."""
    if not chunk_word_lists:
        return []
    all_words: list[RecognizedWord] = []
    global_counter = 0
    local_to_global: dict[tuple[int, int], int] = {}

    for chunk_idx, (words, (keep_start, _keep_end)) in enumerate(
        zip(chunk_word_lists, keep_windows)
    ):
        if not words:
            continue
        local_tags = sorted({w.speaker_tag for w in words if w.speaker_tag > 0})
        if not local_tags:
            all_words.extend(words)
            continue

        if chunk_idx == 0 or not all_words:
            for tag in local_tags:
                global_counter += 1
                local_to_global[(chunk_idx, tag)] = global_counter
        else:
            overlap_floor = keep_start - CHUNK_OVERLAP_SECONDS * 2
            prev_overlap = [
                w for w in all_words
                if w.start_time is not None and w.start_time >= overlap_floor
            ]
            curr_overlap = [
                w for w in words
                if w.start_time is not None and w.start_time < keep_start
            ]
            pair_votes: dict[tuple[int, int], int] = {}
            for cw in curr_overlap:
                if not prev_overlap:
                    break
                ct = cw.start_time or 0.0
                best = min(prev_overlap, key=lambda pw: abs((pw.start_time or 0.0) - ct))
                if abs((best.start_time or 0.0) - ct) < 0.8:
                    pair_votes[(cw.speaker_tag, best.speaker_tag)] = (
                        pair_votes.get((cw.speaker_tag, best.speaker_tag), 0) + 1
                    )
            new_map: dict[int, int] = {}
            assigned: set[int] = set()
            for (local_tag, global_tag), _ in sorted(
                pair_votes.items(), key=lambda x: x[1], reverse=True
            ):
                if local_tag not in new_map and global_tag not in assigned:
                    new_map[local_tag] = global_tag
                    assigned.add(global_tag)
            for tag in local_tags:
                if tag not in new_map:
                    global_counter += 1
                    new_map[tag] = global_counter
                local_to_global[(chunk_idx, tag)] = new_map[tag]

        for w in words:
            g = local_to_global.get((chunk_idx, w.speaker_tag), w.speaker_tag)
            all_words.append(
                RecognizedWord(
                    speaker_tag=g, word=w.word,
                    start_time=w.start_time, end_time=w.end_time,
                )
            )
    return sorted(all_words, key=lambda w: ((w.start_time or 0.0), (w.end_time or 0.0)))


def _gcs_client(project_id: str):
    from google.cloud import storage as gcs  # type: ignore

    creds = _load_service_account_credentials()
    if creds is not None:
        return gcs.Client(project=project_id, credentials=creds)
    return gcs.Client(project=project_id)


def _upload_to_gcs_temp(bucket_name: str, local_path: str, project_id: str) -> str:
    try:
        client = _gcs_client(project_id)
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "google-cloud-storage is required for GCS-backed BatchRecognize. "
            "Install it with: pip install google-cloud-storage"
        ) from exc

    bucket = client.bucket(bucket_name)
    blob_name = f"chirp_audio_temp/{uuid.uuid4().hex}.wav"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    return f"gs://{bucket_name}/{blob_name}"


def _delete_from_gcs(gcs_uri: str, project_id: str) -> None:
    try:
        client = _gcs_client(project_id)
    except ImportError:
        return
    without_scheme = gcs_uri[5:]
    bucket_name, _, blob_name = without_scheme.partition("/")
    try:
        client.bucket(bucket_name).blob(blob_name).delete()
    except Exception as exc:
        logger.debug("Could not delete temp GCS object {}: {}", gcs_uri, exc)


# gRPC code 13 = INTERNAL. Speech V2 BatchRecognize occasionally surfaces this
# as a transient per-file error; retrying with backoff usually clears it before
# we have to fall back to the slower chunked Recognize path.
_BATCH_RETRYABLE_CODES: frozenset[int] = frozenset({13, 14})  # INTERNAL, UNAVAILABLE
_BATCH_MAX_ATTEMPTS = 3
_BATCH_BACKOFF_SECONDS = (2.0, 5.0)  # delays before attempt 2, attempt 3


def _batch_error_code(exc: BaseException) -> Optional[int]:
    """Extract the gRPC code from either a wrapped RuntimeError or a Google API exception."""
    msg = str(exc)
    match = re.search(r"code\s+(\d+)", msg)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            value = code()
            return int(getattr(value, "value", value))
        except Exception:
            return None
    if isinstance(code, int):
        return code
    return None


def _recognize_with_batch_chirp3_gcs(
    client,
    recognizer: str,
    config,
    prepared_audio: PreparedAudio,
    project_id: str,
    gcs_bucket: str,
) -> Optional[TranscriptResult]:
    import time

    gcs_uri: Optional[str] = None
    try:
        logger.info(
            "Audio: uploading to gs://{}/chirp_audio_temp/ for BatchRecognize",
            gcs_bucket,
        )
        gcs_uri = _upload_to_gcs_temp(gcs_bucket, prepared_audio.path, project_id)

        file_metadata = cloud_speech.BatchRecognizeFileMetadata(uri=gcs_uri)
        request = cloud_speech.BatchRecognizeRequest(
            recognizer=recognizer,
            config=config,
            files=[file_metadata],
            recognition_output_config=cloud_speech.RecognitionOutputConfig(
                inline_response_config=cloud_speech.InlineOutputConfig(),
            ),
        )

        last_exc: Optional[BaseException] = None
        for attempt in range(1, _BATCH_MAX_ATTEMPTS + 1):
            try:
                operation = client.batch_recognize(request=request)
                batch_response = operation.result(timeout=600)
                result = _v2_batch_response_to_transcript_result(batch_response)
                if result.words or result.raw_transcript:
                    logger.info(
                        "Audio: BatchRecognize with diarization succeeded (attempt {})",
                        attempt,
                    )
                    return result
                logger.warning(
                    "Audio: BatchRecognize returned empty result (attempt {}); "
                    "falling back to chunked mode",
                    attempt,
                )
                return None
            except Exception as exc:
                last_exc = exc
                code = _batch_error_code(exc)
                if code in _BATCH_RETRYABLE_CODES and attempt < _BATCH_MAX_ATTEMPTS:
                    delay = _BATCH_BACKOFF_SECONDS[attempt - 1]
                    logger.warning(
                        "Audio: BatchRecognize transient error code {} on attempt {} "
                        "({}); retrying in {}s",
                        code, attempt, exc, delay,
                    )
                    time.sleep(delay)
                    continue
                break

        logger.warning(
            "Audio: BatchRecognize via GCS failed ({}); falling back to chunked",
            last_exc,
        )
        return None
    finally:
        if gcs_uri:
            _delete_from_gcs(gcs_uri, project_id)


def _recognize_chunked_with_chirp3_v2(
    client,
    recognizer: str,
    config,
    prepared_audio: PreparedAudio,
) -> TranscriptResult:
    logger.info("Audio: pause-aware chunking with Recognize (no GCS bucket configured)")
    chunks, chunk_dir = _split_pcm_wav_by_silence(prepared_audio.path)
    chunk_results: list[TranscriptResult] = []
    chunk_word_lists: list[list[RecognizedWord]] = []
    keep_windows: list[tuple[float, float]] = []
    try:
        for chunk_idx, chunk in enumerate(chunks):
            with open(chunk.path, "rb") as audio_handle:
                request = cloud_speech.RecognizeRequest(
                    recognizer=recognizer,
                    config=config,
                    content=audio_handle.read(),
                )
            try:
                response = client.recognize(request=request)
            except Exception as exc:
                logger.warning("Audio: Recognize failed for chunk {} ({}); skipping", chunk_idx, exc)
                continue
            res = _v2_response_to_transcript_result(response, time_offset=chunk.start_time)
            res.words = _trim_words_to_window(
                res.words, keep_start=chunk.keep_start, keep_end=chunk.keep_end
            )
            chunk_results.append(res)
            chunk_word_lists.append(res.words)
            keep_windows.append((chunk.keep_start, chunk.keep_end))
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)

    aligned_words = _align_speakers_across_chunks(chunk_word_lists, keep_windows)
    if aligned_words:
        timed_turns = _build_timed_turns(aligned_words)
        return TranscriptResult(
            raw_transcript=_render_text_from_words(aligned_words),
            detected_languages=list(
                {
                    code
                    for r in chunk_results
                    for code in r.detected_languages
                }
            ),
            words=aligned_words,
            timed_turns=timed_turns,
        )
    return _merge_transcript_results(chunk_results)


def _transcribe_offline_with_diarization(
    audio_file: str,
    project_id: Optional[str],
    language_codes: list[str],
    min_speakers: int,
    max_speakers: int,
    chirp_region: str,
    enable_denoiser: bool,
) -> TranscriptResult:
    if not _SPEECH_AVAILABLE:
        raise RuntimeError(
            f"google-cloud-speech is not installed: {_SPEECH_IMPORT_ERROR}"
        )

    project_id = project_id or _resolve_project_id()
    if not project_id:
        raise RuntimeError(
            "Missing Google Cloud project id. Set GCP_PROJECT_ID or GOOGLE_APPLICATION_CREDENTIALS."
        )
    if min_speakers < 1 or max_speakers < min_speakers:
        raise ValueError("Invalid diarization range.")

    diarization_langs = [lc for lc in language_codes if lc in DIARIZATION_SUPPORTED_LANGUAGES]
    if not diarization_langs:
        diarization_langs = language_codes[:1]
        logger.warning(
            "Audio: no requested languages support Chirp 3 diarization; falling back to '{}'",
            diarization_langs[0],
        )
    elif len(diarization_langs) > 3:
        diarization_langs = diarization_langs[:3]

    prepared_audio = _prepare_audio_for_recognition(audio_file)
    sa_credentials = _load_service_account_credentials()
    client_kwargs: dict = {
        "client_options": ClientOptions(
            api_endpoint=f"{chirp_region}-speech.googleapis.com"
        ),
    }
    if sa_credentials is not None:
        client_kwargs["credentials"] = sa_credentials
    client = SpeechV2Client(**client_kwargs)
    recognizer = f"projects/{project_id}/locations/{chirp_region}/recognizers/_"

    denoiser_cfg = None
    if enable_denoiser:
        try:
            denoiser_cfg = cloud_speech.DenoiserConfig(denoise_audio=True)
        except AttributeError:
            logger.debug("Audio: DenoiserConfig unavailable in installed google-cloud-speech")

    diarization_config = cloud_speech.SpeakerDiarizationConfig(
        min_speaker_count=max(1, min_speakers),
        max_speaker_count=max(min_speakers, max_speakers),
    )
    config_kwargs: dict = dict(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=prepared_audio.sample_rate_hertz,
            audio_channel_count=prepared_audio.channel_count,
        ),
        language_codes=diarization_langs,
        model="chirp_3",
        features=cloud_speech.RecognitionFeatures(
            enable_automatic_punctuation=True,
            enable_word_time_offsets=True,
            diarization_config=diarization_config,
        ),
    )
    if denoiser_cfg is not None:
        config_kwargs["denoiser_config"] = denoiser_cfg
    config = cloud_speech.RecognitionConfig(**config_kwargs)

    gcs_bucket = _get_gcs_bucket()
    transcript_result: Optional[TranscriptResult] = None

    def _run(cfg) -> TranscriptResult:
        if gcs_bucket:
            res = _recognize_with_batch_chirp3_gcs(
                client=client,
                recognizer=recognizer,
                config=cfg,
                prepared_audio=prepared_audio,
                project_id=project_id,
                gcs_bucket=gcs_bucket,
            )
            if res is not None:
                return res
        return _recognize_chunked_with_chirp3_v2(
            client=client, recognizer=recognizer, config=cfg, prepared_audio=prepared_audio
        )

    try:
        transcript_result = _run(config)
    except Unauthenticated as exc:
        raise RuntimeError(
            "Google auth failed (401) for Speech-to-Text V2. "
            "Verify service-account access and Speech API permissions."
        ) from exc
    except ServiceUnavailable:
        raise RuntimeError(
            "Google Speech service returned 503. Retry later."
        ) from None
    except GoogleAPICallError as exc:
        text = str(exc)
        if "invalid argument" in text.lower() and len(diarization_langs) > 1:
            primary = diarization_langs[0]
            logger.warning(
                "Audio: Chirp 3 rejected language set; retrying with primary language '{}'",
                primary,
            )
            retry_kwargs = {**config_kwargs, "language_codes": [primary]}
            retry_config = cloud_speech.RecognitionConfig(**retry_kwargs)
            transcript_result = _run(retry_config)
        else:
            raise RuntimeError(f"Speech V2 Chirp 3 recognition failed: {exc}") from exc
    finally:
        if prepared_audio.cleanup_path and os.path.exists(prepared_audio.cleanup_path):
            try:
                os.remove(prepared_audio.cleanup_path)
            except OSError:
                pass

    if transcript_result is None:
        raise RuntimeError("Speech recognition did not return a response.")
    if transcript_result.words:
        timed_turns = _build_timed_turns(transcript_result.words)
        transcript_result.timed_turns = timed_turns
        rebuilt = _render_text_from_words(transcript_result.words)
        if rebuilt:
            transcript_result.raw_transcript = rebuilt
    return transcript_result


# ── Speaker segments + diarization correction ────────────────────────────────
def _build_speaker_segments(transcript_result: TranscriptResult) -> list[SpeakerSegment]:
    if transcript_result.timed_turns:
        return [
            SpeakerSegment(
                speaker_tag=int(turn["speaker_tag"]),
                text=str(turn["text"]),
                start_time=float(turn["start_time"]),
                end_time=float(turn["end_time"]),
            )
            for turn in transcript_result.timed_turns
            if str(turn.get("text", "")).strip()
        ]
    segments: list[SpeakerSegment] = []
    for word_info in transcript_result.words:
        token = word_info.word.strip()
        speaker_tag = int(word_info.speaker_tag or 0)
        if not token or speaker_tag <= 0:
            continue
        if segments and segments[-1].speaker_tag == speaker_tag:
            segments[-1].text = f"{segments[-1].text} {token}".strip()
            segments[-1].end_time = word_info.end_time
        else:
            segments.append(
                SpeakerSegment(
                    speaker_tag=speaker_tag,
                    text=token,
                    start_time=word_info.start_time,
                    end_time=word_info.end_time,
                )
            )
    return segments


def _merge_consecutive_same_speaker(segments: list[SpeakerSegment]) -> list[SpeakerSegment]:
    merged: list[SpeakerSegment] = []
    for s in segments:
        if merged and merged[-1].speaker_tag == s.speaker_tag:
            merged[-1].text = f"{merged[-1].text} {s.text}".strip()
            merged[-1].end_time = s.end_time
        else:
            merged.append(
                SpeakerSegment(
                    speaker_tag=s.speaker_tag,
                    text=s.text,
                    start_time=s.start_time,
                    end_time=s.end_time,
                    inferred_role=s.inferred_role,
                )
            )
    return merged


def _split_utterances(raw_transcript: str) -> list[str]:
    out: list[str] = []
    for line in (raw_transcript or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(r"(?<=[\.\?\!])\s+", line):
            part = part.strip()
            if part:
                out.append(part)
    return out


# Gemini 2.5 Flash / Pro support up to this many output tokens. Use the full
# budget so 40-min and 2-hour calls don't get silently truncated.
_GEMINI_MAX_OUTPUT_TOKENS = 65536
# Hard cap on utterances per Gemini call. With the compact digit-string encoding
# (one char per utterance), 5000 utterances ≈ 5000 chars ≈ ~1500 output tokens —
# fits well under the budget, leaving plenty of headroom for prompt/JSON wrapper.
_GEMINI_DIARIZATION_MAX_UTTERANCES = 5000


def _gemini_full_diarization(
    raw_transcript: str,
    max_utterances: int = _GEMINI_DIARIZATION_MAX_UTTERANCES,
    timed_turns: Optional[list[dict]] = None,
) -> Optional[list[SpeakerSegment]]:
    """Content-based 2-speaker re-diarization using Gemini.

    Primary speaker-assignment path when Chirp 3 BatchRecognize is unavailable
    (the chunked Recognize fallback does not produce reliable cross-chunk
    speaker tags). Uses a compact digit-string encoding (``"1212..."``) so even
    a 2-hour call's assignment list fits well inside the model output budget.

    When ``timed_turns`` is provided, each turn becomes one "utterance" and its
    ``start_time``/``end_time`` are preserved on the resulting segments. This
    avoids the [00:00] timestamp problem when re-diarizing a transcript that
    came back from chunked Recognize. Without ``timed_turns`` the function
    falls back to sentence-splitting the raw text (no timestamps).
    """
    # Build (text, start_time, end_time) tuples per utterance.
    utterance_items: list[tuple[str, Optional[float], Optional[float]]] = []
    if timed_turns:
        for turn in timed_turns:
            text = str(turn.get("text", "")).strip()
            if not text:
                continue
            start = turn.get("start_time")
            end = turn.get("end_time")
            utterance_items.append(
                (
                    text,
                    float(start) if start is not None else None,
                    float(end) if end is not None else None,
                )
            )
    if not utterance_items:
        utterance_items = [(s, None, None) for s in _split_utterances(raw_transcript)]

    if len(utterance_items) < 2:
        logger.info(
            "Audio: Gemini diarization skipped ({} utterances)", len(utterance_items)
        )
        return None
    if len(utterance_items) > max_utterances:
        logger.warning(
            "Audio: Gemini diarization truncating {} utterances to {}",
            len(utterance_items), max_utterances,
        )
    utterance_items = utterance_items[:max_utterances]
    utterances = [u[0] for u in utterance_items]
    last_idx = len(utterances) - 1
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(utterances))

    prompt = (
        "This is a 2-speaker phone-call recording — typically a legal consultation\n"
        "between a CALLER (client/victim seeking help) and a LAWYER (advising).\n"
        "The transcript may be in English, Hindi, or code-switched between them.\n\n"
        "Speaker 1 = LAWYER: asks short clarifying questions, asks for facts /\n"
        "dates / proof, gives professional guidance. Common turns: \"बताइए\",\n"
        "\"ठीक है\", \"कब हुआ\", \"क्या प्रूफ है\", \"WhatsApp कर दीजिए\".\n"
        "Speaker 2 = CALLER: narrates a personal problem in first person; long\n"
        "descriptive turns; emotional language; asks for help.\n\n"
        "CRITICAL — Quoted speech: when one speaker quotes another (\"He said:\n"
        "...\"), the whole sentence is still that speaker. Do not switch the\n"
        "speaker tag inside a quote.\n\n"
        f"Assign EVERY numbered utterance from 0 to {last_idx} to Speaker 1 or 2.\n"
        "Both speakers must appear at least once. Do not invent extra speakers.\n\n"
        "Output format — ONLY this JSON, no commentary:\n"
        f'{{"assignments":"<string of exactly {len(utterances)} digits, each "1" or "2", '
        "indexed by utterance order>\"}\n"
        f'Example for 5 utterances where 0,2,4=Lawyer and 1,3=Caller: {{"assignments":"12121"}}\n\n'
        "Utterances:\n" + numbered
    )

    logger.info(
        "Audio: Gemini diarization running on {} utterances (max output tokens {})",
        len(utterances), _GEMINI_MAX_OUTPUT_TOKENS,
    )
    data = _generate_gemini_json(prompt, max_output_tokens=_GEMINI_MAX_OUTPUT_TOKENS)
    if not data:
        logger.warning("Audio: Gemini diarization failed (empty/invalid response)")
        return None
    assignments_raw = data.get("assignments")
    speaker_map: dict[int, int] = {}

    # Preferred compact form: a digit-string of '1' and '2'.
    if isinstance(assignments_raw, str):
        digits = re.sub(r"[^12]", "", assignments_raw)
        if not digits:
            logger.warning("Audio: Gemini diarization returned empty digit string")
            return None
        for i, ch in enumerate(digits):
            if i > last_idx:
                break
            speaker_map[i] = int(ch)
    # Backward-compat: still accept a list of {"i":N,"s":1} objects.
    elif isinstance(assignments_raw, list):
        for item in assignments_raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("i", item.get("index", -1)))
                spk = int(item.get("s", item.get("speaker", 0)))
            except (TypeError, ValueError):
                continue
            if 0 <= idx <= last_idx and spk in (1, 2):
                speaker_map[idx] = spk
    else:
        logger.warning(
            "Audio: Gemini diarization returned unexpected assignments type: {}",
            type(assignments_raw).__name__,
        )
        return None

    if not speaker_map:
        logger.warning("Audio: Gemini diarization parsed zero valid assignments")
        return None

    prev = 1
    segments: list[SpeakerSegment] = []
    for i, (utt, start_t, end_t) in enumerate(utterance_items):
        spk = speaker_map.get(i, prev)
        prev = spk
        if segments and segments[-1].speaker_tag == spk:
            segments[-1].text = f"{segments[-1].text} {utt}".strip()
            # Extend the trailing timestamp; keep start_time anchored to first turn.
            if end_t is not None:
                cur_end = segments[-1].end_time
                segments[-1].end_time = end_t if cur_end is None else max(cur_end, end_t)
        else:
            segments.append(
                SpeakerSegment(
                    speaker_tag=spk, text=utt, start_time=start_t, end_time=end_t,
                )
            )
    if len({s.speaker_tag for s in segments}) < 2:
        return None
    return segments


def _build_role_prompt(segments: list[SpeakerSegment], max_chars: int = 12000) -> str:
    by_speaker: dict[int, list[str]] = {}
    for seg in segments:
        tag = int(seg.speaker_tag or 0)
        if tag <= 0:
            continue
        by_speaker.setdefault(tag, []).append(seg.text.strip())
    lines: list[str] = []
    for tag in sorted(by_speaker):
        combined = " ".join(p for p in by_speaker[tag] if p).strip()
        if not combined:
            continue
        if len(combined) > 3500:
            combined = f"{combined[:3500]} ... [truncated]"
        lines.append(f"Speaker {tag}: {combined}")
    blob = "\n".join(lines).strip()
    if len(blob) > max_chars:
        blob = f"{blob[:max_chars]} ... [prompt-truncated]"
    return (
        "You are classifying speakers in a recorded phone call between a "
        "client/victim (the caller, seeking legal help) and a lawyer (the "
        "advisor on the receiving end). Use the content of each turn — "
        "questions about facts, timelines, prior incidents typically come from "
        "the lawyer; descriptions of what happened, requests for help, and "
        "emotional context typically come from the client.\n"
        "Roles: `victim_party` (the caller/client), `lawyer` (the legal "
        "advisor), `unknown` (use only if the content is too sparse to decide).\n"
        "Assign DISTINCT roles when the call is clearly two-party — do not "
        "label both speakers with the same role.\n"
        'Return JSON only: {"speakers":[{"speaker_tag":1,"role":"victim_party|lawyer|unknown",'
        '"confidence":0-100,"reason":"short reason"}]}\n\n'
        "Transcript:\n" + blob
    )


def _heuristic_speaker_roles(
    segments: list[SpeakerSegment],
) -> Optional[dict[int, str]]:
    """Last-resort role assignment when Gemini role inference fails.

    Heuristic for a 2-party legal call:
    - Lawyer turns are typically SHORTER (clarifying questions) and contain
      MORE question marks than caller turns (which are long, descriptive
      narratives).
    - The caller (= victim_party) is the other speaker.

    Returns ``{speaker_tag: role}`` for exactly 2 speakers, or ``None`` if
    we don't have enough data to decide.
    """
    by_tag: dict[int, list[str]] = {}
    for seg in segments:
        tag = int(seg.speaker_tag or 0)
        if tag <= 0:
            continue
        text = (seg.text or "").strip()
        if text:
            by_tag.setdefault(tag, []).append(text)

    if len(by_tag) != 2:
        return None

    def _score(turns: list[str]) -> tuple[float, float]:
        """Returns (mean_turn_chars, question_density). Lower mean + higher q = lawyer."""
        if not turns:
            return (0.0, 0.0)
        total_chars = sum(len(t) for t in turns)
        mean_chars = total_chars / len(turns)
        questions = sum(t.count("?") for t in turns)
        q_density = questions / max(1, len(turns))
        return (mean_chars, q_density)

    tag_a, tag_b = sorted(by_tag.keys())
    mean_a, q_a = _score(by_tag[tag_a])
    mean_b, q_b = _score(by_tag[tag_b])

    # Need a clear signal — both totals must be non-trivial.
    if mean_a == 0 or mean_b == 0:
        return None

    # Combine the two cues: lawyer = shorter mean turn + more question marks.
    # `score_a` higher ⇒ tag_a is more lawyer-like.
    score_a = (1.0 / mean_a) + q_a
    score_b = (1.0 / mean_b) + q_b
    if abs(score_a - score_b) < 1e-6:
        return None

    if score_a > score_b:
        return {tag_a: "lawyer", tag_b: "victim_party"}
    return {tag_a: "victim_party", tag_b: "lawyer"}


def _infer_speaker_roles_with_gemini(segments: list[SpeakerSegment]) -> Optional[RoleInference]:
    if not segments:
        return None
    speaker_ids = sorted({int(s.speaker_tag) for s in segments if int(s.speaker_tag) > 0})
    if not speaker_ids:
        return None

    logger.info(
        "Audio: role inference running for speakers {}", speaker_ids,
    )
    # Retry on transient Gemini failures. The role-inference call is small
    # and idempotent so retries are cheap; without them a single network
    # hiccup loses role labels for the whole transcript.
    import time as _time
    backoffs = (0.0, 0.5, 2.0)  # 3 attempts: immediate, +0.5s, +2s
    data: Optional[dict] = None
    for attempt, delay in enumerate(backoffs, start=1):
        if delay:
            _time.sleep(delay)
        data = _generate_gemini_json(
            _build_role_prompt(segments, max_chars=12000),
            max_output_tokens=8000,
        )
        if data:
            if attempt > 1:
                logger.info(
                    "Audio: role inference Gemini call succeeded on attempt {}",
                    attempt,
                )
            break
        logger.warning(
            "Audio: role inference Gemini call returned no data (attempt {}/{})",
            attempt, len(backoffs),
        )
    if not data:
        return None

    speakers = data.get("speakers")
    if not isinstance(speakers, list):
        logger.warning(
            "Audio: role inference returned unexpected `speakers` type: {}",
            type(speakers).__name__,
        )
        return None

    role_by_speaker: dict[int, str] = {}
    confidence_by_speaker: dict[int, int] = {}
    evidence_by_speaker: dict[int, list[str]] = {}
    detected_roles: list[str] = []
    for item in speakers:
        if not isinstance(item, dict):
            continue
        try:
            tag = int(item.get("speaker_tag"))
        except (TypeError, ValueError):
            continue
        if tag not in speaker_ids:
            continue
        role = str(item.get("role", "unknown")).strip().lower()
        if role not in {"victim_party", "lawyer", "unknown"}:
            role = "unknown"
        try:
            confidence = max(0, min(100, int(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0
        reason = str(item.get("reason", "")).strip()
        role_by_speaker[tag] = role
        confidence_by_speaker[tag] = confidence
        evidence_by_speaker[tag] = [reason] if reason else []
        if role != "unknown" and role not in detected_roles:
            detected_roles.append(role)

    if not role_by_speaker:
        logger.warning("Audio: role inference returned no usable assignments")
        return None
    logger.info(
        "Audio: role inference produced {} (confidences: {})",
        role_by_speaker, confidence_by_speaker,
    )
    return RoleInference(
        role_by_speaker=role_by_speaker,
        confidence_by_speaker=confidence_by_speaker,
        evidence_by_speaker=evidence_by_speaker,
        detected_roles=detected_roles,
    )


# ── Output formatting ─────────────────────────────────────────────────────────
def _format_mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


def _build_speaker_letter_map(segments: list[SpeakerSegment]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    next_letter_index = 0
    for seg in segments:
        tag = int(seg.speaker_tag or 0)
        if tag in mapping:
            continue
        # A, B, C, ..., Z, AA, AB...
        letter = chr(ord("A") + next_letter_index) if next_letter_index < 26 else f"S{tag}"
        mapping[tag] = letter
        next_letter_index += 1
    return mapping


# `inferred_role` values → user-facing label. Caller = client/victim seeking
# help; Lawyer = legal advisor on the receiving side.
#
# IMPORTANT — role assignment contract:
# When `transcript_result.speaker_source == "gemini_diarization"` the speaker
# tags ALREADY encode roles per the prompt in `_gemini_full_diarization`:
# tag 1 = lawyer, tag 2 = victim_party. We derive `inferred_role` from the tag
# directly in `_sync_transcribe`; do NOT add a second Gemini call to "discover"
# what the diarization prompt has already established.
# For `speaker_source == "diarization"` (Chirp 3 BatchRecognize native) the
# tags are reliable but contain no role info — we still call
# `_infer_speaker_roles_with_gemini`, with a heuristic fallback if Gemini fails.
_ROLE_TO_LABEL = {
    "victim_party": "Caller",
    "lawyer": "Lawyer",
}


def _build_role_label_map(
    segments: list[SpeakerSegment],
    letter_map: dict[int, str],
) -> dict[int, str]:
    """Map ``speaker_tag`` → display label, preferring inferred role when reliable.

    Falls back to ``Speaker A/B`` when role inference is missing OR ambiguous
    (e.g. the model assigned the same role to multiple speakers — a low-confidence
    signal that's safer not to render).
    """
    role_by_tag: dict[int, str] = {}
    for seg in segments:
        tag = int(seg.speaker_tag or 0)
        if tag in role_by_tag:
            continue
        inferred = (seg.inferred_role or "").strip().lower()
        if inferred in _ROLE_TO_LABEL:
            role_by_tag[tag] = inferred

    used_labels = list(role_by_tag.values())
    role_collision = len(used_labels) != len(set(used_labels))
    if role_collision:
        logger.warning(
            "Audio: role-label collision (multiple speakers assigned same role: {}); "
            "falling back to Speaker A/B labels",
            role_by_tag,
        )
    elif not role_by_tag:
        logger.info(
            "Audio: no roles inferred for any speaker — using Speaker A/B labels"
        )

    label_map: dict[int, str] = {}
    for tag, letter in letter_map.items():
        inferred = role_by_tag.get(tag)
        if inferred and not role_collision:
            label_map[tag] = _ROLE_TO_LABEL[inferred]
        else:
            label_map[tag] = f"Speaker {letter}"
    return label_map


# ── Hinglish transliteration ──────────────────────────────────────────────────
# Devanagari Unicode block (Hindi, Marathi, etc. write here). Detecting any
# code-point in this range tells us the segment carries native-script content
# we should transliterate to Roman/Hinglish.
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def _has_devanagari(text: str) -> bool:
    return bool(text) and bool(_DEVANAGARI_RE.search(text))


def _transliterate_segments_to_hinglish(
    segments: list[SpeakerSegment],
) -> list[SpeakerSegment]:
    """Convert Devanagari Hindi text in each segment to Roman / Hinglish.

    Chirp 3 transcribes Hindi audio in Devanagari script ("मैम, मैं बात कर
    रहा हूं"). The product wants Hinglish output ("Maam, main baat kar raha
    hoon") so a lawyer reading the transcript can scan the spoken content
    without needing Devanagari literacy. Already-Roman / English / mixed
    code-switched portions are preserved verbatim — only the Devanagari runs
    are transliterated.

    Implementation: one batched Gemini call over all segments at once.
    Segments are submitted with stable indexes; the model returns the
    transliterated text per index. Falls back to leaving segments as-is on
    any parse / API failure (Devanagari is still readable, so no data loss).

    Mutates the segments in place AND returns them for convenience.
    """
    if not segments:
        return segments

    # Skip the round-trip entirely when no segment carries Devanagari (audio
    # was English-only, or the transcript was already in Roman script).
    if not any(_has_devanagari(seg.text or "") for seg in segments):
        return segments

    if not _gemini_configured():
        logger.warning(
            "Audio: Hinglish transliteration skipped — Gemini not configured; "
            "transcript will retain Devanagari script."
        )
        return segments

    numbered = "\n".join(
        f"{i+1}. {(seg.text or '').strip()}" for i, seg in enumerate(segments)
    )
    prompt = f"""Convert the following spoken-language utterances from Devanagari (Hindi) script to Roman / Hinglish script.

RULES (apply strictly):
1. Preserve MEANING and SPOKEN SOUND — write Hindi words the way they are pronounced, using Roman letters as a Hindi speaker would casually type on a phone (e.g. मैं → main, हूं → hoon, बात → baat, कर रहा → kar raha, ठीक है → theek hai, क्या → kya, नहीं → nahi, मेरी → meri, मतलब → matlab).
2. Words that the speaker said in ENGLISH (already in the source as-is, like "WhatsApp", "yes", "ok", "company", "WhatsApp pe", "consultation", "yes sir", "policy") MUST stay in their original English spelling. Do NOT translate them.
3. Code-switched / Hinglish phrases stay code-switched (e.g. "Within 24 hours" stays as "Within 24 hours"; "ये एंप्लॉयमेंट एग्रीमेंट को" → "yeh employment agreement ko").
4. Numbers, currency (₹2000, ₹7000), and dates stay verbatim — do not translate or rewrite them.
5. Do NOT translate Hindi to English. The output must be the same Hindi (or Hinglish) words, just typed in Roman letters.
6. Preserve punctuation, line breaks, and the order of utterances exactly.

Return ONLY this JSON:
{{"transliterated":["<utterance 1 in Hinglish>","<utterance 2 in Hinglish>", ...]}}

The output array MUST have exactly {len(segments)} entries, in the same order as the numbered list below. If an utterance is already fully in Roman script, return it unchanged.

Utterances:
{numbered}
"""
    data = _generate_gemini_json(prompt, max_output_tokens=_GEMINI_MAX_OUTPUT_TOKENS)
    if not data:
        logger.warning(
            "Audio: Hinglish transliteration Gemini call returned no data; "
            "transcript will retain Devanagari script."
        )
        return segments

    arr = data.get("transliterated")
    if not isinstance(arr, list):
        logger.warning(
            "Audio: Hinglish transliteration response missing 'transliterated' "
            "list (got {}); transcript will retain Devanagari script.",
            type(arr).__name__,
        )
        return segments

    if len(arr) != len(segments):
        logger.warning(
            "Audio: Hinglish transliteration count mismatch (got {} vs {} "
            "segments); applying as much as possible.",
            len(arr), len(segments),
        )

    converted = 0
    for i, seg in enumerate(segments):
        if i >= len(arr):
            break
        new_text = arr[i]
        if not isinstance(new_text, str):
            continue
        new_text = new_text.strip()
        if not new_text:
            continue
        # Sanity guard: if the model returned the original Devanagari, skip
        # so we don't pretend success when nothing changed.
        if _has_devanagari(new_text) and new_text == (seg.text or "").strip():
            continue
        seg.text = new_text
        converted += 1
    logger.info(
        "Audio: Hinglish transliteration applied to {}/{} segments",
        converted, len(segments),
    )
    return segments


def _format_transcript_with_speakers(segments: list[SpeakerSegment]) -> str:
    """Produce ``[MM:SS] <label>: text`` lines for the document_assessments transcript.

    ``<label>`` is ``Caller`` / ``Lawyer`` when role inference succeeded, and
    falls back to ``Speaker A`` / ``Speaker B`` otherwise.
    """
    if not segments:
        return ""
    letter_map = _build_speaker_letter_map(segments)
    label_map = _build_role_label_map(segments, letter_map)
    lines: list[str] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        ts = _format_mmss(seg.start_time or 0.0)
        label = label_map.get(int(seg.speaker_tag or 0), f"Speaker {letter_map.get(int(seg.speaker_tag or 0), '?')}")
        lines.append(f"[{ts}] {label}: {text}")
    return "\n".join(lines)


# ── Sync orchestration ────────────────────────────────────────────────────────
def _sync_transcribe(
    file_path: str,
    *,
    languages: list[str],
    min_speakers: int,
    max_speakers: int,
    chirp_region: str,
    enable_denoiser: bool,
    enable_role_inference: bool,
) -> AudioTranscriptionResult:
    if not os.path.exists(file_path):
        return AudioTranscriptionResult(success=False, error=f"file_not_found: {file_path}")

    if not _SPEECH_AVAILABLE:
        return AudioTranscriptionResult(
            success=False,
            error=f"google-cloud-speech not installed: {_SPEECH_IMPORT_ERROR}",
        )

    duration_seconds: Optional[float] = None
    try:
        # Best-effort duration via ffprobe alternative — wave only handles WAV.
        # We use ffmpeg -i implicitly via _wav_duration_seconds after conversion;
        # at this stage we'll just leave duration_seconds=None for non-WAV inputs.
        if file_path.lower().endswith(".wav"):
            try:
                duration_seconds = _wav_duration_seconds(file_path)
            except Exception:
                duration_seconds = None

        transcript_result = _transcribe_offline_with_diarization(
            audio_file=file_path,
            project_id=None,
            language_codes=languages,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            chirp_region=chirp_region,
            enable_denoiser=enable_denoiser,
        )
    except Exception as exc:
        logger.error("Audio transcription failed: {}", exc)
        return AudioTranscriptionResult(
            success=False, error=f"transcription_failed: {exc}", duration_seconds=duration_seconds
        )

    raw_transcript = (transcript_result.raw_transcript or "").strip()
    if not raw_transcript:
        return AudioTranscriptionResult(
            success=False,
            error="empty_transcript",
            detected_languages=transcript_result.detected_languages,
            speaker_source=transcript_result.speaker_source,
            duration_seconds=duration_seconds,
        )

    segments = _build_speaker_segments(transcript_result)
    if not segments:
        # Single-speaker fallback — preserve content even without diarization.
        segments = [SpeakerSegment(speaker_tag=1, text=raw_transcript)]
        transcript_result.speaker_source = "no_diarization"

    initial_speaker_count = len({s.speaker_tag for s in segments})
    logger.info(
        "Audio: speaker_source={}, raw segments={}, unique speakers={}",
        transcript_result.speaker_source, len(segments), initial_speaker_count,
    )

    # Content-based re-diarization with Gemini. Always preferred when available —
    # the chunked-Recognize fallback path produces unreliable cross-chunk speaker
    # tags (each chunk's local "1"/"2" can drift into fresh global tags), so for
    # any call that didn't come from BatchRecognize we strongly want Gemini to
    # collapse the output back to 2 speakers. ``timed_turns`` carries the original
    # word timestamps so the re-diarized segments don't all collapse to [00:00].
    if enable_role_inference:
        gemini_segs = _gemini_full_diarization(
            raw_transcript, timed_turns=transcript_result.timed_turns
        )
        if gemini_segs and len({s.speaker_tag for s in gemini_segs}) >= 2:
            segments = gemini_segs
            transcript_result.speaker_source = "gemini_diarization"
            logger.info(
                "Audio: Gemini diarization succeeded — {} segments, {} speakers",
                len(segments), len({s.speaker_tag for s in segments}),
            )
        else:
            segments = _merge_consecutive_same_speaker(segments)
    else:
        segments = _merge_consecutive_same_speaker(segments)

    # Safety net: a 2-party phone call should never end up with more than 2-3
    # distinct speakers in the final output. If we still have many speakers at
    # this point, the chunked-Recognize alignment failed AND Gemini diarization
    # didn't rescue it. Better to render a single "Speaker" rather than emit a
    # transcript with a dozen+ fake speaker labels.
    final_speakers = {s.speaker_tag for s in segments}
    if len(final_speakers) > 3:
        logger.warning(
            "Audio: detected {} distinct speakers in final output ({}); "
            "collapsing to single speaker (chunked-recognize alignment failed and "
            "Gemini diarization did not produce 2 speakers)",
            len(final_speakers), sorted(final_speakers),
        )
        for seg in segments:
            seg.speaker_tag = 1
            seg.inferred_role = None
        segments = _merge_consecutive_same_speaker(segments)
        transcript_result.speaker_source = "collapsed_no_diarization"

    role_inference: Optional[RoleInference] = None
    if enable_role_inference and segments:
        # When the speaker tags came from `_gemini_full_diarization`, the
        # diarization PROMPT itself has already pinned roles per tag:
        #   tag 1 = Lawyer, tag 2 = Caller (= victim_party).
        # So we can derive `inferred_role` directly from the tag — no second
        # Gemini call needed (and no second Gemini failure mode). This removes
        # the most common cause of "Speaker A / Speaker B" labels: a transient
        # role-inference Gemini hiccup wiping out roles on a perfectly-diarized
        # call.
        if transcript_result.speaker_source == "gemini_diarization":
            implicit_role_by_tag = {1: "lawyer", 2: "victim_party"}
            for seg in segments:
                tag = int(seg.speaker_tag or 0)
                if tag in implicit_role_by_tag:
                    seg.inferred_role = implicit_role_by_tag[tag]
            logger.info(
                "Audio: roles derived from gemini_diarization tags "
                "(tag 1=lawyer, tag 2=caller) — skipping role-inference Gemini call"
            )
        elif len({s.speaker_tag for s in segments if int(s.speaker_tag or 0) > 0}) >= 2:
            # BatchRecognize / native diarization gave reliable tags but no role
            # meaning. Run the second Gemini call to assign roles.
            role_inference = _infer_speaker_roles_with_gemini(segments)
            if role_inference:
                for seg in segments:
                    inferred = role_inference.role_by_speaker.get(seg.speaker_tag)
                    if inferred and inferred != "unknown":
                        seg.inferred_role = inferred
            else:
                # Gemini-based role inference failed AND retries inside the
                # function exhausted. Fall back to a content heuristic so we
                # don't render bare Speaker A / B for a clearly 2-party call.
                heuristic_roles = _heuristic_speaker_roles(segments)
                if heuristic_roles:
                    for seg in segments:
                        role = heuristic_roles.get(int(seg.speaker_tag or 0))
                        if role:
                            seg.inferred_role = role
                    logger.info(
                        "Audio: applied heuristic role inference {}",
                        heuristic_roles,
                    )
        else:
            # Single-speaker source (no_diarization / collapsed_no_diarization).
            # Role labels would be misleading for a single tag — skip.
            logger.debug(
                "Audio: single-speaker source ({}); skipping role inference",
                transcript_result.speaker_source,
            )

    # Devanagari → Hinglish: convert Hindi script to Roman so the transcript
    # is readable for users not literate in Devanagari. English / code-switched
    # portions are preserved verbatim. Runs in-place; on any failure the
    # segments stay in their original script (lossless). Track whether the
    # source had Devanagari so we know to rebuild raw_transcript afterwards.
    original_had_devanagari = _has_devanagari(raw_transcript) or any(
        _has_devanagari(s.text or "") for s in segments
    )
    segments = _transliterate_segments_to_hinglish(segments)

    formatted = _format_transcript_with_speakers(segments)

    seg_dicts: list[dict[str, Any]] = []
    letter_map = _build_speaker_letter_map(segments)
    label_map = _build_role_label_map(segments, letter_map)
    for seg in segments:
        d = asdict(seg)
        tag = int(seg.speaker_tag or 0)
        d["speaker_label"] = label_map.get(tag, f"Speaker {letter_map.get(tag, '?')}")
        seg_dicts.append(d)

    # If the source had Devanagari, rebuild raw_transcript from the
    # (now-transliterated) segments so downstream consumers — vector store
    # ingestion, Gemini analysis on raw_transcript, summary/strength
    # generation — see the same Hinglish script the FE renders. If
    # transliteration partially failed, the leftover Devanagari segments are
    # carried through verbatim (still readable, no data loss).
    if original_had_devanagari:
        rebuilt_raw = " ".join(
            (s.text or "").strip() for s in segments if (s.text or "").strip()
        ).strip()
        if rebuilt_raw:
            raw_transcript = rebuilt_raw

    return AudioTranscriptionResult(
        success=True,
        transcript=formatted,
        raw_transcript=raw_transcript,
        segments=seg_dicts,
        detected_languages=transcript_result.detected_languages,
        speaker_source=transcript_result.speaker_source or "diarization",
        duration_seconds=duration_seconds,
    )


# ── Public async entry point ──────────────────────────────────────────────────
async def transcribe_audio(
    file_path: str,
    *,
    languages: Optional[list[str]] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    chirp_region: Optional[str] = None,
    enable_denoiser: bool = True,
    enable_role_inference: bool = True,
) -> AudioTranscriptionResult:
    """Transcribe a local audio file with speaker diarization.

    Heavy work runs inside ``asyncio.to_thread`` so the event loop stays
    responsive. Returns an ``AudioTranscriptionResult`` — never raises for the
    common failure modes (empty audio, missing creds, ffmpeg errors, GCP
    failures); the caller should branch on ``result.success`` and fall back to
    ``transcript_status: "failed"`` when False.
    """
    langs = languages or _settings_languages() or DEFAULT_LANGUAGE_CODES
    min_s = int(min_speakers if min_speakers is not None else getattr(settings, "audio_min_speakers", 2))
    max_s = int(max_speakers if max_speakers is not None else getattr(settings, "audio_max_speakers", 6))
    region = (
        chirp_region
        or getattr(settings, "chirp_region", None)
        or DEFAULT_CHIRP_REGION
    )
    return await asyncio.to_thread(
        _sync_transcribe,
        file_path,
        languages=langs,
        min_speakers=min_s,
        max_speakers=max_s,
        chirp_region=region,
        enable_denoiser=enable_denoiser,
        enable_role_inference=enable_role_inference,
    )


def _settings_languages() -> Optional[list[str]]:
    raw = getattr(settings, "audio_languages", None)
    if not raw:
        return None
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [code.strip() for code in str(raw).split(",") if code.strip()]
