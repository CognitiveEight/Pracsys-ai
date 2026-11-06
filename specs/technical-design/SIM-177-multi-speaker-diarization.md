# SIM-177: Multi-speaker audio diarization (1 to 8 speakers) — technical design

- **Requirements:** `specs/requirements/SIM-177-multi-speaker-diarization.md`
- **Status:** approved (2026-11-02, by the requester)
- **Approver:** tech lead (AI service)

## Summary
The speech service (Chirp 3 BatchRecognize) is asked for 1 to
`AUDIO_MAX_SPEAKERS` (8) speakers. If it rejects that range, it is asked once
more with a lower maximum (`AUDIO_MAX_SPEAKERS_RETRY`, 6). After recognition,
one decision function chooses whose speaker tags to trust:
- reliable native tags with 3 or more speakers are kept. The Gemini text pass
  may only correct them without changing the count;
- two native speakers keep today's two-person call path unchanged;
- one native speaker, or tags from the unreliable chunked fallback, go
  through a new N-speaker Gemini pass that decides the count itself, within
  1 to max.

The "more than 3 → one speaker" rule is replaced by a cap that merges only
near-silent false speakers, plus a full collapse only above the configured
maximum. Three or more final speakers always get neutral Speaker A, B, C...
labels in order of first appearance; Caller / Receiver stays for exactly two.
Payload shapes do not change.

## Affected code
| File | Change |
|---|---|
| `app/core/config.py` | `audio_min_speakers` 2 → **1**, `audio_max_speakers` 6 → **8**; **new** `audio_max_speakers_retry: int = 6` |
| `env.example` | `AUDIO_MIN_SPEAKERS=1`, `AUDIO_MAX_SPEAKERS=8`, **new** `AUDIO_MAX_SPEAKERS_RETRY=6`, with a one-line comment each |
| `app/processing/audio_transcription.py` | Main change; see Design. Functions touched: `TranscriptResult` (new field), `_batch_recognize_wav`, `_recognize_chunked_with_chirp3_v2`, `_recognize_long_audio_windowed`, `_transcribe_offline_with_diarization`, `_gemini_full_diarization`, `_gemini_diarization_assignments_blocked`, `_gemini_diarization_assignments` (2-speaker prompt kept), `_build_role_label_map`, `_sync_transcribe`, `transcribe_audio` (stale `getattr` defaults 2 / 6). New helpers listed below |
| `app/socket/event_utils.py` | `SPEAKER_LABEL_GUARDRAIL`: name "Speaker A, Speaker B, Speaker C ... (any letter)" instead of only A and B, in both the list and the "NEVER write" sentence |
| `app/prompts/extraction_prompts.py` | Parties sub-extractor placeholder rule (the "SPEAKER / DIARIZATION PLACEHOLDERS" bullet): same widening |
| `app/socket/handlers/extraction.py` | **No code change.** `_SPEAKER_PLACEHOLDER_RE` (`speaker\s*[a-z0-9]?`) already matches Speaker C to Speaker H; the new suite pins that with a check. The three repeated comments that name only "Caller", "Receiver" may be widened (comment only) |
| `tests/offline/verify_multi_speaker_diarization.py` | **New** offline suite (fake Chirp results and fake Gemini; no billed calls) |
| `tests/test_audio_long_recordings.py`, `tests/test_audio_transliteration.py` | Unchanged; must stay green (see "Compatibility with existing fakes") |
| `CHANGELOG.md` | Entry: behaviour change, new env var, changed defaults, "payload shape unchanged; transcript text may carry Speaker C to H" |

Not touched: `app/processing/document_processor.py` (it passes
`speaker_source` and `segments` through as internal fields),
`app/socket/handlers/documents.py` (the `document_assessments` audio entry),
`_align_speakers_across_chunks`, `_build_speaker_letter_map` (already assigns
letters A..Z by first appearance), `_TRANSCRIPT_LINE_RE` and the
transliteration path (both independent of the label text).

## Event and payload changes
None to any event or schema. `document_feature_response`
(`document_assessments`) audio entries keep `transcript` and
`transcriptStatus` with the same types. Only the label text inside
`transcript` (`[MM:SS] <label>: text`) can now be `Speaker C` to `Speaker H`.
That is additive in value, not in shape, so no backend ticket is needed. The
frontend should confirm that the extra labels render (requirements, open
question 2).

