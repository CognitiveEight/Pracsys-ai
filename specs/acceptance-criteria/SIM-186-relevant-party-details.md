# SIM-186: Relevant Parties shows each party's address, email, phone and masked ID numbers — acceptance criteria

- **Requirements:** `specs/requirements/SIM-186-relevant-party-details.md`
- **Design:** `specs/technical-design/SIM-186-relevant-party-details.md`

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a document stating a party's address, email, phone and masked Aadhaar/PAN | relevant_parties is generated | the party carries them, IDs masked | `tests/offline/verify_relevant_party_details.py::details_extracted` |
| AC-2 | any identifier value, masked or not | the panel is emitted from any path | no more than the last 4 characters' digits are visible | `tests/test_party_details.py::test_mask_*`, `verify_relevant_party_details.py::emit_masks_every_path` |
| AC-3 | the model returns a value not in the party's source document | relevant_parties is generated | the value is dropped | `verify_relevant_party_details.py::invented_values_dropped`, `tests/test_party_details.py::test_verify_*` |
| AC-4 | a lawyer-entered email on a party | the panel regenerates | the lawyer's email is kept | `verify_relevant_party_details.py::user_value_survives_regeneration`, `tests/test_party_details.py::test_merge_*` |
| AC-5 | add/edit with an unmasked ID number | the party is stored and emitted | it is masked | `verify_relevant_party_details.py::edit_masks_identifier` |
| AC-6 | parties with details | a relevant-parties chat context is built | the details appear, IDs masked | `verify_relevant_party_details.py::chat_sees_details` |
| AC-7 | a generation with details | it runs | no address, email, phone or ID value appears in any log line | `verify_relevant_party_details.py::no_values_logged` |
| AC-8 | other panels using the shared party extractor | they run | their prompt is unchanged | `tests/test_party_details.py::test_shared_extractor_prompt_unchanged` |
| AC-10 | a party found in a paged PDF with a known file name | relevant_parties is emitted | it carries `sourceDocId`, `sourceDocName` and `pageNumber` (first-named page); without page markers or a file name those keys are left out; a user-added party has none | `verify_relevant_party_details.py::source_fields`, `tests/test_party_details.py::test_first_mention_page` |
| AC-11 | a party with no address, email, phone or source | relevant_parties is emitted | those keys are left out, never sent as "" (backend contract SIM-184) | `tests/test_party_details.py::test_mask_party_payload_leaves_out_unknown_fields`, `verify_relevant_party_details.py::details_extracted` |
| AC-9 | a real backend and frontend | a fictional document with contact details is uploaded | the panel shows them | **manual** — needs the frontend to render the new fields, and `make e2e` |

## Non-functional
- `tests/offline/verify_logging.py` passes.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run (panel change)
- [ ] `CHANGELOG.md` updated; payload change called out
