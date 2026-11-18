# SIM-211: Draft Word files filed per workspace, short-lived, purged

- **Jira:** SIM-211
- **Status:** draft

## Problem
Every generated draft .docx was uploaded to
`draft/<userId>/<uuid>-<title>.docx` with a signed link valid until 2050:
- the workspace purge never deleted these files, and could not find them,
  because they were filed by user;
- the draft title, which can name the parties, was in the object name;
- a signed link cannot be revoked, so every link ever issued still works.

The backend never reads `downloadUrl`, so nothing depends on these links.

## Goal
Draft files are filed by workspace, carry no title, have a short link, and
are erased by the workspace purge. The old files are removed once, safely.

## Scope
- In: the drafting export upload; the workspace purge; `generate_signed_url`
  losing its 2050 default; a one-off cleanup script for `draft/`.
- Out: rotating the signing key (it would break the backend's links too); a
  storage lifecycle rule on `drafts/` (DevOps).

## Constraints
- [x] Personal data: no title in object names, no names or URLs in logs.
- [x] Workspace purge: the delete must retry and escalate like every other
  purge stage, never fail silently.
- [ ] Socket payload change: none (`downloadUrl` keeps its meaning; the link
  now expires).
- [ ] Paid model call: none.

## Success measure
A purged workspace leaves no object under `drafts/<workspaceId>/`; the
cleanup dry run reports the legacy files; no new object appears under
`draft/`.