`speaker_source` (internal, stored in the processing result as
`audio_speaker_source` and never emitted) gains one **new** value,
`gemini_multi_diarization`. Existing values keep their meaning.

## Design

### 1. Settings and the speaker range sent to Chirp
- `transcribe_audio` reads `audio_min_speakers` (1) and `audio_max_speakers`
  (8). Its `getattr` fallbacks change to the same values.
- `_transcribe_offline_with_diarization` builds `SpeakerDiarizationConfig`
  from them, as today (it already clamps `min_speaker_count >= 1`).
- `_stitch_window_results` is called with `min_speakers=max(2, min_speakers)`
  so that lowering the default to 1 does not change how windows of a long
  recording are matched. With 1, a speaker silent in the first overlap would
  be merged into the only known speaker one window earlier than today.

### 2. Retry at a lower maximum if the range is rejected
Today `_batch_recognize_wav` swallows every non-transient error and returns
`None`. The caller then falls back to chunked Recognize, so a rejected range
would silently land on the unreliable path. Change:
- **new** exception `SpeakerRangeRejected(RuntimeError)`. `_batch_recognize_wav`
  raises it, instead of returning `None`, when `_batch_error_code(exc) == 3`
  (INVALID_ARGUMENT) and the error text contains "speaker" or "diarization"
  (case-insensitive). Detection is a **new** helper,
  `_is_speaker_range_rejection(exc) -> bool`. The GCS temp object is still
  deleted (`finally`), and nothing is billed: `record_stt_seconds` runs only
  after a successful job, as today.
- `_recognize_chunked_with_chirp3_v2`: the per-chunk `except Exception`
  re-raises when `_is_speaker_range_rejection` is true, instead of skipping
  the chunk. Otherwise every chunk would be skipped and the recording would
  come back empty.
- Windowed path: the exception leaves `f.result()`. The existing
  `except BaseException` cancels the other windows and re-raises.
- `_transcribe_offline_with_diarization` wraps its attempt in at most two
  diarization ranges: `(min, max)`, then `(min, min(max, retry))`. The second
  is used only when `audio_max_speakers_retry < audio_max_speakers`. It runs
  `_raise_if_cancelled()` before the retry, logs one warning (the two maxima,
  no text), and rebuilds `config_kwargs` with the new
  `SpeakerDiarizationConfig`. The existing language-set retry (`"invalid
  argument" in text`) stays as it is and applies within either attempt.
  `SpeakerRangeRejected` is not a `GoogleAPICallError`, so it never reaches
  that branch.
- If the retry is rejected too, the run raises `RuntimeError` as today
  (`transcription_failed`, `transcriptStatus: failed`). It does not quietly
  fall back to chunked Recognize under a range we know is refused.

### 3. Marking unreliable native tags
**New** field `TranscriptResult.native_tags_reliable: bool = True`.
- `_recognize_chunked_with_chirp3_v2` sets it to `False`: cross-chunk
  alignment (`_align_speakers_across_chunks`) is the path whose tags drift.
- `_recognize_long_audio_windowed` sets it to `False` if any window fell back
  to chunked Recognize. It needs a **new** flag on the window result; a
  chunked window result already carries `False`, so it is the logical AND
  over results.
- `_batch_recognize_wav` results keep `True`.

### 4. Choosing the speaker tags (in `_sync_transcribe`)
Let `n` be the number of distinct native tags after `_build_speaker_segments`,
`reliable = transcript_result.native_tags_reliable` and `max_s` the maximum
actually used (8, or 6 after a retry). A **new** helper,
`_choose_speaker_segments(...)`, encodes the table below; its result and
`speaker_source` replace today's unconditional Gemini override. It runs only
when `enable_role_inference` is true, as today; otherwise the native segments
are merged as today.

