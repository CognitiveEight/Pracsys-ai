---
description: Draft a retrospective page in Confluence from Jira and git history (facts only; the team writes the opinions)
argument-hint: <scope e.g. "release v5.1.0" or "2026-10-01..2026-10-14">
---

Draft a retrospective for: **$ARGUMENTS** (if empty, ask for the period or
release).

1. Gather facts only, read-only:
   - Jira (project SIM): issues completed, carried over and reopened in the
     period;
   - git: `git log --oneline` for the period on `development`, merge commits
     and their PR numbers;
   - review rounds and `make e2e` failures only if the user supplies them.
2. Follow the **confluence-docs** skill: read the **Retrospective** template
   and fill its Facts table. Leave "What went well", "What did not go well"
   and the opinions in "Agentic workflow" as hints for the team.
3. Show me the title and outline; create the child page only with my yes.
4. Action items become Jira issues only when I confirm each one.
