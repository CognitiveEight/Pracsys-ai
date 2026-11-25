# SIM-228: Technical design

## Converter — `app/processing/doc_to_docx.py`
`convert_doc_to_docx(data) -> Optional[bytes]`, blocking (called via
`asyncio.to_thread`):
- input already a `.docx` (zip with `word/document.xml`) → returned as is;
- disabled (`TEMPLATE_DOC_CONVERT_ENABLED=false`), no `soffice`/`libreoffice`
  on PATH, empty or > 50 MB input → `None`;
- otherwise, under a module lock (waited for at most the timeout; a busy
  converter returns `None`), in a fresh temp dir (cleanup errors ignored):
  `soffice --headless --norestore --nolockcheck --nodefault
  -env:UserInstallation=file://<tmp>/profile --convert-to "docx:MS Word 2007 XML"
  --outdir <tmp> <tmp>/input.doc`, `HOME=<tmp>`, own session; on
  `TEMPLATE_DOC_CONVERT_TIMEOUT_SECONDS` the process group gets SIGKILL, and
  it is killed after a normal exit too, so no child outlives the call;
- output accepted only if it exists, is ≤ 100 MB and opens with python-docx.
One `doc_to_docx ok|failed status=…` line with sizes and duration.

## Ingest — `DraftingAgent.ingest_template`
- `source_ext` keeps the uploaded type; after download a `.doc` is converted
  and, on success, `content`/`downloaded_bytes` become the `.docx` and
  `ext_lower = "docx"`, so extraction (`_filename_for_extraction(name,
  ext_lower)`), the skeleton upload and profiling all take the `.docx` path.
- Payload: `extension` = `source_ext` (so a later re-download is converted
  again), new `doc_conversion` = `ok` | `failed` | absent.
- Cache: a `.doc` payload with no `doc_conversion` is re-ingested once when
  `_doc_converter_available()`; `hash_stable` is false for it so it is
  re-profiled from structured text. A recorded `failed` is not retried until
  the file changes.
- `needs_doc_conversion(payload)`: `.doc`, no `doc_conversion`, converter
  installed. Used by ingest and by `_apply_global_published`
  (`app/socket/handlers/template_sync.py`), whose "same version and file =
  no-op" shortcut would otherwise never let a cached `.doc` reach ingest; that
  sync's fresh `fileUrl` is used, not the expired cached link.
- `_lazy_backfill_template_skeleton` and the render-path condition accept
  `doc` and convert before upload.

## Container
Runtime stage installs `libreoffice-writer-nogui` next to `antiword`.

## Alternatives rejected
- Backend converts at upload: needs the Node team and LibreOffice there too.
- Reject `.doc` uploads: existing templates break; lawyers re-save files.
- Pure-Python `.doc` parsing: no maintained library keeps layout.