| Case | Gemini pass | Accept its result when | `speaker_source` |
|---|---|---|---|
| `reliable` and `n >= 3` | N-speaker, **exactly `n`** speakers (correction) | it returns exactly `n` speakers, maps one-to-one onto the native tags (majority vote per Gemini tag over utterances) and changes at most `_MAX_NATIVE_CORRECTION_SHARE` (**new** constant, 0.25) of utterances. Otherwise the native tags stand | unchanged (`diarization`) |
| `reliable` and `n == 2` | Today's 2-speaker call pass (`_gemini_diarization_assignments`, prompt unchanged) | as today: it returns 2 speakers | `gemini_diarization` (implicit tag 1 = lawyer, 2 = victim_party, as today) |
| `reliable` and `n == 1` | N-speaker, **1 to `max_s`** | it returns 2 or more speakers that survive the near-silent cap (§5); otherwise one speaker | `gemini_multi_diarization`, or unchanged |
| not `reliable` (chunked) | N-speaker, **1 to `max_s`** (2-speaker call pass instead when `max_s <= 2`; this is the rollback setting) | it returns at least 1 speaker | `gemini_multi_diarization` |
| Gemini fails at any step | — | native tags, merged as today | unchanged |

Under the correction rule, Gemini can fix a turn tagged to the wrong person,
but it can never merge or add speakers. When it is accepted, each corrected
utterance takes the native tag its Gemini speaker maps to, so letters still
follow the native first-appearance order.

`_gemini_full_diarization` gains keyword-only parameters
`speaker_count: Optional[int] = None` (exact) and `max_speakers: int = 2`.
The defaults keep today's behaviour exactly. Its `len(...) < 2 → None` guard
applies only in 2-speaker mode; in N-speaker mode, one speaker is a valid
answer.

**N-speaker prompt.** **New** `_gemini_multi_speaker_assignments(utterances,
*, speaker_count, max_speakers)`, beside the unchanged 2-speaker
`_gemini_diarization_assignments`:
- neutral wording: "a recording of a meeting, hearing or call"; no lawyer or
  caller roles;
- either "there are exactly K speakers" or "between 1 and M speakers; use 1
  if only one person speaks";
- number speakers 1..K in order of first appearance;
- the quoted-speech rule kept from the 2-speaker prompt;
- the same compact JSON: `{"assignments": "<one digit per utterance>"}`.
  Digits 1..8 are still one character each, so the output budget is
  unchanged (`_GEMINI_MAX_OUTPUT_TOKENS`, `_GEMINI_DIARIZATION_MAX_UTTERANCES`).
- Parsing: whitespace and commas are stripped. A digit outside 1..M (or
  1..K), or any other character, rejects the block (returns `None`), instead
  of being stripped and shifting every later index. Fewer digits than
  utterances is tolerated as today (the tail inherits the previous speaker).
  The list form accepts `s` in 1..M.

### 5. Long recordings: N-speaker blocks
`_gemini_diarization_assignments_blocked` keeps its block size and overlap
(`_GEMINI_DIARIZATION_BLOCK_OVERLAP`, 200 utterances) and its
`_raise_if_cancelled()` at the top of every block. The `3 - tag` flip becomes
a **new** pure helper, `_map_block_tags(prev_map, block, overlap_range,
known_count, expected_count)`:
- in the overlap, vote `(block_tag, prev_tag)` pairs and assign greedily by
  vote count, one-to-one: the same method as `_stitch_window_results`;
- a block tag absent from the overlap takes an unclaimed known speaker if the
  known count already equals the expected count (exact mode), otherwise the
  next new tag;
- in range mode, a block may not raise the total above `max_speakers`. A
  block that would is rejected, and the whole map returns `None` (native tags
  stand), the same all-or-nothing rule as today.

For two speakers this reduces exactly to today's flip/no-flip decision. The
block function calls whichever assignment function the mode needs, looked up
at call time (not bound as a default argument), so `monkeypatch.setattr` in
tests still works.

### 6. The near-silent cap (replaces the "> 3 → one speaker" rule)
**New** `_merge_near_silent_speakers(segments) -> tuple[list[SpeakerSegment], int]`,
which runs after §4 and replaces the block at "Safety net: a 2-party phone
call ...":
- runs only when there are 3 or more distinct speakers, so a two-person call
  is never merged down to one;
- a speaker is "near-silent" when its words are less than
  `_NEAR_SILENT_WORD_SHARE` (**new**, 0.02) of all words AND it has at most
  `_NEAR_SILENT_MAX_TURNS` (**new**, 2) turns;
- each of its segments takes the tag of the temporally previous segment, or
  the next one if it is first; then `_merge_consecutive_same_speaker`;
- the loop never merges below 2 speakers, and returns the number merged for
  the log line.

