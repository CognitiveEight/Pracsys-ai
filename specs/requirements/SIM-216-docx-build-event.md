# SIM-216: Build a Word file from stored draft text on request

- **Jira:** SIM-216
- **Owner:** product owner (drafting)
- **Status:** draft
- **Target branch flow:** feature/SIM-216-docx-build-event → development → staging → main
- **Depends on:** SIM-202 (normaliser, `docxStatus`, `placeholderCount`),
  SIM-208 (standard layouts), SIM-211 (draft files under
  `drafts/<workspaceId>/`, short-lived links)

## Problem
The Word file of a draft is built only inside the drafting chat, at the
moment the AI writes the draft. Two cases are left without one:

- **The copy failed.** The backend could not copy the AI's file into its own
  storage, or the AI reported `docxStatus: "failed"`. Today the only way to
  get a file is to ask the AI to redraft, which changes the text.
- **Older drafts.** Drafts saved before the AI produced Word files, and
  drafts the lawyer has since edited in the in-app editor (stored as HTML),
  have no Word file at all (SIM-218).

## Goal
The backend can ask the AI for a Word file built from a draft's **current
text**, without changing that text, and always gets an answer it can act on.
The file looks the same as one the drafting chat would build from the same
text: real tables, headings, nested lists, highlighted fill-ins, the
template's formatting or the standard layout for the document type.

## Scope
- In:
  - new inbound event `draft_docx_build_request` and outbound
    `draft_docx_build_response` (contract: section 3 of the drafting
    contract, copied into `docs/contracts/backend-ai-socket-events.md`);
  - Markdown drafts (normalised as SIM-202) and HTML drafts from the in-app
    editor (converted to the same Markdown structure);
  - the drafting chat's renderer and template lookup, reused;
  - idempotency on `requestId`, a time cap, refusal for deleted cases;
  - correcting the contract's SIM-202 wording: the backend copies the file
    into its own encrypted storage; the AI link is a short-lived hand-off.
- Out (explicitly not doing):
  - letterhead / format presets (SIM-217, blocked on the backend schema);
  - revising an uploaded base file and the open-changes check (SIM-219);
  - the backend's side: the "Rebuild Word file" button, the SIM-218
    back-fill job, the copy into its storage.

## Users and surfaces
- Backend → AI: `draft_docx_build_request`.
- AI → backend: `draft_docx_build_response`, and the .docx at its
  `downloadUrl`.
- Lawyer: "Rebuild Word file" on a draft; the Word file of an older draft.

## Constraints
- [x] Changes a socket payload: two **new** events; nothing existing changes.
- [ ] Role-dependent behaviour (the role is only passed to the template
  route, as in the drafting chat)
- [x] Touches personal data and the workspace purge: draft text and title
  are case content and are never logged; files go under the purged prefix;
  a deleted case is refused and its purge is not revoked.
- [x] Adds a paid call: only on the template route, through the existing
  router; billed to the workspace and reported in `metadata`.
- [ ] Indian-law output
- [ ] Shared workspaces (`userId` is the owner; the backend gates who may
  press Rebuild)

## Success measure
- Every request with a readable `requestId` gets exactly one
  `draft_docx_build_response` (more only for a re-send).
- One `draft_docx_build status= reason= format= route=` line per build, so
  failure reasons can be counted after release.

## Open questions
- Backend: confirm the signed-link expiry so `DRAFT_DOCX_LINK_TTL_SECONDS`
  matches (contract section 5).
- Backend: sign off the contract (status "proposed").
- Should a re-send after a **failed** answer re-run rather than replay? The
  contract says replay for 10 minutes; the backend uses a new `requestId`
  for a new attempt.
