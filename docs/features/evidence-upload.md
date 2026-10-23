---
title: Evidence upload
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Create a workspace and upload evidence
  - Evidence to analysis panels
other_side:
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4554822
related_specs: []
---

# Evidence upload

## In plain words

When a lawyer adds a document to a case, the AI service downloads it and reads
it. Typed documents are read directly. Scanned pages and photos go through text
recognition. Spreadsheets are read as tables, so totals are computed exactly.
Recordings are transcribed, with each speaker labelled. Each document gets a
readability score, a short summary, a document type and an evidence-strength
rating. Identity numbers such as Aadhaar or PAN are masked before any AI model
sees the text. The text is split into small passages and stored, so every
panel and chat answer can quote it and point at the page. If a document reads
badly, the lawyer can ask for it to be read again. It keeps its place in the
case; it is not treated as a new upload.

## Who uses it

- Lawyers, who upload evidence and see a quality score per document.
- Every analysis panel and the AI chat, which read only what this step stored.

## What the user sees

A list of supporting documents, each with a readability score and rating, a
summary, a type and an evidence strength. Recordings also show a transcript. A
poorly read document can be reloaded. See the lawyer app page linked in
`other_side`.

## How it works

1. The backend sends the file list (on a new case, a sync, a version restore or
   a single-document reload). Each file carries its backend id, a signed URL
   and an `extension`. The display name usually has no extension, so the
   `extension` field decides how the file is read.
2. The file is downloaded, with retries on network errors, 5xx and 429. Files
   over 500 MB are refused. Files in a batch are processed one after another.
