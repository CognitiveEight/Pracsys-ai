# SIM-211: Draft file storage — acceptance criteria

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a draft is exported | uploaded | the object is `drafts/<workspaceId>/<uuid>.docx`, no title or user id | `test_upload_is_filed_by_workspace_without_title` |
| AC-2 | a draft is exported | linked | the link expires after the configured TTL | `test_link_is_short_lived` |
| AC-3 | any caller | signs a link without an expiry | it fails | `test_signed_url_has_no_default_expiry` |
| AC-4 | a missing or unsafe workspace id | uploaded | refused, nothing written | `test_invalid_workspace_id_is_refused` |
| AC-5 | files in two workspaces and legacy files | one workspace is purged | only its `drafts/` files are deleted | `test_purge_deletes_only_that_workspace` |
| AC-6 | a storage error | purged | the purge raises (and is retried) | `test_purge_raises_on_storage_error` |
| AC-7 | a workspace is purged | the purge runs | draft files are deleted after the vector delete; the audit has the count | `verify_workspace_lifecycle.py` purge group |
| AC-8 | legacy and stray objects under `draft/` | the cleanup dry-runs | nothing is deleted; counts only, no names | `test_cleanup_dry_run_deletes_nothing` |
| AC-9 | the same | the cleanup runs with `--execute` | only legacy-pattern objects are deleted | `test_cleanup_execute_deletes_only_legacy_pattern` |
