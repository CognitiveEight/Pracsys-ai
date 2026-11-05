# SIM-173: AI service reads the platform-wide Global Template Library feed

- **Jira:** SIM-173 (sub-task of SIM-58, epic SIM-145)
- **Owner:** AI service (SimplerToday)
- **Status:** draft — for review
- **Contract:** Confluence "AI brief: Global Template Library (SIM-58) — socket
  changes for the AI service", version 2 (AI side agreed 2026-10-29). Backend
  spec: pracsys-be `docs/specs/SIM-58-global-template-library/` (design.md §4–5).
- **Target branch flow:** feature/SIM-173-global-template-library → development → staging → main

## Problem
Templates belong to one lawyer today, and the AI keeps one list per lawyer
(`template_sync`). SIM-58 replaces that with one Global Template Library that
admins publish (versioned) and every lawyer uses. When the switch-over hides
lawyers' own templates, every per-lawyer list arrives empty. Without this
change the AI then suggests no templates at all, and admin uploads are never
profiled, so the Drafting Assistant's category pills stop working.

## Goal
Drafting suggestions, profiling and drafting all work from the one published
global list, for every lawyer in every firm, with no change to the drafting
output.

## Scope
- In:
  - one global template store keyed by template id, holding the current
    published version;
  - `request_global_template_sync` on every (re)connect;
    `global_template_sync_response` replaces the whole set;
  - `global_template_sync` pushes: `published`, `retired`, `deleted`;
  - global profiling: `request_blueprint_proposal` with `scope: "global"`,
    answered with `scope: "global"` and no `userId`;
  - suggestions from published global templates only, never retired ones;
  - drafting with `templateScope` / `templateVersion`: a draft keeps the
    version it started from;
  - removing the per-lawyer events: `template_sync` / `template_sync_response`
    are no longer handled and `request_template_sync` is no longer sent
    (decided 2026-10-29, ahead of the backend's clean-up step).
- Out (explicitly not doing):
  - per-firm or per-lawyer filtering;
  - the knowledge base (`kb_*` events);
  - any change to the drafting output.

## Users and surfaces
`drafting_assistant` panel, drafting chat (`user_message_request` with
`chat_type=DRAFTING_ASSISTANT`), `request_blueprint_proposal` /
`template_blueprint_proposal`, and three new events.

## Constraints
- [x] Changes a socket payload (new events; optional new fields; the global
  proposal has `scope` and no `userId`) — backend told (brief v2)
- [ ] Role-dependent behaviour
- [ ] Touches personal data, PII redaction or the workspace purge
- [x] Adds a paid call (profiling and OCR of global templates) — metered and
  logged as unbilled, like per-lawyer template work today
- [ ] Indian-law output
- [ ] Shared workspaces

## Success measure
On staging (brief §9): connect sends the request and the reply restores the
list; a published template is suggested to a test case; a retired one is not;
a draft started on v3 still renders v3 after v4 is published.

## Open questions
None blocking. With the per-lawyer events removed, this must not reach an
environment before the backend sends the global feed and admins have published:
until then the Drafting Assistant has no templates to suggest.
