---
name: spec-writing
description: How to write the spec files (requirements, technical design, acceptance criteria) for a Jira issue in the SimplerToday AI service. Use when creating or updating anything under specs/, when running /spec, or when the planner agent is asked to plan a change.
---

# Writing specs for a Jira issue

Specs live under `specs/` and are named by the Jira key:

```
specs/requirements/SIM-<n>-short-name.md         what and why
specs/technical-design/SIM-<n>-short-name.md     how
specs/acceptance-criteria/SIM-<n>-short-name.md  how we prove it
```

Start each from the `_TEMPLATE.md` in its directory. `specs/README.md` says
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
- **Implementation tasks**: an ordered list of small steps, each small enough
  for one commit. They may become Jira Subtasks.
- Status starts as `draft`. Only a human sets `approved`.

## Acceptance criteria

- One row per behaviour: Given / When / Then / Verified by.
- Each row maps to exactly one automated check, or is marked **manual** with
  the reason.
- Always consider the edge cases in the template: duplicate `requestId`,
  stop mid-run, disconnect, `view` collaborator, model error/timeout/block.

## Never

- Paste payloads, case text, party or participant names, phone numbers,
  credentials or customer data. Describe shapes instead:
  `{workspaceId, documentId}`.
- Invent requirements. If the ticket is unclear, list it under
  **Open questions** and leave the design narrow.
