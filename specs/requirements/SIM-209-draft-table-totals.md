# SIM-209: Drafts — check table totals in code

- **Jira:** SIM-209 (epic SIM-203)
- **Status:** draft

## Problem
Models add up columns wrongly. A capitalisation table in a real draft
showed 990,000 shares where its rows summed to 1,000,000. Better formatting
cannot catch this, and a lawyer may not notice it.

## Goal
Every total row in a draft's tables is re-added in code before delivery. A
wrong figure is flagged in place, or corrected when configured to.

## Scope
- In: Markdown pipe tables in rendered, revised and re-translated drafts;
  Total, Grand total, Sub-total and Aggregate rows; Indian and international
  grouping; currency prefixes; percentages.
- Out: totals written in prose, and cross-table consistency.

## Constraints
No payload change, no paid call, no personal data in logs (counts only).

## Success measure
The log line `draft_totals wrong= tables= action=` reports every wrong total.
