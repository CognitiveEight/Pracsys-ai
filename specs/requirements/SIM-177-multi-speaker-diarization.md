# SIM-177: Audio transcripts separate up to 8 speakers instead of forcing every recording into 2

- **Jira:** SIM-177 (Task; labels `ai`, `feature`; no parent epic, no links)
- **Owner:** AI service (SimplerToday)
- **Status:** draft
- **Target branch flow:** feature/SIM-177-multi-speaker-diarization → development → staging → main

## Problem
A lawyer who uploads a recording with three or more people in it (a meeting,
a hearing, a conference call) gets a transcript that pretends only two people
spoke. Words said by a third or fourth person are attributed to one of the two
"sides", so the transcript misstates who said what. That is the one thing a
recording is evidence of. When the speech service does separate more voices,
the service throws that work away: a re-labelling pass that only knows
two-person phone calls overwrites it, and a safety rule turns any recording
with more than three voices into a single unlabelled speaker.

A single-person voice note has the opposite problem. The service always asks
for at least two speakers, so one person's dictation can be split into two
"speakers".

## Goal
An audio transcript shows each distinct voice, from one to eight, under its
own label. A two-person call keeps today's Caller / Receiver labels. A
recording with three or more voices uses neutral labels (Speaker A, B, C...)
in the order they first speak. Nothing downstream mistakes those labels for
parties to the case.

## Scope
- In:
  - recordings with 1 to 8 speakers, short and long (including the windowed
    path for recordings over the batch limit);
  - keep the speech service's own speaker separation when it finds three or
    more voices. The text re-labelling pass may fix obvious slips but never
    lowers the speaker count;
  - the text re-labelling pass supports N speakers, including long recordings
    processed in blocks, with labels kept consistent across blocks;
  - labels: Caller / Receiver stays for two-person calls; three or more
    speakers are ALWAYS neutral (Speaker A, B, C...); a one-person recording
    is one speaker;
  - replace the "collapse to one speaker when there are more than 3" rule with
    a cap that only removes near-silent false speakers. A full collapse only
    happens when the result is beyond the configured maximum;
  - the placeholder rules in panel and extraction prompts cover every Speaker
    letter, not just A and B;
  - settings: `AUDIO_MIN_SPEAKERS` default 1, `AUDIO_MAX_SPEAKERS` default 8,
    with one retry at a lower maximum (6) if the speech service rejects the
    speaker range.
- Out (explicitly not doing):
  - more than 8 speakers;
  - streaming recognition;
  - real names or legal roles (judge, witness, counsel...) for three or more
    speakers. User decision: neutral labels only;
  - recognising the same voice across different recordings (voice
    fingerprinting);
  - frontend changes. The payload shape is unchanged. The frontend team should
    confirm that labels beyond Speaker B render;
  - the optional pyannote voice fallback;
  - re-transcribing audio already held. Existing transcripts change only when
    the document is re-read (reload or re-upload).

## Users and surfaces
- Ingestion of audio evidence on `workspace_created`, `workspace_sync`,
  `workspace_version_restore` and `document_reload_request`, all through
  `process_document` and then `transcribe_audio`.
- `document_feature_response` with `featureType=document_assessments`, audio
  entries: the `transcript` text changes (more labels). `transcriptStatus` and
  the entry's fields do not.
- Every panel and chat turn that reads the transcript as document text, and
  the party extraction behind `strategy_objective` / `analysis_report`, which
  must not list Speaker C and later letters as parties.

## Constraints
- [ ] Changes a socket payload or `featureType`. No: the shape of the
  `document_assessments` audio entry is unchanged; only the label text inside
  `transcript` can now go beyond Speaker B. The frontend is told, but nothing
  has to change.
- [ ] Role-dependent behaviour. No: speaker roles inside a recording (Caller /
  Receiver) are not the workspace role gate, and nothing here reads or sets
  the workspace role.
- [ ] Touches personal data, PII redaction or the workspace purge. No change:
  transcripts were already personal data and are stored, redacted and purged
  exactly as today. No new store, cache or log of transcript text.
- [x] Adds a paid call. The speaker-range retry can run a second
  Speech-to-Text job (only after the first was rejected, and a rejected job
  is not billed). A multi-speaker recording that turns out to have exactly two
  speakers can need one more small role-inference model call. Both are metered
  at the existing call sites; the per-minute price is unchanged.
- [ ] Indian-law output.
- [ ] Shared workspaces.

## Success measure
- The offline suite `tests/offline/verify_multi_speaker_diarization.py`
  passes for 1, 2, 3, 5 and 8 speakers.
- In production logs, the new per-recording decision line (see technical
  design) shows `final_speakers` above 2 for multi-party recordings, and
  `speaker_source=collapsed_no_diarization` becomes rare. Today it fires on
  every recording with 4 or more detected voices.
- `make e2e` (human, billed) passes with no new errors and no leaked names in
  the service log.
- Manual check on staging with fictional recordings of 1, 2, 3 and 5 voices:
  label count matches, and a two-person call still shows Caller / Receiver.

## Open questions
1. Google's Chirp 3 model page gives no speaker maximum; 8 comes from the
   product side. We do not know the exact error text a rejected range
   returns, so the retry is triggered by INVALID_ARGUMENT plus a speaker or
   diarization keyword. A staging run should confirm the text, or confirm
   that 8 is accepted.
2. Frontend: confirm that Speaker C to Speaker H render in the transcript
   view, with no fixed two-colour mapping. No frontend ticket is linked yet.
3. Thresholds for the near-silent cap: proposed as less than 2% of words AND
   at most 2 turns. They need to be checked on real multi-party recordings.
   See the technical design.
4. Should transcripts already held be re-transcribed? This spec says no
   (out of scope); a bulk reload would be a separate, billed ticket.
