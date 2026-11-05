# SIM-174: Shared knowledge base — acceptance criteria

- **Requirements:** `specs/requirements/SIM-174-knowledge-base.md`
- **Design:** `specs/technical-design/SIM-174-knowledge-base.md`

All checks are in `tests/test_knowledge_base.py`.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | an empty shelf | `kb_document_added` arrives | the document is indexed under its id in the shelf (never a case), with title, category and description, and without the link | `test_added_document_is_indexed_under_its_id_without_the_link` |
| AC-2 | a held document | the same `requestId` is re-sent, then the same id with a new `requestId` | the re-send is ignored; the new one replaces the copy (one copy) | `test_resend_is_ignored_and_same_id_replaces` |
| AC-3 | a held document | `kb_document_deleted` arrives; then an unknown id | every copy is erased; the unknown id is a no-op | `test_deleted_document_is_erased_and_unknown_id_is_a_no_op` |
| AC-4 | an ingest still fetching | the document is deleted | nothing is written afterwards | `test_erase_during_fetch_stops_the_ingest` |
| AC-5 | an ingest writing | the document is deleted | the erase waits for the write and then erases it | `test_erase_during_write_waits_then_erases` |
| AC-6 | the store fails | an erase runs | it retries; after the last attempt it raises, escalates and forces a re-read on the next sync | `test_failed_erase_retries_then_escalates` |
| AC-7 | A and B held | `kb_sync` lists B and C | A is erased, C ingested, B not re-fetched | `test_sync_is_a_full_replace` |
| AC-8 | A held, B fetching | `kb_sync` with `documents: []` | both are erased | `test_empty_sync_erases_everything_including_an_ingest_in_flight` |
| AC-9 | A held | `kb_sync` lists A as an invalid item | A is kept, not erased | `test_sync_keeps_a_held_document_whose_item_was_invalid`, `test_invalid_payloads_change_nothing` |
| AC-10 | a lawyer asks a question | the shelf is empty / holds documents / chat use is off | no search / only passages scoring ≥ 6 / nothing | `test_chat_reads_the_shelf_only_when_it_holds_something` |
| AC-11 | a shelf passage in the prompt | the answer cites `[K1]` | the citation is `knowledge_base` with title and page, no annexure or file name; its figures check against the shelf text | `test_knowledge_base_citation_names_the_title_not_an_annexure` |
| AC-12 | shelf passages for a turn | the chat prompt is built | they appear in their own section, labelled `[K#]`, marked as not the case's documents | `test_chat_prompt_shows_the_shelf_apart_from_the_case` |
| AC-14 | a case's documents | its panels are built | the shelf search is described by the case's documents (types, subjects, provisions), not their full text or the objective | `test_case_query_describes_the_case_without_its_text_when_it_can` |
| AC-15 | relevant shelf passages | a panel prompt is built | they come in a block marked as not the case's documents, within budget | `test_panel_block_is_marked_as_not_the_case_and_respects_its_budget` |
| AC-16 | one upload run | Strategy, Analysis Report and Applicable Laws are built | one shelf search serves all three; a shelf change searches again | `test_case_panels_share_one_search_and_an_erase_invalidates_it`, `test_one_upload_run_searches_the_shelf_once_for_all_three_panels` |
| AC-17 | any case | panels are built | only the configured panels read the shelf; fact panels never do; `""` turns it off; a shelf failure never blocks a panel | `test_only_the_configured_panels_read_the_shelf`, `test_a_shelf_failure_never_blocks_a_panel` |
| AC-13 | the service is running | the three events arrive | each is registered and handled | `test_handlers_are_registered`, `test_handlers_add_delete_and_sync` |

## Negative and edge cases
- An expired link (403) indexes nothing and does not raise
  (`test_a_failed_fetch_leaves_nothing_and_does_not_raise`).
- An extraction that yields only a message is not indexed
  (`test_extraction_messages_are_unreadable`).
- Text is redacted before it is stored (`test_text_is_redacted_before_it_is_stored`).
- An unknown category is filed as `other` (`test_unknown_category_is_filed_as_other`).
- The shelf's workspace id can never be a case id (`test_the_shelf_is_never_a_case`).

## Non-functional
- Log lines carry ids and counts only (`tests/offline/verify_logging.py` passes).

## Definition of done
- [x] Every AC above has a passing check
- [x] `make lint` and `make test` pass
- [ ] `make e2e` run by a human (chat handler changed)
- [ ] Staging checklist in brief §9 against the backend's staging events (manual)
- [x] `CHANGELOG.md` updated; payload change called out
- [x] Jira ticket SIM-174 created (template layout)