Then, if the distinct count is still above `max_s`, all tags collapse to one
speaker and `speaker_source = "collapsed_no_diarization"`, keeping today's
meaning of that value. This can only happen on the unreliable path, or after a
long-recording stitch that minted extra speakers.

The thresholds are module constants, not settings (smallest change).
Requirements open question 3 asks for tuning on real recordings.

### 7. Roles and labels
- In `_sync_transcribe`, role inference runs only when the final count is
  exactly 2:
  - `gemini_diarization`: implicit roles, as today;
  - native or `gemini_multi_diarization`: `_infer_speaker_roles_with_gemini`,
    then `_heuristic_speaker_roles`, as today.

  With 3 or more speakers, role inference is skipped and `inferred_role` is
  cleared on every segment. With 1, it is skipped as today.
- `_build_role_label_map`: if `len(letter_map) > 2`, return
  `Speaker <letter>` for every tag regardless of `inferred_role`. This
  deterministic guard means three or more speakers are always neutral, even
  if a future caller sets roles.
- Letters come from `_build_speaker_letter_map`: first appearance, A..H for
  8 speakers.

### 8. Logging
One **new** INFO summary line per recording, after §6. It carries only
counts, enums and ids from the logging context:
`Audio: diarization mode={} native_speakers={} reliable={} final_speakers={} near_silent_merged={} source={}`.

The existing per-step lines stay; none of them logs text. The retry warning
logs both maxima. Nothing logs utterances, prompts or model output: the
parse-failure path keeps `chars(text)`, and the N-speaker parser follows it.

### 9. Model calls and cost
- Every Gemini call still goes through `_generate_gemini_json` →
  `_generate_model_text` → `gemini_client.generate_sync(..., task="audio_diarization")`
  (`_TASK_DIARIZATION`), with `is_generation_error()` checked there.
- The number of diarization calls per recording is the same as today: one
  pass, blocked for long recordings. Today the pass runs on every recording;
  for `reliable` and `n >= 3` it now runs as a correction pass.
- New spend:
  - one role-inference call when an N-speaker pass ends at exactly 2 speakers
    (the reliable two-speaker path already makes this call today when the
    Gemini pass fails);
  - after a range rejection, a second BatchRecognize job, billed at the
    unchanged per-minute price through the existing `record_stt_seconds`
    site. The rejected job is not billed.

## Checklist against CLAUDE.md
- **Role gate:** n/a. Nothing reads or writes the workspace role. Speaker
  "roles" (Caller / Receiver) are transcript labels inside one document;
  ingestion already runs after `initialize_workspace_with_role`, unchanged.
- **Long-running work:** n/a for new scheduling. Transcription already runs
  in `asyncio.to_thread` from `process_document`, inside the run's tracked
  task. No new task, cache or emit. The retry adds at most one more
  recognition run within the same pipeline cap
  (`PIPELINE_RUN_TIMEOUT_AUDIO_MAX_SECONDS`), and a rejected range fails fast.
- **Loaders and `CancelledError`:** n/a for loaders (no new
  `workspace:refresh`). Cancellation: `transcribe_audio` already catches
  `asyncio.CancelledError`, sets the cancel event and re-raises. The worker
  thread polls `_raise_if_cancelled()`, kept at:
  - every N-speaker block (in `_gemini_diarization_assignments_blocked`);
  - before the speaker-range retry;
  - per window and per chunk, as today.

  `TranscriptionCancelled` is never caught by the new `except` clauses: the
  range-rejection check matches code 3 only and re-raises everything else
  unchanged.
- **Emits through `self._socket_client` / `_emit_document_feature`:** n/a, no
  new emit.
- **Model calls through the router; `is_generation_error()`:** yes, via
  `gemini_client.generate_sync` with stable `task="audio_diarization"`, so it
  works under `GATE0_ENFORCE=true` (no direct provider path). The error
  sentinel is checked in `_generate_model_text`. A `blocked` result returns
  `None`; the native tags stand and nothing is re-routed.
- **Paid calls metered at the lowest call site:** yes, unchanged.
  `record_stt_seconds` sits in `_batch_recognize_wav` (only after a
  successful job) and per chunk in `_recognize_chunked_with_chirp3_v2`; model
  usage is recorded by the router. The worker runs in a copy of the caller's
  context, so costs land on the case.
- **New caches reached by the purge:** n/a, no new cache or module-level
  state.
