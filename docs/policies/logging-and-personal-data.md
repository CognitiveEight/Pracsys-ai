---
title: Logging and personal data (AI service)
type: policy
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers from "The rules"
last_verified: 2026-10-21
jira: SIM-113
approved_by: to confirm
review_by: 2027-04-21
applies_to: all code in app/ and scripts/ of the AI service, every environment, and anyone who reads or ships its logs
---

# Logging and personal data (AI service)

## In plain words

The AI service reads privileged legal material: case documents, names of
parties and clients, phone numbers, and what the lawyer wants to achieve. Its
logs must let us trace any problem without containing any of that. So the
logs record what happened, when, to which case by its internal number, how
long it took and what it cost. They record how much text there was, never the
text. Web links are logged without the secret part that some links carry.
Every line of one piece of work carries the same reference number, so
engineers can follow it from start to finish. A separate audit log records
important actions, such as a case being erased, and is kept for a year.
Automated checks enforce most of these rules on every test run.

## What it covers / doesn't cover

Covers:
- Every log call in `app/` (Loguru), the five log files the service writes,
  and the audit and trace records.
- What may appear in log message text and arguments, and in tracebacks.
- How long each log file is kept.

Does not cover:
- What the service sends to the backend or to model providers (see the
  socket contract and `docs/MODEL_PORTABILITY.md`).
- Logs of other systems (backend, gate0, Qdrant, cloud provider logs).
- Erasing logs when a case is deleted. Logs hold ids, not case content, and
  follow the retention below (see [workspace-erasure.md](workspace-erasure.md)).

## The rules

1. **No socket payloads.** Log a payload's keys and counts, never the payload,
   `json.dumps` of it, or `repr` of it. Inbound events log
   `event <name> received (keys: [...])`.
2. **No case content.** Prompts, model output, document text, chat messages,
   user feedback and objective text are never logged, whole or sliced. Log
   their size with `chars(x)` from `app/utils/redact.py`, which writes
   `<N chars>`.
3. **No URLs in full.** Wrap every URL in `safe_url(url)`
   (`app/utils/redact.py`), which keeps scheme, host and path and drops the
   query string. Signed storage URLs carry their credential in the query
   string, some valid for about 25 years.
4. **No personal data.** No party, participant or client names, phone
   numbers, email addresses, case titles or fact values. Internal ids
   (`workspace_id`, `user_id`, `request_id`, `chat_id`, document ids) are
   allowed.
5. **No secrets.** Never log `SOCKET_SECRET_KEY`, API keys or the handshake
   signature. Handshake headers are logged through `redact_auth_headers`
   (signature masked); the timestamp, nonce and signature length may be logged
   because the backend needs them to diagnose a refusal.
6. **No local variables in tracebacks.** `LOG_DIAGNOSE` stays `false` (the
   default) in staging and production. Local variable values are payloads,
   document text and credentials.
7. **Every inbound event is correlated.** Each event runs inside
   `logger.contextualize(request_id, event, workspace_id[, chat_id])`, and
   workspace-scoped work adds `run_epoch`. Spawned tasks inherit it. Never
   re-type ids into message text to make up for a missing context.
8. **Message style.** Loguru `{}` placeholders, never f-strings. No emoji. One
   summary line for a list, not one per item. An exception is logged once with
   `logger.opt(exception=e).error(...)`.
9. **Separate sinks.** Audit records go only to `logs/audit/audit.log`. Trace
   records (`@tracer.trace`) go to `logs/structured.log`, never to `app.log`.
   `setup_logging()` runs once; a second call does nothing, so the audit sink
   cannot be removed by accident.
10. **Retention.**

    | File | Content | Rotation | Kept |
    |---|---|---|---|
    | `logs/app.log` | Text, at `LOG_LEVEL` | 100 MB, zipped | 30 days |
    | `logs/errors.log` | ERROR and above | 50 MB, zipped | 90 days |
    | `logs/structured.log` | JSON, INFO and above, plus trace records | 100 MB, zipped | 30 days |
    | `logs/audit/audit.log` | Audit events only, JSON (when `ENABLE_AUDIT_LOG=true`, the default) | 100 MB, zipped | 1 year |
    | console | At `LOG_LEVEL` | | |

    Audit logs are archived, not dropped, when the service moves.
11. **One line per model call.** The router writes a single `llm_call` line
    (kind, feature, model, status, duration, tokens, cost, gate0 call id). Do
    not add per-call "generating" or "generated" lines.

## How we enforce it

| Rule | Enforced by | Strength |
|---|---|---|
| 1 | `tests/offline/verify_logging.py` static scan: flags `json.dumps(...)`, `repr(data)` / `repr(payload)`, and a whole `data`, `payload`, `envelope` or `response_body` argument | Automated, `make test` |
| 2 | Same scan: flags slices of content-named variables (`raw[:200]`, `prompt[:500]`, `text`, `content`, `answer` and others) and `RAW` dumps | Automated, partial (see Exceptions) |
| 3 | Same scan: flags any argument whose name ends in `url` unless wrapped in `safe_url()` | Automated, by variable name |
| 4 | Same scan: flags identifiers that look like personal data (`party`, `parties`, `party_names`, `case_title`, `phone`, `mobile`, `email`, `client_name`, `full_name`, `person_name`, `participant(s)`), unless counted with `len()` or redacted | Automated, by variable name |
| 5 | `redact_auth_headers` in `app/socket/auth.py`; review | Code + review |
| 6 | `diagnose` set from `LOG_DIAGNOSE` (default `false`) in `app/observability/logger.py` | Config |
| 7 | `SocketEventHandler.handle` in `app/socket/events.py`; `run_control.py`; runtime check in `verify_logging.py` that a line inside an event carries `request_id`, `event` and `workspace_id` | Automated |
| 8 | Static scan flags f-string messages and emoji | Automated (exception style by review) |
| 9 | Runtime checks in `verify_logging.py`: audit sink survives a second `setup_logging()`, audit records stay out of `app.log`, trace records go to `structured.log` | Automated |
| 10 | Sink settings in `app/observability/logger.py` | Code |
| 11 | Router in `app/llm/`; review | Code + review |

`make e2e` also scans the real service log for the fictional party name used
in its test case, which catches a name leaking through any path.

The `reviewer` and `security-auditor` agents check new log calls against
these rules on every `/review`.

## Exceptions and approvals

- `LOG_DIAGNOSE=true` may be set only on a local machine with fictional data,
  or briefly in staging with the tech lead's approval and fictional data only.
  Never in production.
- Known limits of the automated checks, covered only by review:
  - a bare content variable (not sliced) is not flagged by name;
  - a personal-data value held under a neutral variable name is not caught;
  - the contents of audit records (`audit_logger.log_event(..., data)`) are
    not scanned; today they carry ids, reasons, timestamps and counts;
  - `scripts/` is not scanned (the scan covers `app/` only).
- Any other exception needs the tech lead's written approval in the PR, and a
  `needs-human-review` label (PII handling).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
