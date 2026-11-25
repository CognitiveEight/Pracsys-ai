# SIM-228: Drafts from a legacy .doc template follow the template's own layout

- **Jira:** SIM-228 (Bug)
- **Status:** approved (2026-11-25, in conversation: "AI converts with LibreOffice")
- **Target branch flow:** fix/SIM-228-doc-templates → development → staging → main

## Problem
Template ingest stores a Word skeleton only for `.docx`, PDFs and images. A
Word 97-2003 `.doc` gets none, so every draft from it takes the `defaults`
route with generic styling. Its text comes from `antiword` (plain text) or,
where that is missing, a raw scrape with no line breaks, so the AI also loses
its headings, clause numbering and tables. Two of the five reference
templates (a shareholders' agreement, a will) are `.doc`.

## Goal
A `.doc` template is handled exactly like a `.docx`: structured text, a stored
skeleton, and the template routes that keep its layout (with SIM-226).

## Scope
- In: `.doc` → `.docx` conversion with LibreOffice headless at template
  ingest, before text extraction; the lazy skeleton backfill converts too; a
  `.doc` cached before this change is re-ingested once; the image installs
  `libreoffice-writer-nogui`; two settings.
- Out: `.doc` evidence uploads (document processing is unchanged);
  `.docx`/PDF/image templates.

## Non-functional
- One conversion at a time; bounded by a timeout; the process group is killed
  on timeout. A real `.doc` converts in about 1-2 s.
- Without LibreOffice or on any failure, behaviour is exactly as before, with
  one warning log line (sizes and status only, never a filename or text).
- A re-ingested `.doc` is profiled again once (one model call per template).
- Image size grows by about 300 MB.