- **Ingestion/deletion helpers:** n/a. No new ingestion or deletion path; the
  audio result keeps flowing through `process_document` and the existing
  `_merge_document_assessments` / `add_document_chunks` path.
- **Chat path gated on `mutations_allowed`:** n/a, no chat path.
- **Logging:** counts, tags (integers), modes and maxima only; no transcript
  text, prompts, model output, names or URLs. `tests/offline/verify_logging.py`
  must still pass.

## Risks and rollback
- **Chirp under-splits a two-person call when min is 1.** Mitigation: a
  one-speaker native result goes through the N-speaker pass, which can split
  it. Residual risk: that pass is neutral, so a call it splits gets roles
  from `_infer_speaker_roles_with_gemini` rather than the implicit ones.
- **Near-silent cap misattributes a genuine short contribution** (for
  example, a person who says "yes" twice) to the neighbouring speaker.
  Mitigation: conservative thresholds, applied only with 3 or more speakers.
  Tune in review.
- **Long recordings beyond one window** (over `AUDIO_BATCH_WINDOW_SECONDS`):
  `_stitch_window_results` can still map a person who first speaks in a later
  window onto a known speaker who is silent in the overlap. This is unchanged
  from today and out of scope; noted for a follow-up if staging shows it.
- **Unknown rejection text.** If Google's INVALID_ARGUMENT for the range lacks
  the keywords, the retry never fires and BatchRecognize falls back to
  chunked Recognize. That is degraded but still transcribed (`reliable=False`
  path). Confirm on staging (requirements, open question 1).
- **Rollback without a deploy:** set `AUDIO_MIN_SPEAKERS=2` and
  `AUDIO_MAX_SPEAKERS=2`. Chirp is then asked for exactly 2, the chunked path
  uses the 2-speaker call pass (`max_s <= 2`), and anything above 2 collapses.
  That is today's two-person pipeline. Full rollback: revert the PR.

## Test plan
**New offline suite** `tests/offline/verify_multi_speaker_diarization.py`.
It is a plain script in the style of `verify_document_reload.py` and is
picked up by `make test`'s `verify_*.py` loop. Fakes: `TranscriptResult` /
`RecognizedWord` built directly, with `_transcribe_offline_with_diarization`
monkeypatched to return them; `_generate_gemini_json` (or the two assignment
functions) and `_infer_speaker_roles_with_gemini` patched. Settings pinned
and restored. No Speech, GCS or model calls.

| Group | Checks |
|---|---|
| `speakers_1` | one-voice note → one speaker, `Speaker A`, no Gemini split when the N-speaker fake returns 1 |
| `speakers_2` | reliable 2 → 2-speaker pass → Caller / Receiver as today |
| `speakers_3_5_8` | reliable 3 / 5 / 8 → that many labels, `Speaker A`.. in first-appearance order, no Caller / Receiver |
| `correction` | Chirp 4, Gemini says 2 → native 4 kept; Gemini returns 4 with one slip → slip fixed, count 4 |
| `gemini_failure` | Gemini returns `None` / error sentinel → native tags kept |
| `chunked` | unreliable tags (7 drifting tags) → N-speaker result accepted; fake failure plus above-max tags → `collapsed_no_diarization` |
| `near_silent` | 4 speakers with one at 1% of words and 1 turn → 3 speakers; a two-person call is never merged; a 3-speaker recording with a real short speaker above the threshold is kept |
| `blocks` | 12,000 utterances, 4 speakers, every second block permuted → consistent labels; a block that fails → `None` |
| `range_retry` | fake BatchRecognize raises code 3 with a speaker message → second run with max 6 succeeds; no `record_stt_seconds` for the rejected job; non-speaker code 3 → not retried |
| `labels_guard` | `_build_role_label_map` with 3 tags all carrying roles → neutral labels |
| `placeholders` | `ExtractionMixin._drop_non_party_entities` drops `Speaker C` .. `Speaker H`; `SPEAKER_LABEL_GUARDRAIL` mentions letters beyond B |
| `payload_shape` | a fake multi-speaker processing result → the `document_assessments` audio entry has today's keys and types |
| `cancel` | cancel event set → the block loop raises `TranscriptionCancelled` before the next model call |
| `no_gcs` | no bucket → chunked path, still `success=True` |

