# Specs

Every change that is bigger than a one-line fix starts here, before any code.
One Jira ticket = one spec folder entry in each of the three directories, named
after the ticket key:

```
specs/requirements/SIM-123-short-name.md        what and why (product view)
specs/technical-design/SIM-123-short-name.md    how (files, payloads, risks)
specs/acceptance-criteria/SIM-123-short-name.md how we prove it works
```

The Jira project key is `SIM`. Start each file from the
`_TEMPLATE.md` in its directory.

## Who writes what

| File | Written by | Approved by |
|---|---|---|
| requirements | product owner, or the `planner` agent from the Jira ticket | product owner |
| technical-design | `planner` agent or developer | tech lead |
| acceptance-criteria | `planner` agent, refined by `tester` agent | QA / tech lead |

Code starts only after the technical design is approved. The `developer`
agent implements against the design, the `tester` agent turns each acceptance
criterion into a test, and the `reviewer` agent checks the PR against all
three files. See `docs/development-workflow.md`.

## Rules

- Specs are committed with the code change, in the same PR.
- A spec states facts about this service only. Never paste payloads, case
  text, party names, credentials or customer data into a spec; describe the
  shape instead (`{workspaceId, documentId}`), as CLAUDE.md requires for logs.
- If the implementation has to deviate from the design, update the design in
  the same PR and say why under "Decisions".
