# SIM-219: a chat revision of a draft erases the lawyer's editor changes (AI side)

- **Jira:** SIM-219
- **Owner:** product owner (drafting)
- **Status:** draft
- **Target branch flow:** feature/SIM-219-revise-latest-version → development → staging → main

## Problem
A lawyer opens a draft in the editor, changes it, then asks the Drafting
Assistant for a further change in chat. The AI revises its own earlier copy
of the draft (`session_state["last_draft_markdown"]` in
`app/langgraph/drafting_agent.py`), which has never seen the editor changes.
The new version silently drops everything the lawyer wrote by hand.

Under the agreed flow the backend stores every version of a draft as a .docx
(SIM-211 for the AI-built files) and sends the latest one with the revision
request. The AI service has to revise that file.

## Goal
A chat revision is always built from the backend's latest stored version, so
the lawyer's own edits survive. When that version cannot be revised safely
(open tracked changes or comments) or cannot be read, the lawyer is told why
and nothing changes.

## Scope
- In:
  - `user_message_request` (DRAFTING_ASSISTANT) accepts optional `draftId`,
    `baseVersion`, `baseDocumentUrl`;
  - the AI reads `baseDocumentUrl` once (bounded in time and size), checks it
    for open changes per the shared contract list, converts it to the
    drafting Markdown (headings and tables kept) and revises from it;
  - `ai_response` gains `draftDocument.draftId`, `draftDocument.baseVersion`
    and `draftRefusal {code, trackedChanges, comments}`;
  - docs: contract, socket events, drafting feature page.
- Out (explicitly not doing):
  - AI edits written as tracked changes (SIM-220);
  - the `draft_docx_build_request` event (SIM-216);
  - format presets (SIM-217);
  - the backend's storage and versioning of drafts (backend tickets);
  - deciding how ONLYOFFICE marks resolved comments (SIM-214/SIM-215): until
    then every comment counts.

## Users and surfaces
- Drafting Assistant chat: `user_message_request` with
  `chat_type = DRAFTING_ASSISTANT`.
- Outbound `ai_response` (drafting fields).
- The draft .docx at `baseDocumentUrl` (read only, never stored).

## Constraints
- [x] Changes a socket payload: additive, optional inbound and outbound
  fields (above). Turns without them behave exactly as before.
- [ ] Role-dependent behaviour
- [x] Touches personal data: the draft text is case content. It is never
  logged; the link is logged only via `safe_url` and never stored.
- [ ] Adds a paid call (no new model call; a refusal makes none at all)
- [ ] Indian-law output
- [x] Shared workspaces: drafting has no `view` gate today; unchanged (see
  design, Decisions).

## Success measure
- Every turn carrying a base logs one
  `draft_revise_from_base outcome= tracked= comments= chars=` line.
- After release, no lawyer report of an editor change lost to a chat
  revision once the backend sends `baseDocumentUrl`.

## Open questions
- Backend sign-off on the field names and refusal codes (contract proposed
  2026-11-17).
- Does the backend send `baseDocumentUrl` on every drafting turn once v1
  exists, or only on revision turns? The AI handles both.
