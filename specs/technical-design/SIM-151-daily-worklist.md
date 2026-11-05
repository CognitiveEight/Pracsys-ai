# SIM-151: Daily PracSys worklist — technical design

- **Requirements:** `specs/requirements/SIM-151-daily-worklist.md`
- **Status:** approved by the product owner on 2026-10-23
- **Approver:** product owner

## Summary
A scheduled GitHub Actions job runs a small, tested Python script that reads
Jira SIM with three JQL queries and posts one formatted message to
`#pracsys-daily`. No AI model. Same Slack bot ("PracSys Triage") and Jira
service account as the triage workflow (SIM-133).

## Affected code
| File | Change |
|---|---|
| `scripts/delivery/daily_worklist.py` | **new**: build and post the worklist |
| `tests/test_daily_worklist.py` | **new**: tests first, no network |
| `.github/workflows/daily-worklist.yml` | **new**: schedule + manual run, read-only |
| `tests/test_delivery_workflows.py` | static checks for the new workflow |
| main branch | **new** copy of the workflow file only (GitHub schedules from the default branch, `main`, until an admin changes it) |

Nothing under `app/` changes.

## Event and payload changes
None.

## Design
**Schedule.** `cron: "30 3 * * 1-6"` (09:00 IST, Mon–Sat) plus
`workflow_dispatch`. Job runs only if repo variable `DAILY_ENABLED == 'true'`.
`permissions: contents: read`. Checks out `development`, runs
`python3 scripts/delivery/daily_worklist.py post`. No third-party action
besides checkout (pinned by SHA).

**Queries** (all through Jira REST `POST /rest/api/3/search/jql`, fields
`summary, status, priority, assignee, updated, duedate, customfield_10075,
customfield_10021`):
1. In progress: `project = SIM AND status in ("Spec", "In Progress", "In Review", "QA") AND issuetype != Epic ORDER BY updated ASC` (epics are umbrellas, not daily work; Spec and QA count as active work although Jira files them under the "To Do" category)
2. Next up: `project = SIM AND status = "To Do" AND issuetype != Epic ORDER BY priority DESC, created ASC` (first 10)
3. Blocked and overdue: `project = SIM AND statusCategory != Done AND (Flagged is not EMPTY OR duedate < startOfDay())`

**Message** (Slack markdown). Nothing is ever cut: when the worklist is longer
than 3,900 characters it is posted as several consecutive messages, split
only between sections or between people (2026-10-23, product owner: the
"… and N more" cut hid tickets):
```
PracSys worklist — <Weekday, DD Mon YYYY>

In progress (N)
• <assignee>
  – <SIM-n link> <summary> · <component> · <P> · <status> · <d> days since update
Next up (top 10 of N)
  – <SIM-n link> <summary> · <component> · <P>
Blocked and overdue (N)
  – <SIM-n link> <summary> · <component> · flagged | due <date>
```
Empty sections say "None". Priority shown as P1–P4 (Highest→P1, High→P2,
Medium→P3, Low/Lowest→P4).

**Slack.** `chat.postMessage` to `DAILY_CHANNEL_ID` (repo variable,
`#pracsys-daily` = `C0CBJE73YTU`) as a top-level message. This script is the
only one allowed to post top-level, and only to that channel; the triage
helpers keep their thread-only rule.

**Secrets / variables.** Reuses `SLACK_BOT_TOKEN`, `JIRA_EMAIL`,
`JIRA_API_TOKEN`. New variables: `DAILY_CHANNEL_ID`, `DAILY_ENABLED`. The bot
must be invited to `#pracsys-daily`.

## Checklist against CLAUDE.md
- Payloads, role gate, purge, model calls, gate0, billing: n/a (no service
  code, no model).
- PII: posts only ticket key, summary, component, priority, status, assignee
  display name and dates; never descriptions or comments.
- Secrets: read from env in the script; never printed; dry-run mode prints
  the message only.
- Logging: n/a (script output is the Actions log; no ticket text beyond what
  is posted).

## Risks and rollback
- GitHub starts scheduled runs late (seen: ~30 minutes). Acceptable for a
  morning digest.
- Rollback: set `DAILY_ENABLED` to `false`.

## Test plan
`tests/test_daily_worklist.py` (no network: `_jira` and `_slack` replaced):
grouping by assignee and ordering; top-10 cut; blocked and overdue; empty
sections; priority mapping; long worklists split into several messages with
nothing dropped; posts go only to
`DAILY_CHANNEL_ID`; `--dry-run` prints without posting.
`tests/test_delivery_workflows.py`: gate on `DAILY_ENABLED`, read-only,
pinned checkout, cron at 03:30 UTC Mon–Sat. `make e2e` not needed.

## Decisions
- No AI model: a fixed format is clearer and costs nothing (2026-10-23,
  product owner).
- Separate channel `#pracsys-daily` (product owner).
