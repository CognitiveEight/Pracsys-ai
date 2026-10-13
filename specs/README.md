# Specs

Every change that is bigger than a one-line fix starts here, before any code.
One Jira ticket = one folder, named after the ticket key:

```
specs/SIM-123-short-name/
  requirements.md         what and why (product view)
  technical-design.md     how (files, payloads, risks)
  tasks.md                ordered implementation steps, one commit each
  acceptance-criteria.md  how we prove it works
  test-cases.md           the check behind each criterion
```

The Jira project key is `SIM`. Copy each file from `specs/_templates/`
(same file name) and fill it in.

## Who writes what

| File | Written by | Approved by |
|---|---|---|
| requirements | product owner, or the `planner` agent from the Jira ticket | product owner |
| technical-design | `planner` agent or developer | tech lead |
| tasks | `planner` agent; the `developer` agent updates the status column | tech lead, with the design |
| acceptance-criteria | `planner` agent, refined by `tester` agent | QA / tech lead |
| test-cases | `planner` agent drafts, `tester` agent completes | QA / tech lead |

Code starts only after the technical design is approved. The `developer`
agent works through `tasks.md` in order, one commit per step. The `tester`
agent turns each acceptance criterion into the checks in `test-cases.md`.
The `reviewer` agent checks the PR against the whole folder. See
`docs/development-workflow.md`.

## Rules

- Specs are committed with the code change, in the same PR.
- A spec states facts about this service only. Never paste payloads, case
  text, party names, credentials or customer data into a spec; describe the
  shape instead (`{workspaceId, documentId}`), as CLAUDE.md requires for logs.
- If the implementation has to deviate from the design, update the design in
  the same PR and say why under "Decisions".
- A task step that is dropped or split is marked in `tasks.md`, not deleted.
