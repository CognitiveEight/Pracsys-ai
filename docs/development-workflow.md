# Development workflow

How a change goes from a Jira ticket to production. `ST` stands for the Jira
project key.

## Branches

```
feature/ST-123-short-name ──PR──► development ──PR──► staging ──PR──► main
fix/ST-124-short-name     ──PR──┘
```

- Branch from `development`. Name it with the ticket key so Jira links the
  branch, commits and PR automatically (GitHub for Jira).
- Commits: `feat(ST-123): …`, `fix(ST-123): …`, `chore: …`.
- Nobody, human or agent, commits directly to `development`, `staging` or
  `main`.

## Steps

| # | Step | Who | Output | Jira status |
|---|---|---|---|---|
| 1 | Ticket written with goal and scope | product owner | Jira ticket | To Do |
| 2 | Specs | `planner` agent → reviewed by owner + tech lead | `specs/requirements`, `technical-design`, `acceptance-criteria` | Spec Review |
| 3 | Implementation | `developer` agent or developer | commits on the ticket branch | In Progress |
| 4 | Tests | `tester` agent | tests mapped to each AC; `make test` green | In Progress |
| 5 | Review | `reviewer` agent, then a human approver | findings + verdict on the PR | In Review |
| 6 | Merge to `development` | human | — | Done (dev) |
| 7 | Promote to `staging`, then `main` | release owner | release PR, CHANGELOG | Released |

Run each step from Claude Code in this repo with its command. Each one reads
the Jira issue and asks before writing anything back to Jira:

| Step | Command | Agent(s) | Confluence record |
|---|---|---|---|
| 2 Specs | `/spec SIM-123` | `planner` | Business Requirement (Product Management for an epic), Cross Functional Requirements check, Risk & Backlogs rows |
| 3 Implementation | `/implement SIM-123` | `developer` | Developer Onboarding Process, if setup changed |
| 4 Tests | `/verify SIM-123` | `tester` | none |
| 5 Review | `/review feature/SIM-123-short-name` | `reviewer` + `security-auditor` | Risk & Backlogs rows for open risks |
| 7 Promotion | `/release-check staging` (or `main`) | none; checks only, never deploys | Release Management page |
| Any time | `/meeting <topic>`, `/retro <period>` | none | Meeting & Discussion, Retrospective |

The templates live under **SimplerToday – SDLC Templates** in Confluence; the
`confluence-docs` skill holds the page map.

The rules are enforced, not only written down: `.claude/settings.json` denies
credential files, `git push`/`merge` and `make e2e`, and the hooks in
`.claude/hooks/` block commits on protected branches and `--no-verify`.

## Gates

Before a commit (pre-commit hook, installed with `pre-commit install`):
- gitleaks secret scan, private-key and large-file checks, `ruff`.

Before a PR is approved:
- `make lint` and `make test` pass.
- `make e2e` has been run if the change touches socket handlers, panels or
  document processing (it makes real, billed model calls; a human starts it).
- `CHANGELOG.md` updated; any socket payload change called out.
- One human approval. Changes to payloads, the role gate, the workspace
  purge, PII handling or billing are labelled `needs-human-review` and need
  the tech lead.

## What agents may and may not do

- May: read the code, write specs, edit code and tests on a ticket branch,
  run `make lint` / `make test`, commit on the ticket branch.
- May not: push, merge, force-push, delete branches, run `make e2e`, read or
  touch credentials and `.env`, or bypass the pre-commit hook.

## Local setup

```bash
make setup
pip install -r requirements-dev.txt
pre-commit install
make lint && make test
```

## Not set up yet

- CI (GitHub Actions running `make lint` and `make test` on every PR) and
  branch protection on `development` / `staging` / `main`.
- The Jira project, its workflow statuses and the GitHub for Jira link.
- Running agents from CI (`anthropics/claude-code-action@v1`, which needs an
  `ANTHROPIC_API_KEY` repository secret added by an admin).
