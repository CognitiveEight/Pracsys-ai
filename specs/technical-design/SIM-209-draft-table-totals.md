# SIM-209: Draft table totals — technical design

- **Requirements:** `specs/requirements/SIM-209-draft-table-totals.md`
- **Status:** draft

## Design
`app/processing/draft_totals.check_table_totals(markdown, fix)` (**new**) is
pure and uses `Decimal` arithmetic.
- **Total rows.** A total row is a row whose first non-numeric cell starts
  with total, grand total, sub-total or aggregate. It totals the rows since
  the previous total row.
- **Which columns are checked.** Only columns whose total cell is a number
  and whose every row is a number, a dash or empty. A placeholder or text
  anywhere skips the column.
- **Tolerance.** Percentages tolerate half a unit of the last decimal per
  row.
- **Output.** A wrong total becomes `<stated> <check total: X>`, which the
  renderer highlights. With `DRAFT_TOTALS_FIX=true` (**new**, default false)
  it is replaced by X. X keeps the column's own form: prefix, suffix,
  grouping and decimals.
- **Where it runs.** It is called in `_render_and_attach` after the final
  normalisation, so every render, revision and re-translation is checked.
  A failure leaves the draft unchanged.

## Checklist against CLAUDE.md
- Role gate, loaders, emits, caches, purge: n/a.
- Model calls: none. The model never does arithmetic over rows.
- Logging: counts only.

## Rollback
`DRAFT_TOTALS_FIX=false` (the default) only flags. Reverting the commit
removes the check.

## Test plan
`tests/test_draft_totals.py`. No `make e2e` impact beyond the drafting path.
