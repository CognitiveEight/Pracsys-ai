# SIM-192: Panel citations point at the evidence page, not the paper-book index — acceptance criteria

- **Jira:** SIM-192 (bug; ships on the SIM-186 branch as its own commit)

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a party whose details are on a later page of a paper-book with an index | Relevant Parties is emitted | `pageNumber` is the page holding the details | `tests/test_index_pages.py::test_party_cites_the_page_with_its_details_not_the_index` |
| AC-2 | a party with no details, named in the index and in prose | Relevant Parties is emitted | `pageNumber` is the first prose page | `tests/test_index_pages.py::test_party_without_details_cites_its_first_non_index_mention` |
| AC-3 | a dated entry found on the index and on its annexure page | the panel is cited | the annexure page is cited | `tests/test_index_pages.py::test_dated_entry_cites_the_annexure_page_not_the_index` |
| AC-4 | an item found only on the index | the panel is cited | the index page is still cited | `tests/test_index_pages.py::test_item_found_only_in_the_index_still_gets_cited` |
| AC-5 | documents without an index (prose, a balance sheet, an INDEX heading with no page references, dates that look like ranges) | pages are classified | no page is an index, so citations are unchanged | `tests/test_index_pages.py::test_documents_without_an_index_have_none` and the two tests after it |
| AC-7 | a dated entry the generator placed on the index, or could not place | List of Dates / Procedural Timeline resolve its page | the page carrying the date; an unplaceable entry gets no page (never a guessed 1) | `tests/test_index_pages.py::test_anchor_page_skips_the_index_restating_the_date`, `test_anchor_page_never_guesses`, `test_timeline_ignores_a_model_page_that_is_the_index`, `test_timeline_unplaceable_entry_gets_no_page_not_page_one`, `test_list_of_dates_agent_page_skips_the_index_and_never_guesses`, `test_list_of_dates_handler_page_skips_the_index` |
| AC-8 | a chat reply citing an index passage for a date or a party | citations are checked | the citation moves to the page with the date / the party's details; an index-only fact keeps its index citation | `tests/test_index_pages.py::test_chat_citation_of_the_index_moves_to_the_page_with_the_date`, `test_chat_citation_of_the_index_moves_to_the_memo_of_parties`, `test_chat_uncited_date_is_attached_to_the_real_page_not_the_index`, `test_chat_index_only_fact_keeps_its_index_citation` |
| AC-9 | a large workspace with a paper-book | chat retrieves passages | index passages are marked and ranked after real pages | `tests/test_index_pages.py::test_chat_marks_index_passages_for_the_model`, `test_chat_retrieval_ranks_index_chunks_last`, `test_chat_evidence_marks_index_passages_and_drops_them_first` |
| AC-6 | the real paper-book from Test Case 5 | it is uploaded and the panel shows the petitioner | the citation is the Memo of Parties page, not Page 1 | **manual** — real case data stays out of the repo; checked locally against the service's own extraction (index 1-3; details page 11) |

## Definition of done
- [ ] AC-1..AC-5 and AC-7..AC-9 pass; AC-6 checked in the app (parties, List of Dates and a chat question)
- [ ] `make lint` and `make test` pass
- [ ] `CHANGELOG.md` updated
