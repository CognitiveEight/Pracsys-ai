# SIM-173: Global Template Library feed — acceptance criteria

- **Requirements:** `specs/requirements/SIM-173-global-template-library.md`
- **Design:** `specs/technical-design/SIM-173-global-template-library.md`

All checks are in `tests/test_global_templates.py`.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | the service connects | it asks for the global list and the reply is empty, then full, then missing one id | an empty list is accepted; the full set is held; the missing id is no longer offered; an unchanged template is not fetched again | `test_connect_snapshot_replaces_the_set_and_accepts_an_empty_list` |
| AC-2 | a published v1 | `published` is re-sent, then v2 same file, then v3 new file | the re-send does nothing; v2 reuses the extraction; v3 is fetched; v2 stays readable for drafts pinned to it | `test_published_resend_is_a_no_op_and_versions_reuse_the_same_file` |
| AC-3 | a published template | `retired`, then `published` again; a profiled draft is `deleted` | retired is not offered but still resolves; republished is offered again; the deleted draft is erased | `test_retired_and_deleted_pushes` |
| AC-4 | any lawyer | the global library has nothing published, then one template | the lawyer is offered nothing, then exactly the published template; admin drafts and lazily fetched legacy templates are never offered | `test_every_lawyer_is_offered_the_published_global_library_only` |
| AC-5 | an admin uploads a Document Draft | `request_blueprint_proposal` with `scope: "global"` | the reply has `scope: "global"`, no `userId`, the echoed `fileHash`; one profile on upload, a forced one on Re-profile; failure replies `status: "failed"` | `test_global_profiling_reply` |
| AC-6 | any profiling request | `request_blueprint_proposal` arrives without `scope` | it is profiled as a global draft and answered with `scope: "global"`, no `userId` | `test_profiling_request_without_scope_is_global` |
| AC-7 | v4 is current | a draft pinned to v3 is resolved | v3 is read (lazy-fetched if not held) and v4 stays current | `test_draft_keeps_the_version_it_started_from`, `test_drafting_fetches_an_unheld_global_version` |
| AC-8 | a drafting write-back of a v3 payload | it is stored | it updates v3 only and never changes the published status | `test_write_back_goes_to_the_version_it_came_from` |
| AC-9 | a `user_message_request` | it carries `templateScope` and `templateVersion` | both parse | `test_user_message_request_carries_scope_and_version` |
| AC-10 | a snapshot was applied this connection | a pipeline or chat turn warms templates | no new request goes out (a forced one does); `request_template_sync` is never sent | `test_template_sync_is_requested_once_per_connection` |
| AC-11 | a lawyer template absent from the store | a suggested-draft click names it | it is still never resurrected from the stale URL | `test_lawyer_template_click_still_never_resurrects_a_deleted_one` |
| AC-12 | the service is running | `template_sync` or `template_sync_response` arrives | it is not handled; the global events are | `test_per_lawyer_events_are_no_longer_handled` |

## Negative and edge cases
- A same-version re-send is a no-op (AC-2).
- A new version that fails to download keeps the previous version on offer
  (`test_failed_new_version_keeps_the_previous_one`).
- A preset (no file) is stored globally without a download
  (`test_preset_is_stored_globally`).
- An empty `"blueprint": {}` never reaches ingest as an approved blueprint (AC-1).

## Non-functional
- New log lines carry ids, counts, versions and statuses only, with no URLs or
  names (`tests/offline/verify_logging.py` passes).

## Definition of done
- [x] Every AC above has a passing check
- [x] `make lint` and `make test` pass
- [ ] `make e2e` run by a human (handlers changed)
- [ ] Staging checklist in brief §9 run against the backend's staging events (manual: needs the live backend)
- [x] `CHANGELOG.md` updated; payload changes called out
