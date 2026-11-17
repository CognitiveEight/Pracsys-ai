# SIM-217: Drafts — apply the lawyer's chosen court or letterhead format

- **Jira:** SIM-217
- **Status:** draft
- **Target branch flow:** feature/SIM-217-apply-formats → development → staging → main

## Problem
The lawyer picks a letterhead or court format when starting a draft. The
backend already sends it on every drafting message as `formatLayout`, but the
AI parses it and ignores it, so the AI-built Word file never carries the
letterhead, the court heading block, the margins or the page numbers. Today
only the browser's own Word export applies formats, and the frontend team
will retire that export once the AI does it.

## Goal
Every draft Word file the AI builds (first draft, revision, translation,
final draft) follows the chosen format faithfully, without losing the draft
when a setting or an image is bad.

## Scope
- In: `formatLayout.formatting.page`, `.letterhead` and `.court` applied to
  the rendered .docx on every export route; `formatLayout` read top level or
  from `draftOrigin.formatLayout`; the latest format kept in the drafting
  session; a formatter that works on any existing .docx (so it can later
  re-apply a format to a lawyer-edited file).
- Out: the "Other format" button that re-applies a format to an edited file
  (later ticket; this provides the function); the draft build event
  (SIM-216); revisions from a base file (SIM-219); fresh short-lived image
  links (backend side of SIM-217).

## Users and surfaces
`user_message_request` with `chat_type: DRAFTING_ASSISTANT`;
`draftDocument.downloadUrl` in `ai_response`.

## Constraints
- [ ] Changes a socket payload or `featureType` — no new field; the existing
  `formatLayout` is now consumed, and `draftOrigin` is accepted as an optional
  opaque field.
- [x] Touches personal data: letterhead text (firm names, addresses) and image
  links. Never logged; image links never stored.
- [ ] Adds a paid call — none (no model call; one image download per image).

## Success measure
One `draft_format applied type= page= watermark= page_number=
letterhead_blocks= court_blocks= images_ok= images_failed= unknown_keys=` log
line per formatted export, with `images_failed=0` once the backend sends
fresh links, and the frontend able to retire its own Word export.

## Open questions
- Some letterhead field names were cut off in the source (`headerSpacing*`,
  footer styling, contact-bar fields). Unknown keys are ignored and reported
  by name; confirm the full list with the frontend.
- The full `pageNumber` value list was cut off; unknown values are ignored.
- The default alignment of a court block with no `align` (centre assumed).
