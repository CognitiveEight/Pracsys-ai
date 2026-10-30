# Agent prompts (SIM-71 Phase 3)

The exact instructions the agent workflows give Claude
(`anthropics/claude-code-action`, agent mode):

| File | Used by | Equivalent interactive commands |
|---|---|---|
| `spec.md` | `.github/workflows/agent-spec.yml` | `/spec <KEY>` |
| `implement.md` | `.github/workflows/agent-implement.yml` | `/implement`, `/verify`, `/review` |

Each workflow's `prompt:` is a short message naming this file, the issue key
and the absolute paths of the ticket and outcome files under
`$RUNNER_TEMP/agent/`. Ticket text never appears in a prompt string, a
workflow expression or a `run:` script.

What the workflows rely on, and enforce whatever the model does:

- The agent step holds only `ANTHROPIC_API_KEY`, plus the job's read-only
  `github.token` (checkout credentials are not persisted). The allowed tools
  are file tools, `Task` and a fixed list of `git` / `make` prefixes: no
  `git push`, `gh`, `curl`, web tools or `make e2e`. `.claude/settings.json`
  and its hooks still apply.
- The outcome file is untrusted. `scripts/delivery/stage_io.py` keeps only
  the fields described in the prompts and turns anything else into
  `blocked` / `workflow-failed`.
- Pushing, the PR, Jira and Slack happen in a separate job on a fresh runner,
  after the agent's job has ended, with `AGENT_PUSH_TOKEN` and the Jira and
  Slack secrets. That job checks the branch (`guard_branch.py`), confines the
  spec stage to its own spec files, and forces `needs-human-review` for
  sensitive paths (`review_labels.py`).

Changing a prompt changes what runs unattended. Such a PR touches
`.github/**`, so it is always `needs-human-review`.
