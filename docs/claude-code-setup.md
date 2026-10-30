# Claude Code in this repository

How Claude Code is set up for SimplerToday. It covers what is shared through
git, what each person or account has to set up, and how the agentic workflow
runs. Read this when you add a new Claude account, move to a new machine, or
need to understand why Claude refused to do something.

## 1. What Claude reads automatically

When Claude Code starts in this repository it loads the following, with no
action from you:

| Source | Scope | Purpose |
|---|---|---|
| `CLAUDE.md` | Whole repo, every session | The rules and architecture. The single source of truth. |
| `.claude/rules/*.md` | `security.md` and `pr.md` always; the rest only when Claude touches matching paths (`app/socket/**`, `app/processing/**`, `app/rag/**`, `app/llm/**`, `tests/**` …) | Short checklists distilled from CLAUDE.md |
| `.claude/settings.json` | Every session | Team permissions and hooks (section 3) |
| `.claude/agents/*.md` | On demand | Project subagents: `planner`, `developer`, `tester`, `reviewer`, `security-auditor` |
| `.claude/commands/*.md` | On demand | Slash commands: `/spec`, `/implement`, `/verify`, `/review`, `/release-check`, `/retro`, `/meeting` |
| `.claude/skills/*/SKILL.md` | Loaded when the task matches | `spec-writing`, `testing-patterns`, `confluence-docs` |
| `.mcp.json` | Every session | The Atlassian (Jira and Confluence) MCP server. It holds no keys. |

All of the above is committed, so every account gets the same setup from the
clone. Nothing here is tied to one person's account.

## 2. What is personal and not shared

| Item | Where | What to do on a new account or machine |
|---|---|---|
| `.claude/settings.local.json` | Repo, git-ignored | Optional. Personal allow-rules collected from approving prompts. Start empty. Do not copy someone else's: it may allow things the team settings deny on purpose. |
| `CLAUDE.local.md` | Repo, git-ignored | Optional personal notes. Never put rules the team needs in it. |
| Auto-memory | `~/.claude/projects/<repo-path>/memory/` | Per user, per machine, per checkout path. A new account starts with **no** memory. Anything the team depends on must be in CLAUDE.md or `docs/`, not in memory. |
| Atlassian sign-in | Per user, through the MCP OAuth flow | Run `/mcp`, choose `atlassian`, and sign in with your own Atlassian account. Access follows your Jira and Confluence permissions. |
| `.env` and credential files | Repo working tree, git-ignored | Get them from the tech lead (onboarding.md, section 4). Claude is blocked from reading them. |

## 3. Guardrails, and why Claude refuses some things

These are enforced by `.claude/settings.json` and `.claude/hooks/`. They are
deliberate. Do not loosen them in `settings.local.json` to get past a prompt.

| Claude cannot… | Enforced by | Reason |
|---|---|---|
| Read or edit `.env`, `bns-ai-*.json`, `gcp-*.json`, `application-service-*.json`, `*Creds*.txt`, even through Bash | `permissions.deny` + `hooks/block-secrets.sh` | Secrets must never enter a model context |
| `git push`, `merge`, `rebase`, `reset --hard`, `branch -D` | `permissions.deny` + `hooks/protect-branch.sh` | A human publishes and integrates |
| Commit on `development`, `staging` or `main`, or use `--no-verify` | `hooks/protect-branch.sh` | Ticket branches only; gitleaks must run |
| Run `make e2e` / `scripts/e2e_local_backend.py` | `permissions.deny` | It makes real, billed model calls |
| Leave a ruff error behind after an edit | `hooks/validate-code.sh` (PostToolUse) | Lint is fixed in the same step |

The hooks need `jq` and `python3` on PATH. `validate-code.sh` uses
`env/bin/ruff` if it exists, otherwise `ruff` on PATH, and otherwise skips.

Claude also writes nothing to Jira or Confluence without your explicit "yes",
one confirmation per page or update. The commands and the `confluence-docs`
skill say so.

## 4. The agentic workflow

```
/spec SIM-<n>        planner agent        → specs/{requirements,technical-design,acceptance-criteria}/SIM-<n>-*.md
                                           + Business Requirement page (Confluence), Risk rows
     (human approves the technical design: Status: approved)
/implement SIM-<n>   developer agent      → commits on feature/SIM-<n>-…, make lint + make test green
/verify SIM-<n>      tester agent         → one automated check per acceptance criterion
/review [branch]     reviewer + security-auditor (parallel, read-only) → findings + verdict
     (human: push, open PR, label needs-human-review if required, run make e2e if required, merge)
/release-check staging|main               → checklist + Release Management page draft (never deploys)
/meeting <topic>, /retro <period>         → Confluence pages from supplied notes or facts only
```

`/implement` refuses to start unless the technical design says
`Status: approved`. Each command reports what it verified and what it did not.

## 5. First session on a new account

1. Clone the repo and complete `onboarding.md` section 4 (`make test` green).
2. Install `jq` if it is missing. Without it the hooks fail and block tool
   calls.
3. Start Claude Code in the repo root and accept the project settings when
   prompted.
4. Run `/mcp` and authenticate `atlassian`. Check it works by asking Claude to
   read a known SIM issue.
5. Ask Claude to summarise CLAUDE.md's rules for the area you are about to
   work in. This checks that it loaded the right context.
6. Try a read-only command first, for example `/review` on an existing
   branch.

## 6. Keeping this setup healthy

- **A rule the team needs goes in CLAUDE.md or `.claude/rules/`, never in
  memory or `CLAUDE.local.md`.** Another account will not see it otherwise.
- When code changes a rule, update CLAUDE.md in the same PR. The reviewer
  agent checks code against CLAUDE.md, so a stale rule turns into wrong review
  findings.
- Changes to `.claude/settings.json` or `.claude/hooks/` are reviewed like
  code. The security-auditor flags any change that loosens them.
- If Confluence and CLAUDE.md disagree, CLAUDE.md wins. Correct the page.
- The Confluence page and space ids the skill uses are in
  `.claude/skills/confluence-docs/SKILL.md`. If the space moves, update them
  there.
