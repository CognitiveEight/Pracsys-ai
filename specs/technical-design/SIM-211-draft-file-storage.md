# SIM-211: Draft file storage — technical design

- **Requirements:** `specs/requirements/SIM-211-draft-file-storage.md`
- **Status:** draft

## Design
- **`app/storage/draft_files.py` (new)** is the only place a draft file is
  written or erased:
  - `upload_draft_docx(workspace_id, bytes)` uploads to
    `drafts/<workspaceId>/<uuid4 hex>.docx` and returns a V2 signed link that
    expires after `DRAFT_DOCX_LINK_TTL_SECONDS` (new setting, default 900,
    the backend's GCS_SIGNED_URL_TTL_MINUTES of 15). It raises when the bucket
    is not configured or the workspace id is missing or unsafe, so a draft is
    never written where the purge cannot reach it.
  - `delete_workspace_draft_files(workspace_id)` lists and deletes the
    prefix and raises on any storage error (not-found counts as deleted).
- **`app/storage/gcs.py`:** `generate_signed_url` requires `expires_at` (no
  2050 default); new `list_blob_names` and `delete_blob_strict`.
- **Drafting export** (`DraftingAgent._export_draft_docx`) calls
  `upload_draft_docx`. The old `_DRAFT_GCS_PREFIX` and `_safe_filename` are
  removed. A failure still returns None, so `docxStatus` is `failed` and the
  Markdown ships.
- **Purge:** `_purge_workspace` stage 6 deletes the draft files after the
  Qdrant deletes, inside the existing retry/escalation; the audit event
  carries `draft_files_deleted`.
- **Cleanup:** `scripts/cleanup_legacy_draft_files.py` lists `draft/`,
  deletes only names matching `draft/<id>/<32 hex>-<name>.docx`, dry run by
  default, `--execute` to delete, prints counts only.

## Checklist against CLAUDE.md
- Purge: new stage raises on failure, so the retry and CRITICAL escalation
  cover it. Module-level code, not a handler cache, so the sweep is not
  involved.
- Logging: counts and `safe_url` only; no titles.
- Role gate, loaders, emits, payloads, model calls: n/a.

## Rollout
1. Deploy. 2. Run the cleanup as a dry run in production and review the
counts. 3. Run `--execute` after sign-off (a person runs it).

## Rollback
Revert the commit. Links issued meanwhile expire on their own.

## Test plan
`tests/test_draft_files.py` (fake GCS client) and the purge group of
`tests/offline/verify_workspace_lifecycle.py`.
