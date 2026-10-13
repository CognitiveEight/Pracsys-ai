---
description: Turn a Jira issue's acceptance criteria into tests and run them (runs the tester agent)
argument-hint: <JIRA-KEY e.g. SIM-20>
---

Verify Jira issue **$ARGUMENTS**.

1. Read `specs/acceptance-criteria/$ARGUMENTS-*.md`. If it is missing, stop
   and say so.
2. Use the **tester** agent, following the **testing-patterns** skill, to
   write one automated check per acceptance criterion and run `make test`
   and `make lint`.
3. Report a table: AC id, check (file::name), pass/fail. List every AC that
   can only be checked manually and why.
4. Do not run `make e2e`. If it is required, say so; a human runs it.
