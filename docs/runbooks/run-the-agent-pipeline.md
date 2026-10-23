---
title: Run the agent pipeline
type: runbook
repo: Pracsys-ai
owner: AI team
status: draft (pilot not started)
audience: tech lead, design approvers, repository admins
last_verified: 2026-10-23
jira: SIM-153
risk: HIGH
environments: [github-actions]
---

# Run the agent pipeline

## In plain words

Three GitHub workflows let Claude take a Jira ticket from "ready" to a pull
request. `agent-dispatch.yml` runs every 15 minutes and starts the other two.
`agent-spec.yml` writes the three spec files and opens a draft PR named
`<KEY>: specs`. A design approver reads it and adds the PR label
`design-approved` (gate G2). `agent-implement.yml` then writes the code, runs
the tests and a review, and pushes to the same PR. Nothing merges: a person
reviews and merges (G3). Everything is off until the repository variable
`AGENTS_ENABLED` is `true`.

The design is `specs/technical-design/SIM-71-automated-delivery-pipeline.md`
("Phase 3 as built" and "Risks and rollback"). This page is the operating
procedure.

## Before the pilot

All of these are admin or owner actions in GitHub settings. None is in code.
**Do not set `AGENTS_ENABLED=true` until every box is ticked.** The first two
are what stops any writer from reading the agent secrets (security audit A1):
`workflow_dispatch` runs the workflow file at whatever ref the dispatcher
names, so a writer could otherwise dispatch a modified branch copy, or push a
new `on: push` workflow, and read every repository-level secret.

- [ ] **Create the Environment `pracsys-agents`** (Settings → Environments).
      Deployment branches and tags: **Selected branches, `main` only.** No
      required reviewers (the dispatcher starts runs unattended).
- [ ] **Move the agent secrets into it, and delete their repository-level
      copies:** `ANTHROPIC_API_KEY`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
      `SLACK_BOT_TOKEN`, `AGENT_PUSH_TOKEN`. Every job in `agent-*.yml` that
      reads a secret declares `environment: pracsys-agents` (a static test
      keeps it so).
      `slack-triage.yml` and `daily-worklist.yml` are unchanged and still read
      `ANTHROPIC_API_KEY`, `JIRA_*` and `SLACK_BOT_TOKEN` at repository level.
      So the Environment must hold **separate credentials** (a separate
      Anthropic CI key with its own spend limit, a separate Jira API token, a
      re-issued Slack token). A repository-level copy with the same value as
      the Environment's defeats the control. `AGENT_PUSH_TOKEN` has no other
      reader: delete its repository copy.
- [ ] **A push identity outside `DESIGN_APPROVERS`.** `AGENT_PUSH_TOKEN` must
      belong to a machine user or GitHub App that is not a design approver.
      Otherwise anything acting with the token could add `design-approved` as
      an approver.
- [ ] **Branch rulesets** on `development`, `staging` and `main` with no
      bypass for that push identity.
- [ ] **Repository variables:** `DESIGN_APPROVERS` (comma-separated GitHub
      logins; empty or unset fails the dispatcher job with an error and starts
      no implement run), `DELIVERY_CHANNEL_ID`, optionally
      `AGENT_MAX_CONCURRENT` (default 1), `AGENT_MODEL`, `AGENT_MAX_TURNS`
      (spec, default 60), `AGENT_MAX_TURNS_IMPLEMENT` (default 120).
- [ ] **GitHub labels exist:** `agent-pr`, `design-approved`,
      `agent-implemented`, `needs-human-review`.
- [ ] **The three workflow files are on `main`** (a human copies them; scheduled
      and dispatched runs use `main`'s copy) and the scripts are on
      `development`.
- [ ] **Anthropic workspace spend limit** set on the CI workspace. It is the
      hard budget control: the pipeline's own `budget` flag is advisory.
- [ ] The pilot checks listed in the design's risk table (harden-runner with
      the Qdrant service, `allowed_bots`, the action pin, the activity API
      with the read-only job token).

## Approving a design (G2)

1. Read the three spec files on the PR **at its current head**. The label is
   not tied to a commit; what you label is whatever the head is when you add
   it.
2. Add the label `design-approved`. Do not push after labelling: any push at
   or after the label (to the second) makes the approval stale.
3. The next dispatcher tick (up to 15 minutes) starts the implement stage.
   Its closing Jira comment and Slack notice say "design approved at <first 7
   of the SHA>": check that it is the head you read.

## Re-running implement after a blocked run

A blocked or failed implement run leaves these behind, and each one stops the
dispatcher from starting the issue again. Undo all that apply:

| What the run left | Where | Why it stops a re-run | Undo |
|---|---|---|---|
| `agent-implemented` | PR label | the dispatcher skips the PR and the gate refuses it (`already-implemented`, quietly) | remove the label |
| `agent-blocked` | Jira label | `check-scope` excludes the issue | remove the label |
| Flagged (Impediment) | Jira field | not checked by the code, but it says the issue is blocked | clear the flag |
| `agent-running` | Jira label | `check-scope` excludes the issue | normally removed by the run; remove it if the run was cancelled before cleanup |
| status In Progress | Jira status | `check-scope` requires To Do or Spec | move the issue back to **Spec** |
| a draft push after the label | PR branch | the approval is stale (`approval-stale`) | review the new head, then remove and re-add `design-approved` |

The issue must still carry `agent-ready` and `ai-repo`. Then wait for the next
dispatcher tick, or start a fresh run yourself (below).

Specific G2 refusals (reason in the run's job summary; Jira and Slack say
`g2-refused`):

- `approval-stale`: something was pushed at or after the label. Review the
  head, remove and re-add the label.
- `approval-stale` that will not clear after a re-label: a commit on the
  branch is dated in the future, so it sorts after the label. Re-commit it
  with a correct date, push, re-label.
- `protected-path`: the branch changes the pipeline or the gates. Remove that
  change from the branch.
- `protected-path-stale`: `development` changed a protected file (for example
  the Makefile or a `conftest.py`) since the branch was cut. Merge or rebase
  `development` into the branch, push, re-label.
- `push-unverified`: the push record could not be read. Check that the
  activity API answers for the branch with the job token, then re-run.
- `git-check-failed`: the git check crashed. Look at the run log, fix the
  cause, then re-run.
- `approver-not-allowed`: the label was added by someone not in
  `DESIGN_APPROVERS`. An approver re-adds it.

A PR whose head is not the issue's own ticket branch is refused quietly
(`wrong-branch`, job summary only): nothing in Jira or Slack changes.

**Do not use "Re-run failed jobs"** on an implement run. It reuses the
`prepare` job's outputs, so G2 is not checked again. Start a fresh run
instead:

```bash
gh workflow run agent-implement.yml --ref main -f issue=SIM-<n> -f pr=<n> -f sha=<40-hex PR head>
```

## Stopping it

- Set `AGENTS_ENABLED` to anything other than `true`: every agent job stops at
  its `if:`.
- As a last resort revoke the Environment's `ANTHROPIC_API_KEY`. CI and
  deploys are unaffected.

## What is advisory and what is a control

"Ready for review", the `gates` job's lint and test result and the `budget`
reason are the pipeline's opinion. The gates job runs the code under test and
the budget flag comes from the agent's runner, so either could be forged. The
controls are the required PR CI checks, the human review and merge (G3) and
the Anthropic workspace spend limit.