3. The text is extracted by type:
   - **PDF with a text layer**: read directly, with `--- Page N ---` markers
     and repeated headers and footers removed.
   - **Scanned PDF**: each page is OCR'd (Google Vision), 4 pages at a time.
     The scan's own photo is used when the page is provably just one plain
     JPEG; otherwise the page is rendered at 200 DPI. Reading order is rebuilt
     from word positions on single-column pages. Unreadable or low-confidence
     pages get an `[OCR note: ...]` marker.
   - **Images** (jpg, jpeg, png, gif, bmp, tiff, webp): OCR.
   - **DOCX** (list numbering kept), **DOC** (best-effort), **TXT**, **Markdown**.
   - **XLSX / XLSM**: parsed once into a typed table set (real row numbers,
     column headers and kinds, the sheet's own Total rows). The text opens with
     a whole-workbook summary, then row blocks that restate the sheet and rows.
   - **PPTX**: slide by slide (title, text, tables, chart data, speaker notes),
     one page marker per slide. A slide that is mostly a picture has the
     picture OCR'd.
   - **Audio** (mp3, wav, m4a, ogg, flac, opus, webm, aac): transcribed with
     speaker labels into `[MM:SS] Speaker A: text` lines, which become the
     document's text.
   - **Legacy .xls / .ppt**: rejected with an explicit "unsupported format"
     message, so the score shows a failure.
   - Anything else is identified from its content, and otherwise read as text.
4. A readability score (0-100) is computed from the raw text. A document is
   "readable" at 50 or above. A parsed spreadsheet is lifted to at least 90,
   because its cells were read exactly.
5. PII is masked. Seven identifier types are redacted (Aadhaar, PAN, Udyam,
   passport, voter ID, driving licence, bank account). Spreadsheets are
   redacted column by column, using only each cell's own header as context.
6. One model call classifies the redacted text: summary, evidentiary strength,
   document type and extracted fields.
7. The redacted text is chunked (about 1,800 characters, 15% overlap, none for
   spreadsheets) and stored in the evidence collection with page ranges and
   headings. Spreadsheet rows also go to a separate tables collection for exact
   computation. When the knowledge graph is on, facts are extracted in the
   background. A document with no readable text is stored as a short
   "low-content marker" so later syncs do not re-read it forever.
8. The results are sent as one `document_assessments` panel. Its cached copy
   is the case's document manifest: every feature prompt treats it as the
   ground truth for which documents exist.

### Reloading one document

`document_reload_request` re-reads one document. It is a re-read, not a delete
plus an upload. The document keeps its backend id. The handler resolves the id
from the payload (target file id, then a matching file hash, then storage name
or path, then a unique filename match in the manifest). It never invents one:
an id-less reload is abandoned. Old passages and the manifest entry are removed
before the re-read. If the re-read fails, the document is recorded as removed
evidence. If it succeeds, that record is cleared. All panels are then re-run,
tagged against `currentState` so the lawyer's approved and rejected items keep
their status.

## Rules and limits

- The model only ever sees redacted text. Full identifier values travel only in
  `sensitiveFields`, for the backend's sealed storage, and must not be shown.
- The manifest is merged on every add and pruned on every removal, never
  overwritten.
- Chunk ids are deterministic; re-ingesting a document replaces its chunks.
- Audio needs `ffmpeg`. Full diarization needs `GCS_BUCKET_NAME`; without it,
  transcripts may lack speaker labels.
- A reload re-sent under the same `requestId` is ignored while in flight (up to
  30 minutes) or within 180 seconds of success. A re-send after a failure runs
  again.
- Reload, sync and restore revoke a pending workspace deletion.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| Scan with no readable text | Low score, "Very Poor" or "Poor" rating | Stores a low-content marker; panels see the document exists but has no content |
| Some OCR pages fail | Normal score, lower if many pages fail | Retries a transient Vision failure once; marks the page with an OCR note |
| Legacy .xls or .ppt | Failed score with an "unsupported format" message | Stores nothing usable; asks for .xlsx / .pptx |
| Audio transcription fails | Entry shows transcript status `failed` | Entry collapses to id, name and status; no summary |
| Download fails (network error, 5xx or 429) | Document marked failed with an error | Makes up to 5 attempts, then reports the failure. An expired URL (4xx) is not retried |
| File over 500 MB | Document marked failed | Refuses the download |
| Vector store write fails | Document listed, but answers do not cite it | Logs a warning; `storedInVectorDb` is false |
| Reload with no resolvable id | Panels stay as they were | Abandons the reload and logs why |

## For engineers

### Where the code is

- `app/processing/document_processor.py` — download, extract, score, redact,
  classify, store; `process_batch`.
- `app/processing/file_handlers.py` — routing by extension and content sniff,
  PDF / DOCX / PPTX / text extraction, `filename_for_extraction`.
- `app/processing/ocr.py` — scanned-PDF OCR rules.
- `app/processing/tables.py` — `TableSet`, `redact_tableset`, `render_text`.
- `app/processing/audio_transcription.py` — Chirp 3 transcription and diarization.
- `app/processing/readability.py`, `app/processing/pii_detector.py`,
  `app/processing/document_classifier.py`.
- `app/rag/chunker.py`; `app/vectorstore/qdrant.py` (`add_document_chunks`,
  `replace_document_tables`).
- `app/socket/handlers/documents.py` — manifest envelope, merge, prune, ledger.
- `app/socket/handlers/document_reload.py` — single-document reload.

### Events

Inbound: `workspace_created`, `workspace_sync`, `workspace_version_restore`,
`document_reload_request`. Outbound: `workspace:refresh`, then
`document_feature_response` with `featureType: document_assessments`. See
[the socket contract](../contracts/backend-ai-socket-events.md).

### Data

- Qdrant `<collection>_evidence_v1` (chunks, dense and sparse vectors) and
  `<collection>_tables_v1` (spreadsheet rows, no vectors).
- The manifest and the removed-documents ledger ride the workspace state cache.
- Local staging under `settings.workspace_data_dir`.

### Security

- Logs carry document ids, never file names, text or identifier values
  (`chars()`, `safe_url()`).
- PII is masked before the model call and before storage.
- Signed URLs are never logged in full.

### Tests that guard it

- `tests/test_ocr.py`, `tests/test_tables.py`, `tests/test_office_extraction.py`,
  `tests/test_pdf_boilerplate.py`, `tests/test_audio_long_recordings.py`,
  `tests/test_audio_transliteration.py`, `tests/test_document_wording_and_dates.py`.
- `tests/offline/verify_ingestion.py`, `tests/offline/verify_document_reload.py`,
  `tests/offline/verify_rag.py` (needs a local Qdrant).

### Known gaps

- `_download_file` logs the file name in its success and error lines, while
  the rest of the path logs ids only because names can carry party names.
- Legacy `.doc` goes through a `docx2txt` path that is not built for the binary
  format; how well it reads real .doc files was not verified.
- Files in a batch are processed one at a time; there is no batch-level
  parallelism or overall timeout.
- PII masking covers seven identifier types only. Mobile numbers, names and
  addresses are kept by design.
- No automated test runs `process_document` end to end with the socket handlers.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
