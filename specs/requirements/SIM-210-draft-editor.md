# SIM-210: Draft editor — track changes and comments on the AI draft

- **Jira:** SIM-210
- **Owner:** product owner (drafting); implemented by the backend (Node) and frontend teams
- **Status:** draft

## Problem
The "Open editor" screen shows the draft's Markdown. A lawyer cannot:
- strike out text or suggest changes that someone accepts or rejects;
- comment on a passage;
- work with the real Word layout (tables, numbering, letterhead).

So they download the file and do this in Word, outside the product.

## Goal
"Open editor" opens the AI-built Word file in an embedded, Word-compatible
editor with track changes, comments and co-editing. Saving updates the same
.docx, so a download opened in Microsoft Word shows the same changes and
comments.

## Scope
- In:
  - a self-hosted ONLYOFFICE Docs server;
  - the editor embedded in the frontend;
  - the backend: an editor session (JWT-signed config, edit/review/comment/view
    permission per user) and a save callback that stores each new version;
  - read-only access for a `view` collaborator.
- Out (follow-ups):
  - AI revisions delivered as tracked changes;
  - the AI reading the lawyer's comments as instructions;
  - Google Docs or Microsoft 365 integrations.

## Users and surfaces
- Frontend: Drafting Assistant → "Open editor".
- Backend: draft storage, collaborator permissions.
- AI service: unchanged. It already delivers the file as
  `draftDocument.downloadUrl`, with `docxStatus` (SIM-202).

## Constraints
- [x] Personal data: case drafts stay on our own infrastructure (self-hosted;
  no third-party document storage).
- [x] Shared workspaces: a `view` collaborator must get a read-only editor.
- [ ] Socket payload change: none.
- [ ] Paid model call: none.

## Success measure
A lawyer edits with track changes and comments in the app, downloads the
file, and Microsoft Word shows the same tracked changes and comments.

## Open questions
- Licensing: confirm the current ONLYOFFICE Docs Community Edition terms
  (AGPL; limit on simultaneous connections) against expected concurrency,
  and decide when a commercial edition is needed.
- Hosting: the same cluster as the backend, or a separate VM? The document
  server needs enough RAM and CPU for converting documents.
