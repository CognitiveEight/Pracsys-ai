---
title: Rebuild the statute index
type: runbook
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "Before you start"
last_verified: 2026-10-21
jira: SIM-113
risk: low
environments: [local]
---

# Rebuild the statute index

## In plain words

When the AI lists the laws that apply to a case, the service checks each
section it names against an official list of sections for each Act, taken
from India Code, the government's statute website. That list ships with the
code as one file. This runbook rebuilds the file, for example after a new Act
is passed or sections are amended. It has two steps: download the official
sections, then compress them into the file the service reads. Nothing in
production changes until the new file is reviewed, merged and released like
any other code change. The risk is low, but a bad file can wrongly mark real
sections as invalid, so the automated tests and a human review must pass
first.

## Before you start

- A ticket branch (`feature/SIM-<n>-…` or `fix/SIM-<n>-…`) and a working local
  setup with `make test` green.
- The master laws export (`masterLaws*.json`, a JSON list of rows). It is
  git-ignored and not in the repo; get it from the AI team. The scripts never
  modify it.
- Outbound HTTPS to `indiacode.gov.in`. The fetch script waits 0.4 s between
  requests and caches every response.
- `rapidfuzz` installed (it is in `requirements.txt`).
- What the file is: `app/statutes/data/sections.json`, read by
  `get_section_index()` in `app/statutes/register.py`. `check_provision` uses
  it to mark each law row `verified`, `invalid` or `unverified`, and
  `annotate_law_rows` uses it to add `sectionTitle`. Absence of a section means
  `invalid` only for an Act in this index. The current file (index version
  2026-10-01) holds 165 Acts, 12,530 sections and 16 repealed Acts.

## Steps

### 1. Check the Act matches first

```bash
python scripts/fetch_laws_indiacode.py "masterLaws 1.json" --resolve-only
```

Expected: a JSON summary printed, and a report at
`<input dir>/masterLaws.indiacode_report.json`. No laws file is written.
Read the report for Acts that did not match above `--min-score` (default 80)
and for repealed Acts. Acts whose names differ too much are pinned in the
`PINNED` map in the script; add one there if needed.

### 2. Fetch the official sections

```bash
python scripts/fetch_laws_indiacode.py "masterLaws 1.json"
```

Options: `--out <path>` (default `<input dir>/masterLaws.indiacode.json`),
`--report <path>` (default `<input dir>/masterLaws.indiacode_report.json`),
`--cache <dir>` (default `data/laws/indiacode_cache`), `--min-score <0-100>`.

Expected: `written: <out path>`, the summary, and `report: <report path>`.
Responses are cached, so a re-run is offline and a broken run resumes. The
script refuses to overwrite its input.

### 3. Build the index

```bash
python scripts/build_statute_index.py masterLaws.indiacode.json masterLaws.indiacode_report.json
```

Option: `--out <path>` (default `app/statutes/data/sections.json`).

Expected: `<acts> acts, <sections> sections, <repealed> repealed acts -> app/statutes/data/sections.json`.
Only rows fetched from India Code go in (rows marked `export-unverified` are
left out), because only complete Acts can make a missing section `invalid`.
Repealed Acts are listed by name with the Act that repealed them, without
sections. `index_version` is set to today's date.

### 4. Test and review

```bash
python -m pytest -q tests/test_statute_index.py tests/test_statutes.py
make lint && make test
git diff --stat app/statutes/data/sections.json
```

Expected: all green. `tests/test_statute_index.py` pins the BNS, BNSS and BSA
section counts (358, 531, 170), checks that no IPC Act is in the index and that
the file is compact JSON. If an official change moves those counts, update the
test in the same PR and say why.

### 5. Ship it

Commit `app/statutes/data/sections.json` (and any script or test change) on the
ticket branch with a `CHANGELOG.md` entry, and release it as normal
([run-locally-and-release.md](run-locally-and-release.md)). Do not commit the
`masterLaws*.json` files or the cache.

## How to check it worked

- After deploy, the service log shows
  `Statute section index <version> loaded (<n> acts, <n> sections, <n> repealed acts)`
  with the new version.
- In an applicable-laws panel on a test case, a BNS section shows its official
  heading as the section title.

## If something goes wrong / how to undo

- The index is read once per process (cached). A changed file takes effect
  only after a restart, which in practice means a release.
- If the file is missing or unreadable, the service logs a warning and falls
  back to the hand-built seed in `app/statutes/data/register.json`, so far
  fewer sections can be `verified` or `invalid`.
- To undo, revert the commit that changed `sections.json` and release again.

### Known gaps

- The master laws export is not in the repo and its owner and refresh process
  are not documented here.
- The fetch reuses the drop and replace lists from
  `scripts/clean_master_laws.py`. Whether that cleaning script must also be run
  on the export before step 1 is not documented; this page could not confirm
  it.
- No automated check compares a new index with the previous one Act by Act.

## Who to call (roles)

- AI team: runs the rebuild and reviews the diff.
- Tech lead: approves the PR.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
