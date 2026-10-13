# Branches, commits and pull requests (always applies)

Full detail: `docs/development-workflow.md`. Jira project key: **SIM**.

- Every change has a Jira issue. Work on `feature/SIM-<n>-short-name` or
  `fix/SIM-<n>-short-name`, branched from `development`. Never commit on
  `development`, `staging` or `main` (`.claude/hooks/protect-branch.sh`
  blocks it).
- Commits: `feat(SIM-<n>): …`, `fix(SIM-<n>): …`, `docs(SIM-<n>): …`,
  `chore: …` for housekeeping with no ticket. One logical change per commit.
- Non-trivial changes need the spec files under `specs/` for that key, with
  the technical design approved, before code is written.
- Before handing a branch over: `make lint` and `make test` pass;
  `CHANGELOG.md` updated; any socket payload change called out.
- State whether `make e2e` is required (handlers, panels or ingestion
  touched). A human runs it: it makes real, billed model calls.
- Label the PR `needs-human-review` when it changes a socket payload, the
  role gate, the workspace purge, PII handling, billing or model routing.
- PR description: the user-visible behaviour, the Jira key, config or schema
  impacts, and what was and was not verified. No payload examples with real
  data.
- Agents never push, merge, rebase shared branches or delete branches. A
  human does that.
