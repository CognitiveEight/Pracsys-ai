# SIM-152: Instant replies in #pracsys-requests — technical design

- **Requirements:** `specs/requirements/SIM-152-instant-triage.md`
- **Status:** draft
- **Approver:** product owner (demo); tech lead before production GCP

## Summary
```
Slack (message / reaction in #pracsys-requests)
   │ Events API, HTTPS POST, signed
   ▼
GCP Cloud Function "slack-triage-relay" (2nd gen, Python 3.12)
   │ verify signature → filter → POST /actions/workflows/slack-triage.yml/dispatches
   ▼
GitHub Actions "Slack triage" (unchanged) → replies in Slack within ~1 minute
```
No service code (`app/`) changes. The relay holds no Slack bot token and no
Anthropic key; it can only start one workflow.

## Affected code
| File | Change |
|---|---|
| `relay/slack_events/main.py` | **new**: the HTTP function |
| `relay/slack_events/requirements.txt` | **new**: `functions-framework` only |
| `tests/test_slack_events_relay.py` | **new**: tests first, no network |
| `docs/runbooks/deploy-slack-events-relay.md` | **new**: deploy, configure Slack, roll back |
| `.github/workflows/slack-triage.yml` | concurrency comment only; behaviour unchanged |

## Event and payload changes
None for the AI service. New inbound HTTP endpoint on GCP only.

## Design
**Request handling** (`handle(request)`):
1. Read the raw body. Verify Slack's signature: `v0=` + HMAC-SHA256 of
   `v0:{X-Slack-Request-Timestamp}:{body}` with the signing secret, compared
   in constant time; the timestamp must be within 5 minutes. Fail → 401.
2. `type == "url_verification"` → return the `challenge` (Slack's one-time
   check when the URL is saved).
3. `type == "event_callback"`: consider only
   - `message` events in `TRIAGE_CHANNEL_ID`, without `bot_id`, without a
     `subtype` other than `thread_broadcast` (so joins, edits, deletions and
     the bot's own posts are ignored); top-level posts and thread replies
     both count (a reply "approve" is a decision);
   - `reaction_added` on an item in `TRIAGE_CHANNEL_ID`, reaction
     `white_check_mark`, `heavy_check_mark`, `x` or
     `negative_squared_cross_mark`.
   Anything else → 200, ignored.
4. Slack re-sends an event if it gets no reply within 3 seconds
   (`X-Slack-Retry-Num`); a retry → 200, ignored (the first delivery already
   started a run).
5. Start the workflow: `POST https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/slack-triage.yml/dispatches`
   with `{"ref": "main"}` and the GitHub token; timeout 2 seconds; always
   reply 200 to Slack (a failed dispatch is logged; the 5-minute schedule
   is the backstop).
6. Log one line per request: event type, whether it matched, dispatch
   status. Never the text, user or timestamps of a message.

**Collapsing bursts.** The workflow already has
`concurrency: {group: slack-triage, cancel-in-progress: false}`: GitHub keeps
one run going and replaces any queued run with the newest, so each burst
ends with a run that sees every message. No change needed.

**Configuration** (function env; secrets from Secret Manager):
`SLACK_SIGNING_SECRET` (secret), `GITHUB_DISPATCH_TOKEN` (secret: a
fine-grained token for `CognitiveEight/Pracsys-ai` only, permission
"Actions: read and write", nothing else), `GITHUB_REPO`
(`CognitiveEight/Pracsys-ai`), `TRIAGE_CHANNEL_ID` (`C0CAXK9J7U6`).

**Slack app.** Event Subscriptions on; Request URL = the function URL; bot
events `message.channels` and `reaction_added`. Scopes already granted
(`channels:history`, `reactions:read`).

**Deploy** (runbook): `gcloud functions deploy slack-triage-relay --gen2
--runtime python312 --region <region> --trigger-http --allow-unauthenticated
--entry-point handle --source relay/slack_events --set-secrets ...
--set-env-vars ... --max-instances 3 --memory 256Mi`. Unauthenticated at
the GCP layer because Slack cannot send a Google identity token; the Slack
signature check is the authentication.

## Checklist against CLAUDE.md
- Payloads, role gate, purge, model routing, gate0, billing: n/a.
- Secrets: only in Secret Manager; never logged; the token can start
  workflows in this one repo and do nothing else.
- PII: no message text, user ids or names logged or forwarded.
- Logging: one line per request (type, matched, dispatch status).

## Risks and rollback
- A leaked function URL is harmless without the signing secret.
- Run cost: at most one run per burst; the Anthropic workspace spend limit
  is the cap.
- Rollback: turn off Event Subscriptions in the Slack app (the schedule keeps
  working), or delete the function.

## Test plan
`tests/test_slack_events_relay.py`, no network (dispatch replaced): valid
signature accepted; bad signature, old timestamp, missing headers → 401;
URL verification returns the challenge; person's message in the channel →
one dispatch; bot message, other channel, join/edit subtype → no dispatch;
approval reactions → dispatch, other reactions → none; Slack retry → no
dispatch; dispatch failure still returns 200; nothing of the message text
reaches the log. `make e2e` not needed.

## Decisions
- GCP Cloud Function on a demo project first (product owner, 2026-10-23).
- Dispatch the existing workflow instead of running Claude in the function:
  one place for the rules, the tokens and the tests.
