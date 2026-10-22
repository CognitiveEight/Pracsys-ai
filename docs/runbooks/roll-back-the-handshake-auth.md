---
title: Roll back the AI handshake auth
type: runbook
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "Before you start"
last_verified: 2026-10-21
jira: SIM-113
risk: HIGH
environments: [staging, production]
---

# Roll back the AI handshake auth

## In plain words

When the AI service connects to the main application, it proves who it is
with a signature that changes on every connection and expires within
minutes. The old method used one fixed signature that never expired, so
anyone who copied it once could pose as the AI service and reach every
customer's case files. The old method is kept only as an emergency fallback
in case the new one fails during a change. Using it reopens that weakness,
and it works only while the main application's team has deliberately turned
the old method back on. That is why the risk is high. Use it for the
shortest time possible, tell the other team, and switch back the same day.
Most connection failures are fixed without a rollback.

## Before you start

**First, decide whether you need a rollback at all.** Most refusals are fixed
on the current scheme. Read the refusal code from the backend team (the
backend disconnects without a reason, so the code is only in their logs) and
check this table, from `docs/AI_SERVICE_AUTH_MIGRATION.md` section 8:

| Backend code | What the AI service sees | Usual cause | Fix without rollback |
|---|---|---|---|
| `missing_header` | Refused at once | A signature header missing; or the secret is missing so the service connects unsigned (logged as `Handshake #n NOT signed`) | Set `SOCKET_SECRET_KEY` |
| `bad_signature` | Refused at once | Secret differs between the two sides, or an encoding error | Confirm both sides hold the same secret. The service runs a self-check at startup, so an encoding bug fails startup first. |
| `stale_timestamp` | **Every** attempt refused | Host clock more than 300 s off | Fix NTP on the host (containers use the host clock) |
| `replayed_nonce` | First connect works, every **reconnect** refused | Headers reused | Should not happen: headers are re-signed per attempt. If the backend also checks the nonce on the websocket upgrade, set `SOCKET_TRANSPORTS=websocket` |
| `malformed_nonce` / `malformed_timestamp` | Refused at once | Header format wrong | A code defect; raise it with the AI team |
| `legacy_scheme_disabled` | Refused at once | The service is sending the **legacy** signature and the backend window is closed | Set `SOCKET_AUTH_SCHEME=v1` |
| `secret_not_configured` | All AI connections refused | Secret missing on the **backend** | Backend team |
| `nonce_store_unavailable` | Intermittent refusals | Backend could not record the nonce | Backend team |
| `replay_guard_unverified` | Every attempt refused from backend start | Backend nonce index missing | Backend team. Change nothing on the AI side |

A rollback to the legacy scheme only helps when the v1 code itself is broken
and the old signature would be accepted. You need:

- The backend team to have `AI_SIGNATURE_ALLOW_LEGACY=true` set and restarted
  (their flag; it defaults to off). Without it the legacy signature is refused
  with `legacy_scheme_disabled`.
- `SOCKET_AI_PAYLOAD` set in the AI service environment. Startup refuses
  `legacy` without it.
- The tech lead's approval, and a time agreed to switch back.

Code: `app/socket/auth.py` (header builders), `app/socket/client.py`
(`_get_custom_headers`, refusal diagnostics), `app/core/lifecycle.py`
(`_validate_config`), `app/core/config.py` (`socket_auth_scheme`).

## Steps

### 1. Ask the backend team to open the window

Agree the time. They set `AI_SIGNATURE_ALLOW_LEGACY=true` and restart.
Record the time in the shared channel.

Expected: the backend logs a warning naming the flag on every legacy
acceptance.

### 2. Switch the AI service to legacy

In the AI service environment:

```bash
SOCKET_AUTH_SCHEME=legacy
# SOCKET_AI_PAYLOAD must already be set
```

Restart the service.

Expected, in the AI service log:
- at startup, a warning that `SOCKET_AUTH_SCHEME=legacy` is replayable and
  rollback-only;
- on connect, `Connected on the LEGACY handshake scheme (handshake #n)`.

An alternative that needs no setting change: if the previous release predates
v1 and the window is open, redeploy that release.

### 3. Fix forward

Find and fix the v1 problem on a `fix/SIM-<n>-…` branch. Before deploying,
run the self-check:

```bash
python -m app.socket.auth
```

Expected: `self-check OK`.

### 4. Switch back to v1

```bash
SOCKET_AUTH_SCHEME=v1
```

Restart the service.

Expected: `Socket connected (handshake #n, scheme=v1, …)` and no `LEGACY`
warning. Survive one deliberate reconnect (restart the process) to prove the
reconnect path re-signs.

### 5. Ask the backend team to close the window

They set `AI_SIGNATURE_ALLOW_LEGACY=false` and restart. Confirm the AI service
reconnects afterwards: that proves it is on v1 and not riding the legacy path.
Record the closing time.

### 6. Rotate the secret

The legacy signature is a fixed value valid for as long as the secret lives.
Treat `SOCKET_SECRET_KEY` as possibly exposed and rotate it with the backend
team as a separate, coordinated change.

## How to check it worked

- AI service log: `scheme=v1` on connect, and no `Connected on the LEGACY
  handshake scheme` line after step 4.
- Backend log: handshakes verified on scheme `v1`, and the
  `AI_SIGNATURE_ALLOW_LEGACY` warning has stopped.
- One test workspace returns its panels end to end. A handshake is not a
  working feature.

## If something goes wrong / how to undo

- **Startup fails with `SOCKET_AI_PAYLOAD is required when SOCKET_AUTH_SCHEME=legacy`:**
  set the payload or go back to `v1`.
- **Startup fails with `SOCKET_AUTH_SCHEME must be 'v1' or 'legacy'`:** the
  value is misspelt. Unknown values are refused, never downgraded.
- **Legacy refused with `legacy_scheme_disabled`:** the backend window is not
  open. Ask the backend team, or return to `v1`.
- **Every attempt refused on both schemes:** look at clock drift and the
  secret first. The service prints one diagnosis hint per outage, with local
  UTC time and the timestamp sent.
- **To undo the rollback:** steps 4 and 5.

When reporting a refusal to the backend team, send the UTC time, the
`x-signature-timestamp` and `x-signature-nonce` values (logged at DEBUG) and
the signature length. Never send the signature or the secret.

### Known gaps

- No automated test covers `app/socket/auth.py` or the legacy path. The
  startup self-check and `make e2e` (handshake step) are the only checks.
- `SOCKET_SIGNATURE_ENCODING` is defined in config and `env.example` but no
  code reads it.
- How environment values are changed and the service restarted in staging
  and production is not in this repo; this page could not verify it.

## Who to call (roles)

- Backend team: refusal codes, opening and closing `AI_SIGNATURE_ALLOW_LEGACY`.
- Tech lead: approves the rollback and the secret rotation.
- AI team on call: runs the steps.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
