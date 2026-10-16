---
description: Review a branch against its spec and CLAUDE.md, plus a security audit (runs reviewer and security-auditor)
argument-hint: [branch, default: current branch]
---

Review branch: **$ARGUMENTS** (if empty, use the current branch).

1. Resolve the branch (`git branch --show-current` if none was given) and the
   Jira key in its name (`SIM-<n>`). If there is no key, say so; the change
   is missing its ticket.
2. Run the **reviewer** agent and the **security-auditor** agent on that
   branch, in parallel.
3. Merge their findings into one list, most severe first, without duplicates.
   Each finding: file:line, the problem, a concrete failure scenario.
4. Give one overall verdict: approve / approve with follow-ups / request
   changes. Say whether the PR needs the `needs-human-review` label and
   whether `make e2e` must be run before deploy.
5. For each risk the PR leaves open, propose a **Risk & Backlogs** row
   (**confluence-docs** skill); likelihood, impact and owner stay blank for
   a human.
6. Ask me before posting anything to Jira, Confluence or GitHub. Do not push,
   merge or approve a PR yourself.
