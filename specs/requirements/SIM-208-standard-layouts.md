# SIM-208: AI-only drafts in the standard layout of their document type

- **Jira:** SIM-208 (epic SIM-203)
- **Status:** draft

## Problem
Without a template, every draft gets the same generic Word layout. A legal
notice, a court filing, an affidavit, a vakalatnama and an agreement each
have their own conventional look in Indian practice, and the model's
layout list covered no transaction documents (agreements, deeds, wills,
SHAs, PoAs).

## Goal
With no template, a draft is laid out as its document type is usually laid
out, and the model follows a standard structure for that type.

## Scope
- In:
  - layout profiles for notice/letter, court filing, affidavit,
    vakalatnama, agreement/deed (and a default): margins, body size, line
    spacing, alignment, a centred title block, a centred "VERSUS", page
    numbers;
  - the profile inferred from the draft title;
  - layout-list (registry) entries 11–16: rent/lease, will, SHA/addendum/MoU,
    PoA, undertaking/indemnity, other deeds.
- Out:
  - the formatting rules of a specific court (these are conventional
    defaults, not a statement of compliance);
  - templates, which keep their own format (SIM-203).

## Constraints
No payload change and no paid call.

## Success measure
Fictional samples of each family render in their profile (tests).
