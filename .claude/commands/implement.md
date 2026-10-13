---
description: Implement an approved spec for a Jira issue on its ticket branch (runs the developer agent)
argument-hint: <JIRA-KEY e.g. SIM-20>
---

Implement Jira issue **$ARGUMENTS**.

1. Find its spec folder `specs/$ARGUMENTS-*/`. If `technical-design.md` is
   missing or its **Status** is not `approved`, stop and say so.
2. Check the branch with `git branch --show-current`. If it is not
   `feature/$ARGUMENTS-…` or `fix/$ARGUMENTS-…`, create one from
   `development` and tell me the name. Never work on `development`,
   `staging` or `main`.
3. Use the **developer** agent to implement the design, working through
   `tasks.md` in order, one commit per step.
4. Run `make lint` and `make test` and paste the summary lines.
5. Commit on the ticket branch as `feat($ARGUMENTS): …` or
   `fix($ARGUMENTS): …`. Do not push.
6. Report what changed, what was verified, what was not, and whether
   `make e2e` is required before deploy.
7. If the change touched setup (Makefile, requirements, `env.example`
   variable names, docker-compose, `.claude/`), propose an update to the
   **Developer Onboarding Process** page (**confluence-docs** skill) and
   write it only with my yes.
8. Ask me before updating Jira. With my yes, move the issue to
   **In Progress** and comment with the branch name.
