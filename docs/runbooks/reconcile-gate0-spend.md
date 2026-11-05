---
title: Reconcile case cost with gate0
type: runbook
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "Before you start"
last_verified: 2026-10-21
jira: SIM-113
risk: low
environments: [staging, production]
---

# Reconcile case cost with gate0

## In plain words

Every case shows what its AI work has cost. Most of that cost is calls to AI
models, and those calls also pass through gate0, our gateway that governs and
records model spend. This check compares the two records case by case: the
model cost our service added to each case, and the spend gate0 logged for
that case. If they agree, every model call was billed to the right case. If
they differ, some cost went missing or landed on the wrong case. The check
only reads; it changes nothing. It is part of the release checklist on
staging, and is useful whenever a customer's bill looks wrong. Costs that
never go through gate0, such as reading scanned pages or transcribing audio,
are not part of this comparison.

## Before you start

- Run it from a checkout whose `.env` points at the environment you are
  checking (the same Qdrant as the service, because the script reads each
  case's saved totals from it).
- A gate0 key that can read spend logs (the master key or an admin key), in an
  environment variable. The default name is `GATE0_ADMIN_KEY`. The key is read
  from the environment only, never from a flag, so it stays out of shell
  history. The release owner uses their own key.
- `GATE0_BASE_URL` set (or pass `--base-url`). A trailing `/v1` is stripped.
- `GATE0_APP_NAME` must match the service's value (default `simplertoday`):
  only requests tagged `app:<GATE0_APP_NAME>` are counted.

What is compared: gate0's `spend` per `workspace_id:<id>` tag, against
`totalInputPrice + totalOutputPrice` in the case's persisted `WORKSPACE_META`
state (`metadata.token_usage`). The service adds those tags on every gate0
call (`app/llm/providers/gate0.py`).

## Steps

### 1. Run the comparison

```bash
GATE0_ADMIN_KEY=<key> python scripts/reconcile_gate0_spend.py
```

Options, all real flags of the script:

| Flag | Default | Meaning |
|---|---|---|
| `--since YYYY-MM-DD` | 30 days ago | Start of the spend-log window |
| `--until YYYY-MM-DD` | tomorrow | End of the window |
| `--workspace <id>` | all | One case only |
| `--by-feature` | off | Break each case's gate0 spend down by `feature` tag |
| `--tolerance <usd>` | 0.0005 | Difference treated as equal |
| `--key-env <NAME>` | `GATE0_ADMIN_KEY` | Variable that holds the key |
| `--base-url <url>` | `GATE0_BASE_URL` | gate0 address |

Release check form:

```bash
GATE0_ADMIN_KEY=<key> python scripts/reconcile_gate0_spend.py --by-feature
```

Expected: a table, one row per case, largest spend first:
`workspace  calls  gate0 $  SimplerToday $  diff $  status`, with status `OK`,
`MISMATCH` or `no SimplerToday totals`. Then, if any, the spend tagged with the
app but no workspace (scripts, startup work), and finally
`<n> mismatch(es).` The exit code is 0 with no mismatches, 1 with mismatches,
2 if the key or base URL is missing.

### 2. Read the result

- `OK`: the case's model cost matches gate0 within the tolerance.
- `MISMATCH`: a model call's cost did not reach its case, or reached the wrong
  one. Re-run with `--workspace <id> --by-feature` to see which feature.
- `no SimplerToday totals`: gate0 has spend for a case this Qdrant has no
  totals for (wrong environment, a purged case, or state not migrated).

## How to check it worked

The script prints the window and the app tag it used on the first line. Check
both match what you meant to check. A clean run ends with `0 mismatch(es).`

## If something goes wrong / how to undo

The script is read-only on both sides, so there is nothing to undo.

| Symptom | Cause | What to do |
|---|---|---|
| `Set GATE0_ADMIN_KEY to a gate0 key…` | Key variable not set | Export it, or use `--key-env` |
| `Set GATE0_BASE_URL (or pass --base-url).` | No gate0 address | Set it |
| HTTP 401 or 403 | Key cannot read `/spend/logs` | Use an admin or master key |
| Every case `no SimplerToday totals` | `.env` points at a different Qdrant | Use the target environment's settings |
| Mismatch on an old case | Its totals began before gate0 and include direct Gemini spend | Treat the SimplerToday figure as an upper bound, or compare from a fresh start |
| Real mismatch on a new case | A model call ran outside the case's context, or cost was added outside the ledger | Open a ticket; see CLAUDE.md "Cost tracking covers more than tokens" |

### Known gaps

- Only model cost is compared. OCR, Speech-to-Text, Vertex AI Search and
  embeddings that do not go through gate0 appear only in the case's
  `totalServicePrice`.
- The spend logs are fetched in one request with a 120 s timeout. Very large
  windows may time out; shorten `--since`/`--until`.
- No automated test covers the script itself.

## Who to call (roles)

- Release owner: runs it on staging before a promotion.
- AI team: investigates mismatches.
- Gate0 owners: key access and spend-log questions.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
