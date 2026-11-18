# SIM-220: AI revisions as tracked changes in the lawyer's own file (AI side)

- **Jira:** SIM-220
- **Owner:** product owner (drafting)
- **Status:** draft
- **Depends on:** SIM-219 (revise the latest stored version)
- **Target branch flow:** feature/SIM-220-tracked-change-revisions → development → staging → main

## Problem
Since SIM-219 a chat revision starts from the lawyer's latest .docx, but the
AI returns a clean, newly built Word file. Everything the lawyer formatted by
hand (letterhead, fonts, numbering, spacing) is replaced by the AI's own
layout, and the lawyer cannot see what the AI changed without comparing two
versions by eye.

## Goal
A revision comes back as the lawyer's own file with the AI's edits written as
Word tracked changes, author "SimplerToday AI". The lawyer accepts or rejects
them in the editor (ONLYOFFICE or Microsoft Word) like a colleague's.
Everything the AI did not change stays exactly as the lawyer had it.

## Scope
- In:
  - a deterministic writer: base .docx + base Markdown + revised Markdown →
    the base file with tracked insertions and deletions (words, paragraphs,
    table rows);
  - the drafting revision path uses it when the setting is on and the turn
    revised from `baseDocumentUrl`; any fallback is the SIM-219 clean file;
  - `ai_response.draftDocument.trackedChanges` (additive, optional);
  - one extra instruction in the revision prompt on this path only: copy
    untouched text verbatim;
  - a one-sentence notice to the lawyer when a revision rewrote too much to
    be shown as suggested changes;
  - settings `DRAFT_REVISIONS_AS_TRACKED_CHANGES` (default false) and
    `DRAFT_TRACKED_CHANGES_MAX_CHANGED_RATIO` (default 0.4);
  - the SIM-219 refusal message names the AI's own suggested edits.
- Out (explicitly not doing):
  - formatting-only changes as tracked changes (`w:rPrChange`/`w:pPrChange`);
  - tracked moves, comments written by the AI, or reading comments as
    instructions;
  - edits inside headers, footers, footnotes, text boxes or fields;
  - first drafts, finalize and translate turns (unchanged);
  - backend storage of versions (unchanged; the file is stored like any
    other version).

## Users and surfaces
- Drafting Assistant chat (`user_message_request`, DRAFTING_ASSISTANT) with
  `baseDocumentUrl`.
- Outbound `ai_response.draftDocument` (`downloadUrl`, new `trackedChanges`).

## Constraints
- [x] Changes a socket payload: additive optional `draftDocument.trackedChanges`.
- [ ] Role-dependent behaviour
- [x] Touches personal data: the draft is case content. Never logged; the
  base file is held for the turn only, never stored in the session or Qdrant.
- [ ] Adds a paid call (no new model call)
- [ ] Indian-law output
- [ ] Shared workspaces (unchanged from SIM-219)

## Decided with the user
- The AI's own tracked changes count as open changes (the SIM-219 scanner
  counts every `w:ins`/`w:del`): after one AI revision the lawyer settles the
  suggestions before asking again.
- Deterministic diff; no new model call.
- Off by default until reviewed on real drafts.

## Success measure
- One `draft_tracked_changes outcome=applied|fallback|off ... reason=` line
  per revision; the share of `applied` on real drafts once enabled.
- Reviewed real drafts: accept-all equals the AI's revision, reject-all
  equals the lawyer's version, untouched formatting unchanged.

## Open questions
- Backend/frontend: show `trackedChanges` (e.g. "Review the AI's suggested
  changes") or rely on the editor alone?
- ONLYOFFICE rendering of tracked table-row insertions/deletions needs a
  manual check.
