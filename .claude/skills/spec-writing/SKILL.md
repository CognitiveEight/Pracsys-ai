---
name: spec-writing
description: How to write the spec folder (requirements, technical design, tasks, acceptance criteria, test cases) for a Jira issue in the SimplerToday AI service. Use when creating or updating anything under specs/, when running /spec, or when the planner agent is asked to plan a change.
---

# Writing specs for a Jira issue

Each Jira issue gets one folder under `specs/`, named by its key:

```
specs/SIM-<n>-short-name/
  requirements.md         what and why
  technical-design.md     how
  tasks.md                ordered steps, one commit each
  acceptance-criteria.md  how we prove it
  test-cases.md           the check behind each criterion
```

Copy each file from `specs/_templates/` (same name). `specs/README.md` says
who writes and who approves each file.

## Requirements

- **Problem** from the user's side (the lawyer, or the backend team), not
  the code's.
- **Scope In / Out**: say what is explicitly not being done.
- **Constraints**: tick every box that applies. Each one pulls a rule into
  the design (payload change, role gate, PII/purge, paid call, Indian law,
  shared workspaces).
- **Success measure**: a metric, log line or eval score that exists or will
  exist.

## Technical design

- Name only real files, functions, events, settings and `CacheKinds`. Find
  them with Grep/Glob and read them. Anything that does not exist yet is
  marked **new**.
- **Event and payload changes**: say for each whether it is additive. A
  non-additive change needs a linked backend/frontend issue.
- **Checklist against CLAUDE.md**: answer every line. "n/a" is fine, blank
  is not. For model calls also answer:
  - goes through the router (`gemini_client`), with a stable `task=` route key;
  - works with `GATE0_ENFORCE=true` (no direct provider path);
  - `is_generation_error()` checked; paid calls metered.
- **Risks and rollback**: name the flag or env var that switches it off.
- **Test plan**: which pytest file, which `verify_*.py` group, and whether
  `make e2e` is required.
- Status starts as `draft`. Only a human sets `approved`.

## Tasks

- An ordered list of small steps in `tasks.md`, each small enough for one
  commit that leaves `make lint` and `make test` green.
- Name the files each step touches. A step may become a Jira Subtask; its
  key goes in the Subtask column.

## Acceptance criteria

- One row per behaviour: Given / When / Then / Verified by.
- Each row maps to exactly one automated check, or is marked **manual** with
  the reason.
- Always consider the edge cases in the template: duplicate `requestId`,
  stop mid-run, disconnect, `view` collaborator, model error/timeout/block.

## Test cases

- At least one row in `test-cases.md` per AC: id (`TC-n`), the AC it
  covers, type (unit, offline, e2e, manual), steps, expected result and the
  check as `file::name`.
- `e2e` and `manual` rows say why an automated offline check is not enough.

## Never

- Paste payloads, case text, party or participant names, phone numbers,
  credentials or customer data. Describe shapes instead:
  `{workspaceId, documentId}`.
- Invent requirements. If the ticket is unclear, list it under
  **Open questions** and leave the design narrow.
