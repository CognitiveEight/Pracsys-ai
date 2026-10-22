---
title: Get started with Claude Code in this repo
type: runbook
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "Before you start"
last_verified: 2026-10-21
jira: SIM-113
risk: low
environments: [local]
---

# Get started with Claude Code in this repo

## In plain words

The AI service repository comes with Claude Code already set up for the team.
Everyone who clones it gets the same helper agents, the same shortcut commands
for each step of our delivery process, and the same safety rules. Those rules
stop the assistant from reading passwords and keys, from publishing or merging
code, and from running the one test that costs real money. A person still
approves every design, publishes every branch, merges every change and runs the
paid test. The only personal step is signing in to Jira and Confluence with
your own account, so the assistant can read tickets and draft pages. It never
writes to either without your yes.

## Before you start

- A clone of the repository with local setup done and `make test` green. See
  [run-locally-and-release.md](run-locally-and-release.md).
- `jq` and `python3` on PATH. The hooks call both. Without `jq` the hooks
  fail and block tool calls.
- `ruff` in `env/bin/ruff` or on PATH (`pip install -r requirements-dev.txt`).
  The lint hook skips silently if neither exists.
- An Atlassian account with access to Jira project **SIM** and the team's
  Confluence space.
- Background reading: `docs/claude-code-setup.md` (full reference) and
  `docs/development-workflow.md` (the delivery process).

### What is in the repo

All of this is committed, so every account gets it from the clone.

| Path | What it is |
|---|---|
| `CLAUDE.md` | The rules and architecture. Loaded in every session. The source of truth. |
| `.claude/rules/*.md` | Short checklists. `security.md` and `pr.md` always apply. The rest load only when Claude touches matching paths: `socket-handlers.md` (`app/socket/**`), `processing.md` (`app/processing/**`), `rag-and-storage.md` (`app/rag/**`, `app/vectorstore/**`, `app/ontology/**`, `app/graph/**`, `app/retrieval/**`, `app/workspace/**`), `llm.md` (`app/llm/**`, `app/ai/**`, `app/langgraph/**`, `app/prompts/**`), `testing.md` (`tests/**`), `code-style.md` (`app/`, `scripts/`, `tests/`). |
| `.claude/agents/` | Five subagents: `planner` (writes specs, never code), `developer`, `tester`, `reviewer` (read-only), `security-auditor` (read-only). |
| `.claude/commands/` | `/spec`, `/implement`, `/verify`, `/review`, `/release-check`, `/retro`, `/meeting`. |
| `.claude/skills/` | `spec-writing`, `testing-patterns`, `confluence-docs`. Loaded when a task needs them. |
| `.claude/hooks/` | `block-secrets.sh`, `protect-branch.sh`, `validate-code.sh` (see the table below). |
| `.claude/settings.json` | Team allow and deny lists, and the hook wiring. |
| `.mcp.json` | The Atlassian MCP server. It holds no keys. |

Personal and git-ignored: `.claude/settings.local.json`, `CLAUDE.local.md`,
and your auto-memory under `~/.claude/projects/`. A rule the team needs never
goes in any of these.

#### The commands

| Command | Runs | What it produces |
|---|---|---|
| `/spec SIM-<n>` | `planner` | Reads the Jira issue, writes `specs/{requirements,technical-design,acceptance-criteria}/SIM-<n>-*.md`. Stops if the issue cannot be read. |
| `/implement SIM-<n>` | `developer` | Refuses unless the technical design says `Status: approved`. Works on `feature/SIM-<n>-…` or `fix/SIM-<n>-…`, created from `development` if needed. |
| `/verify SIM-<n>` | `tester` | One automated check per acceptance criterion, then `make test` and `make lint`. |
| `/review [branch]` | `reviewer` + `security-auditor` in parallel | Findings and a verdict. Edits nothing. |
| `/release-check [staging\|main]` | none | A checklist for the release owner. Never deploys, pushes, merges or tags. |
| `/retro <period>` | none | A retrospective page draft from Jira and git facts only. |
| `/meeting <topic>` | none | A meeting page from the notes you supply. Leaves out names, case details and phone numbers. |

