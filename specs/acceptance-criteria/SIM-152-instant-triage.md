# SIM-152: Instant replies — acceptance criteria

- **Requirements:** `specs/requirements/SIM-152-instant-triage.md`
- **Design:** `specs/technical-design/SIM-152-instant-triage.md`

| # | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a request with a valid Slack signature and fresh timestamp | the relay receives it | it is processed | `tests/test_slack_events_relay.py::test_valid_signature_accepted` |
| AC-2 | a bad signature, a timestamp older than 5 minutes, or missing headers | the relay receives it | 401; no dispatch | `tests/test_slack_events_relay.py::test_bad_or_stale_signature_rejected` |
| AC-3 | Slack's URL verification | the relay receives it | it returns the challenge | `tests/test_slack_events_relay.py::test_url_verification` |
| AC-4 | a person's message (top-level or thread reply) in the triage channel | the relay receives it | exactly one workflow dispatch on `main` | `tests/test_slack_events_relay.py::test_person_message_dispatches` |
| AC-5 | a bot message, a message in another channel, or a join/edit/delete subtype | the relay receives it | 200, no dispatch | `tests/test_slack_events_relay.py::test_ignored_events` |
| AC-6 | ✅ or ❌ added in the triage channel; any other reaction | the relay receives it | dispatch for ✅/❌ only | `tests/test_slack_events_relay.py::test_reactions` |
| AC-7 | a Slack retry (`X-Slack-Retry-Num`) | the relay receives it | 200, no dispatch | `tests/test_slack_events_relay.py::test_retry_ignored` |
| AC-8 | GitHub returns an error or times out | the relay dispatches | it still answers 200 and logs the failure | `tests/test_slack_events_relay.py::test_dispatch_failure_still_200` |
| AC-9 | any request | the relay logs | no message text, user id or name appears in the log | `tests/test_slack_events_relay.py::test_logs_carry_no_message_content` |
| AC-10 | the demo deployment | a question is posted in #pracsys-requests | the answer appears in the thread within 2 minutes, ten times in a row | **manual**: demo |

## Negative and edge cases
- Ten messages within a minute: one or two runs, every message answered
  (workflow concurrency).
- Relay down: the 5-minute schedule still answers (about 30 minutes).

## Definition of done
All automated rows pass; `make lint` and `make test` green; runbook
followed on the demo project; AC-10 confirmed.
