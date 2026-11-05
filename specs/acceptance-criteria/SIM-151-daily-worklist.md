# SIM-151: Daily PracSys worklist — acceptance criteria

- **Requirements:** `specs/requirements/SIM-151-daily-worklist.md`
- **Design:** `specs/technical-design/SIM-151-daily-worklist.md`

| # | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | tickets In Progress and In Review for two people and one unassigned | the worklist is built | they are grouped by assignee (Unassigned last), oldest update first, each with component, P-priority, status and days since update | `tests/test_daily_worklist.py::test_in_progress_grouped_by_assignee` |
| AC-2 | 14 To Do tickets of mixed priority | the worklist is built | "Next up" shows 10, highest priority first, and "top 10 of 14" | `tests/test_daily_worklist.py::test_next_up_top_ten` |
| AC-3 | a flagged ticket and an overdue ticket | the worklist is built | both appear under "Blocked and overdue" with "flagged" / "due <date>" | `tests/test_daily_worklist.py::test_blocked_and_overdue` |
| AC-4 | no tickets in a section | the worklist is built | that section says "None" | `tests/test_daily_worklist.py::test_empty_sections` |
| AC-5 | very many tickets | the worklist is built | it is split into several messages, each under 3,900 characters, split only between sections or people, and every ticket appears exactly once | `tests/test_daily_worklist.py::test_long_worklist_split_without_dropping` |
| AC-6 | `DAILY_CHANNEL_ID` set | the script posts | each message goes to that channel only, top-level, in order | `tests/test_daily_worklist.py::test_posts_only_to_daily_channel` |
| AC-7 | `--dry-run` | the script runs | the message is printed and nothing is posted | `tests/test_daily_worklist.py::test_dry_run_does_not_post` |
| AC-8 | `DAILY_ENABLED` not `true` | the schedule fires | the job is skipped | `tests/test_delivery_workflows.py::test_daily_job_gated_on_variable` |
| AC-9 | the live set-up | 09:00 IST on a working day | a post appears in `#pracsys-daily` by 09:45 and its counts match the JQL in Jira | **manual**: first live morning |

## Negative and edge cases
- Jira or Slack unreachable: the job fails visibly in Actions; nothing partial
  is posted.
- A ticket summary with Slack special characters (`<`, `>`, `&`) is escaped.

## Non-functional
- No AI model, no paid call. Runs in under a minute.

## Definition of done
All automated rows pass; `make lint` and `make test` green; the workflow is on
`development` and `main`; AC-9 confirmed on the first live morning.