#### What the agents may not do

| Claude cannot | Enforced by |
|---|---|
| Read or edit `.env`, `bns-ai-*.json`, `gcp-*.json`, `application-service-*.json`, `*Creds*.txt`, including through Bash | `permissions.deny` in `.claude/settings.json` + `.claude/hooks/block-secrets.sh` |
| `git push`, `git merge`, `git rebase`, `git reset --hard`, `git branch -D` | `permissions.deny` + `.claude/hooks/protect-branch.sh` (push) |
| Commit on `development`, `staging` or `main`, or commit with `--no-verify` / `-n` | `.claude/hooks/protect-branch.sh` |
| Run `make e2e` or `scripts/e2e_local_backend.py` (real, billed model calls) | `permissions.deny` |
| Leave a ruff finding in a Python file it just edited | `.claude/hooks/validate-code.sh` (runs after every edit) |
| Write to Jira or Confluence without your explicit yes | The commands and the `confluence-docs` skill (instruction, not a hook) |

Do not loosen any of these in `settings.local.json` to get past a prompt.

## Steps

### 1. Start Claude Code in the repo root

```bash
cd <your clone>
claude
```

Expected: Claude Code asks you to trust the project settings. Accept. From
then on `CLAUDE.md`, the always-on rules and the hooks are active.

### 2. Sign in to Atlassian

Inside Claude Code:

```
/mcp
```

Choose `atlassian` and complete the browser sign-in with your own account.
Expected: the server shows as connected. Access follows your own Jira and
Confluence permissions. No key is stored in the repo.

### 3. Check the setup

Ask Claude to read a known SIM issue, then ask it to summarise the CLAUDE.md
rules for the area you will work in.

Expected: it returns the issue text, and the summary names the right rules
(for example "Stop is a fence" for socket handlers).

### 4. Try a read-only command first

```
/review <an existing branch>
```

Expected: a findings report from `reviewer` and `security-auditor`. No file
changes.

### 5. Run a ticket through the workflow

```
/spec SIM-<n>        # then a human sets the technical design to Status: approved
/implement SIM-<n>
/verify SIM-<n>
/review
```

Expected: specs, commits on the ticket branch, tests mapped to each
acceptance criterion, and a review verdict. Then a human pushes, opens the PR,
adds `needs-human-review` where required, runs `make e2e` where required, and
merges.

## How to check it worked

- Ask Claude to `cat .env`. It must refuse (hook message from
  `block-secrets.sh`).
- On `development`, ask Claude to make an empty commit. It must refuse
  (`protect-branch.sh`).
- `/mcp` lists `atlassian` as connected.

## If something goes wrong / how to undo

| Symptom | Cause | Fix |
|---|---|---|
| Every tool call is blocked with a hook error | `jq` missing | Install `jq` |
| Python edits are never linted | No `env/bin/ruff` and no `ruff` on PATH | `pip install -r requirements-dev.txt` |
| `/spec` stops saying it cannot read the issue | Atlassian not signed in, or no access to SIM | Re-run `/mcp`, sign in, check Jira permissions |
| `/implement` refuses to start | Technical design not `Status: approved` | Get the design approved first |
| Claude seems unaware of a team rule | The rule lives in memory or `CLAUDE.local.md` | Move it into `CLAUDE.md` or `.claude/rules/` in a PR |

Nothing here changes shared state. To undo a local experiment, delete
`.claude/settings.local.json` and start a new session.

### Known gaps

- CI and GitHub branch protection are not set up. The local hooks and human
  review are the only gates.
- The Jira and Confluence write confirmation is an instruction in the commands
  and skill, not a technical block.
- `AGENTS.md` (for non-Claude agents) is out of date. Trust `CLAUDE.md`.

## Who to call (roles)

- Tech lead: access to the repo, Jira, Confluence and the `.env` values.
- AI team: questions about the agents, commands, hooks or rules.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