**Existing suites stay green unchanged:** `tests/test_audio_long_recordings.py`
(including `test_long_transcript_is_not_truncated_and_block_flips_are_reconciled`
and `test_failed_block_falls_back_to_native_tags`, whose fakes take one
positional argument; the 2-speaker path keeps calling
`_gemini_diarization_assignments(block)` with exactly that), and
`tests/test_audio_transliteration.py`. Run `make lint` and `make test`.

**`make e2e` is required** before deploy (ingestion touched). A human runs
it; it is billed. A manual staging check with fictional 1-, 2-, 3- and
5-voice recordings confirms the label counts and the range acceptance.

## Implementation tasks
1. Config: change the defaults, add `audio_max_speakers_retry`, update
   `env.example` and the `getattr` fallbacks in `transcribe_audio`; pass
   `max(2, min_speakers)` to `_stitch_window_results`.
2. Add `TranscriptResult.native_tags_reliable` and set it in the chunked and
   windowed paths.
3. Speaker-range retry: `SpeakerRangeRejected`, `_is_speaker_range_rejection`,
   raise sites in `_batch_recognize_wav` / `_recognize_chunked_with_chirp3_v2`,
   the two-range attempt in `_transcribe_offline_with_diarization`, and the
   `range_retry` group.
4. `_gemini_multi_speaker_assignments` (prompt plus strict parser) and
   `_map_block_tags`; generalise `_gemini_diarization_assignments_blocked`
   and `_gemini_full_diarization` (keyword-only, today's defaults); the
   `blocks` group.
5. `_choose_speaker_segments` and the `_sync_transcribe` rewiring; groups
   `speakers_*`, `correction`, `gemini_failure`, `chunked`.
6. `_merge_near_silent_speakers`, the above-max collapse and the summary log
   line; the `near_silent` group.
7. Roles and labels: the role-inference gate on exactly 2 and the guard in
   `_build_role_label_map`; the `labels_guard` group.
8. Prompt text in `SPEAKER_LABEL_GUARDRAIL` and the extraction prompt; the
   `placeholders` group.
9. `CHANGELOG.md`; run `make lint` and `make test`; hand over for `make e2e`.

## Decisions
Recorded during implementation (2026-11-02):
1. **`TranscriptResult.max_speakers_used`** (new, `Optional[int]`). §4 needs
   "the maximum actually used (8, or 6 after a retry)" in `_sync_transcribe`,
   but `_transcribe_offline_with_diarization` returns only a
   `TranscriptResult`. It records the accepted maximum there; `None` falls back
   to the requested maximum.
2. **Rejection detection also matches a raised `InvalidArgument`.**
   `_batch_error_code` reads `exc.code`, which on a google-api-core exception
   is the HTTP status (400), not the gRPC code. `_is_speaker_range_rejection`
   therefore accepts code 3 *or* `grpc_status_code.name == "INVALID_ARGUMENT"`,
   with the same keyword rule. It never matches `TranscriptionCancelled`.
3. **The chunked path raises `SpeakerRangeRejected`** (wrapping the original)
   rather than re-raising the raw exception. A raw `InvalidArgument` is a
   `GoogleAPICallError` and would otherwise enter the language-set retry.
4. **`_map_block_tags` takes a keyword `max_speakers`** for the range-mode
   limit, which the listed signature had no parameter for. The known speakers
   are read from `prev_map`; the first block keeps its own numbering, so the
   2-speaker pass keeps tag 1 = lawyer. Ties prefer the identity pair, so a
   2-speaker tie does not flip (the old rule flipped only on a strict
   majority). Greedy assignment can differ from the old agree/disagree sum in
   contrived vote splits; the existing block-flip test passes unchanged.
5. **Unreliable tags with `max <= 2`** (rollback setting) take the 2-speaker
   call pass and keep `speaker_source=gemini_diarization`, so the implicit
   roles apply exactly as before, rather than `gemini_multi_diarization`.
6. **One shared dispatcher, `_gemini_assign_utterances`,** serves
   `_gemini_full_diarization` and the correction pass (`_correct_native_tags`),
   which needs per-turn assignments rather than merged segments. The 2-speaker
   path still calls `_gemini_diarization_assignments(block)` with one
   positional argument.
7. **"Survives the near-silent cap"** (reliable, 1 native speaker): the cap
   never merges below 2, so any N-speaker answer of 2 or more is accepted; the
   cap then runs on it like any other result.
