---
paths:
  - "app/processing/**/*.py"
---

# Document processing

Full detail: CLAUDE.md "Scanned PDFs", "Spreadsheet evidence is data",
"Audio evidence".

- `process_file_by_extension` routes by extension, then by content sniffing.
  The backend's `filename` has no extension: route with
  `filename_for_extraction`.
- Evidence and chat attachments use `mark_pages=True` (`--- Page N ---`);
  citations depend on the markers.
- OCR changes must not regress PDFs that read today; keep the fallbacks and
  run `tests/test_ocr.py`.
- Spreadsheets: parse once into a `TableSet`; redact by column
  (`redact_tableset`), never `scan_text` over rendered table text.
- PII redaction runs before any model call, vector write or log; only
  `sensitive_fields` keeps full values.
- Blocking work (OCR, rendering) runs in a worker thread so the socket
  heartbeat survives.
