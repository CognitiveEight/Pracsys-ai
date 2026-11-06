# SIM-177: Multi-speaker audio diarization — acceptance criteria

- **Requirements:** `specs/requirements/SIM-177-multi-speaker-diarization.md`
- **Design:** `specs/technical-design/SIM-177-multi-speaker-diarization.md`

Unless noted, checks are groups in the new
`tests/offline/verify_multi_speaker_diarization.py`. It uses fake Chirp
results and fake Gemini replies with fictional text only, and makes no billed
calls.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a recording where reliable native diarization finds 3, 5 or 8 speakers | it is transcribed | the transcript has exactly that many labels, `Speaker A`, `Speaker B`, ... in order of first appearance, and no `Caller` / `Receiver` | group `speakers_3_5_8` |
| AC-2 | a two-person call with reliable native tags | it is transcribed | the 2-speaker pass runs as today and the labels are `Caller` / `Receiver` | group `speakers_2` |
| AC-3 | a one-person voice note (native finds 1, the N-speaker pass answers 1) | it is transcribed | one speaker, `Speaker A`; the note is not split | group `speakers_1` |
| AC-4 | native finds 1 but the N-speaker pass finds 2 real speakers | it is transcribed | 2 speakers; roles come from role inference (Caller / Receiver) or neutral letters if that fails | group `speakers_1` |
| AC-5 | reliable native finds 4 and Gemini answers 2 | the correction pass runs | the 4 native speakers are kept; the count never drops | group `correction` |
| AC-6 | reliable native finds 4 and Gemini answers 4 with one turn reassigned | the correction pass runs | the slip is fixed, the count stays 4 and the letters keep native first-appearance order | group `correction` |
| AC-7 | reliable native finds 4 and the Gemini reply reassigns more than 25% of turns | the correction pass runs | the native tags stand | group `correction` |
| AC-8 | a long multi-speaker recording (more utterances than one Gemini call takes) where later blocks number the speakers differently | the N-speaker pass runs in blocks | each person keeps one label across all blocks, and no utterance is dropped | group `blocks` |
| AC-9 | a long recording where one block fails or would push the total above the maximum | the blocked pass runs | it returns no map and the native tags stand | group `blocks` |
| AC-10 | 4 or more final speakers, all with real contributions | the cap runs | none are merged and there is no collapse to one speaker | group `near_silent` |
| AC-11 | 4 speakers, one with under 2% of words and at most 2 turns | the cap runs | that speaker's turns join the neighbouring speaker and 3 labels remain | group `near_silent` |
| AC-12 | a two-person call where one side says very little | the cap runs | it is not merged; 2 speakers remain | group `near_silent` |
| AC-13 | chunked fallback tags (unreliable) with more distinct tags than the maximum, and the Gemini pass fails | the cap runs | the transcript collapses to one speaker with `speaker_source=collapsed_no_diarization` and is still delivered | group `chunked` |
| AC-14 | chunked fallback tags (no GCS bucket) | the N-speaker pass succeeds | its speakers are used (`gemini_multi_diarization`) and the transcription succeeds | groups `chunked`, `no_gcs` |
| AC-15 | Gemini returns the error sentinel, an empty reply or digits out of range | any diarization pass | the native tags are kept and transcription succeeds | group `gemini_failure` |
| AC-16 | BatchRecognize rejects the 8-speaker range (INVALID_ARGUMENT naming speakers or diarization) | recognition runs | it retries once with a maximum of 6, the recording is transcribed, the rejected job is not billed and the GCS temp object is deleted | group `range_retry` |
| AC-17 | INVALID_ARGUMENT unrelated to speakers (for example, file too long) | recognition runs | no speaker-range retry; existing behaviour applies | group `range_retry` |
| AC-18 | the retry at 6 is rejected too | recognition runs | the result is `success=False` (`transcriptStatus: failed`), not a silent chunked run under a refused range | group `range_retry` |
| AC-19 | 3 speakers whose segments all carry inferred roles | labels are built | all three are `Speaker <letter>`; roles are never shown for 3 or more | group `labels_guard` |
| AC-20 | an extraction result listing `Speaker C` to `Speaker H` as parties | `ExtractionMixin._drop_non_party_entities` runs | every one is dropped | group `placeholders` |
| AC-21 | the panel and extraction prompts | they are built | the speaker-placeholder rule covers letters beyond B (`SPEAKER_LABEL_GUARDRAIL`, the parties sub-extractor prompt) | group `placeholders` |
| AC-22 | a multi-speaker audio document | `document_assessments` is built | the audio entry has the same keys and types as today (`transcript`, `transcriptStatus`, ...); only label text differs | group `payload_shape` (builds the entry from a fake processing result) |
| AC-23 | no `GCS_BUCKET_NAME` | a multi-speaker recording is transcribed | transcription succeeds (speaker tags may be less exact) | group `no_gcs` |
| AC-24 | existing long-recording and transliteration behaviour | the suites run | `tests/test_audio_long_recordings.py` and `tests/test_audio_transliteration.py` pass unchanged | `make test` |
| AC-25 | staging with fictional 1-, 2-, 3- and 5-voice recordings | uploaded through the lawyer app | the label counts match, a two-person call shows Caller / Receiver, and labels C to E render in the frontend | **manual**: needs live Speech-to-Text, GCS and the frontend |
| AC-26 | the real service against the local stand-in backend | `make e2e` | passes with no new errors and no leaked names in the log | **manual**: billed run, started by a human |

## Negative and edge cases
- **Duplicate re-send under the same `requestId`:** n/a to this change. Audio
  is transcribed inside the existing ingestion events, whose idempotency is
  unchanged. No new event.
- **Stop (`workspace_stop_request`) mid-run:** the cancel event set by
  `transcribe_audio` stops the worker at the next checkpoint. It is checked
  before each N-speaker block and before the speaker-range retry, so no
  further model or Speech call starts (group `cancel`). Panel settling on
  stop is unchanged.
- **Socket disconnect during generation:** n/a. Transcription emits nothing
  itself. The `document_assessments` delivery and buffering on reconnect are
  unchanged.
- **`view` collaborator on a shared workspace:** n/a. Ingestion is not a chat
  mutation, and nothing here reads `askedBy` or the roster.
- **Model error / timeout / safety block:** the native tags stand, and
  transcription still succeeds (AC-15). A `blocked` reply is not routed to
  another provider.
- An exactly-8-speaker recording does not trip the above-maximum collapse
  (group `speakers_3_5_8`).

## Non-functional
- New log lines carry counts, integer tags, modes and maxima only; never
  transcript text, prompts, model output or names
  (`tests/offline/verify_logging.py` passes).
- Cost: Speech-to-Text is still billed per successful job at the existing
  `record_stt_seconds` site, and diarization model calls go through
  `gemini_client.generate_sync` (`task="audio_diarization"`), so they appear
  in the emit `metadata` as today (group `range_retry` asserts the billing
  count).

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
  (AC-25, AC-26)
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run by a human (ingestion touched)
- [ ] `CHANGELOG.md` updated: changed defaults, new `AUDIO_MAX_SPEAKERS_RETRY`,
  "payload shape unchanged; transcript text may carry Speaker C to H"
- [ ] Frontend confirms labels beyond Speaker B render (requirements, open
  question 2)
