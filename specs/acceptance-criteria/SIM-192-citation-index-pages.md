# SIM-192: Panel citations point at the evidence page, not the paper-book index — acceptance criteria

- **Jira:** SIM-192 (bug; ships on the SIM-186 branch as its own commit)

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a party whose details are on a later page of a paper-book with an index | Relevant Parties is emitted | `pageNumber` is the page holding the details | `tests/test_index_pages.py::test_party_cites_the_page_with_its_details_not_the_index` |
| AC-2 | a party with no details, named in the index and in prose | Relevant Parties is emitted | `pageNumber` is the first prose page | `tests/test_index_pages.py::test_party_without_details_cites_its_first_non_index_mention` |
| AC-3 | a dated entry found on the index and on its annexure page | the panel is cited | the annexure page is cited | `tests/test_index_pages.py::test_dated_entry_cites_the_annexure_page_not_the_index` |
| AC-4 | an item found only on the index | the panel is cited | the index page is still cited | `tests/test_index_pages.py::test_item_found_only_in_the_index_still_gets_cited` |
| AC-5 | documents without an index (prose, a balance sheet, an INDEX heading with no page references, dates that look like ranges) | pages are classified | no page is an index, so citations are unchanged | `tests/test_index_pages.py::test_documents_without_an_index_have_none` and the two tests after it |
| AC-6 | the real paper-book from Test Case 5 | it is uploaded and the panel shows the petitioner | the citation is the Memo of Parties page, not Page 1 | **manual** — real case data stays out of the repo; checked locally against the service's own extraction (index 1-3; details page 11) |

## Definition of done
- [ ] AC-1..AC-5 pass; AC-6 checked in the app
- [ ] `make lint` and `make test` pass
- [ ] `CHANGELOG.md` updated
