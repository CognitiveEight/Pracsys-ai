# SIM-71: Automated delivery pipeline (Slack → Jira → agents → PR → CI/CD)

- **Jira:** SIM-71 (epic, High, labels `agentic-sdlc`, `ceo-call`). Related:
  SIM-11 (CI and quality gates epic), SIM-23 (CI), SIM-24 (branch protection +
  GitHub↔Jira), SIM-1 (Coding Agents in Jira epic), SIM-72 / SIM-79 (move the
  repository to the SimplerToday GitHub organisation), SIM-31 / SIM-36
  (monitoring)
- **Owner:** product owner (SimplerToday); tech lead approves the design and
  every guardrail change
- **Status:** draft
- **Target branch flow:** feature/SIM-71-automated-delivery-pipeline → development → staging → main

## Problem
The team agreed (PracSys Engineering "How we work (Jira, Git and Claude)",
2026-10-15/16) to run engineering as an agentic SDLC: ticket → spec → plan →
test-first code → review → QA → release, with Claude Code doing most of the
work under human sign-off. For the AI service the pieces exist but every
hand-off between them is manual:

- Requirements, bugs and requests are raised in Slack. Someone has to notice
  them, rewrite them as Jira tickets, and chase missing detail. Requests get
  lost or arrive without testable acceptance criteria.
- The Claude Code framework in this repo (`.claude/` agents, commands, rules,
  hooks, skills) only runs on a developer's machine, one command at a time
  (`/spec`, `/implement`, `/verify`, `/review`). Nothing picks up a ready
  ticket on its own, so throughput depends on who is free to drive Claude.
- There is no CI on `development` and no branch protection, so nothing stops
  a red build or an unreviewed change being merged (SIM-23, SIM-24 open).
- Deployment is manual: SSH to a GCP VM, `git pull`, restart. There is no
  staging deploy on merge, no recorded approval for production, and no
  scripted rollback.
- Jira statuses do not follow the work: tickets have to be moved by hand,
  and nobody can see from Jira which step a change is in.

## Goal
A request raised in Slack, or a ticket written in Jira, reaches staging
without anyone driving the steps in between. People decide at exactly four
points: the ticket is right (G1), the design is right (G2), the code may merge
(G3), production may change (G4). Everything between those gates is automated,
recorded, cost-capped and can be switched off with one variable.

## Scope
- In:
  - **Phase 1, Foundations:** merge the SIM-23 CI workflow; branch rulesets
    and required checks on `development` / `staging` / `main` and the GitHub
    for Jira link (SIM-24); Jira statuses Spec, In QA, Blocked and the labels
    the pipeline uses; secrets and identities for the pipeline (Anthropic API
    key, Jira service account, a delivery-bot GitHub App, GCP workload
    identity for deploys, Slack webhook); Slack channels for requests and for
    delivery notices.
  - **Phase 2, Slack → Jira triage:** a scheduled Claude cloud routine reads
    named Slack channels only, classifies each new message (requirement / bug
    / question / noise), dedupes against open SIM issues, posts a draft ticket
    in the thread, and creates the Jira issue only after an allow-listed
    approver approves the draft (G1).
  - **Phase 3, Jira → agents:** a priority-ordered queue of `agent-ready` SIM
    tickets for this repo; GitHub Actions running the Claude Code GitHub
    Action with this repo's `.claude/` set-up: spec stage (draft PR with the
    three spec files) → wait for G2 → implement, verify, review on the ticket
    branch → push the branch and mark the PR ready, labelled
    `needs-human-review` where the rules require it. Jira transitions and
    comments, Slack notices, cost reporting, failure handling, idempotency,
    concurrency limits.
  - **Phase 4, CI/CD:** CI on every PR; on merge to `development`, deploy to
    the staging environment and move the ticket to In QA; QA moves it to Done
    by hand; release PRs `development` → `staging` → `main` prepared with the
    `/release-check` checklist; production deploy from `main` behind a GitHub
    Environment with required reviewers (G4); scripted rollback.
  - The "How we automate delivery" page in PracSys Engineering (PRACENG) for
    the tech lead to approve, drafted from these specs.
- Out (explicitly not doing):
  - Any change to the AI service runtime (`app/`), its socket events,
    payloads, caches, model routing or billing ledger.
  - Agents merging anything, pushing to `development` / `staging` / `main`,
    approving PRs, moving a ticket to Done, or deploying to production
    without G4. Merges stay human, even though PRACENG permits agents to
    merge to `development`.
  - Running `make e2e` automatically anywhere. It stays human-started and
    billed.
  - Agents writing to Confluence from CI. Pages stay a human-confirmed step.
  - Automating the other three PracSys repos (BE, FE, Admin) or aligning
    their spec layout (`docs/sdlc/specs/`); listed as an open question.
  - Phase 5 (optional, later): monitoring alerts (SIM-31 / SIM-36) and CI
    failures filed as Bugs through the triage routine.
  - Moving the repository to the SimplerToday organisation (SIM-72 / SIM-79);
    a dependency, not part of this work.

## Users and surfaces
- **Requesters** (product owner, engineers, support): post in the requests
  Slack channel; see the draft ticket in their thread.
- **Approvers** (product owner, tech lead): approve drafts in Slack (G1),
  approve the technical design (G2), review and merge PRs (G3), approve
  production deploys (G4).
- **QA engineer:** tests on staging; the only person who moves a ticket to
  Done.
- **Tech lead / admin:** owns the identities, secrets, rulesets, kill switch
  and cost cap.
- Surfaces: Slack channels (**new**), Jira project SIM (statuses, labels,
  comments), GitHub Actions workflows (**new**, under `.github/workflows/`),
  GitHub Environments `staging` / `production` (**new**), the GCP VM(s)
  running the service, the PRACENG Confluence space.
- No socket event, panel or chat path is touched.

## Constraints
Tick every one that applies; each one pulls in a rule the design must meet.
- [ ] Changes a socket payload or `featureType` (backend/frontend must be told)
- [ ] Role-dependent behaviour (must go through the role gate)
- [x] Touches personal data, PII redaction or the workspace purge: Slack
      messages and Jira text may contain client or case details; production
      holds privileged legal data. Tickets, specs, PR text, Jira comments,
      Slack notices and Actions logs must carry no case data, names, phone
      numbers or ids from cases. The deploy path must never copy data.
- [x] Adds a paid call (model, OCR, STT, search) — cost must be metered:
      not a service call. Agent runs and the triage routine are billed
      Anthropic usage; each run's cost must be reported on the ticket and the
      total capped. The service's own ledger is untouched.
- [ ] Indian-law output (BNS/BNSS/BSA; no IPC in `applicable_laws`)
- [ ] Shared workspaces: a `view` collaborator must not be able to mutate

Further constraints from the team rules (PRACENG, 2026-10-15/16) and this repo:
- Workflow To Do → Spec → In Progress → In Review → In QA → Done (+ Blocked);
  only QA moves to Done; tech lead promotes `development` → `staging` → `main`.
- Priority P1–P4 with the conflict order: compliance and security, incidents,
  committed client work, quality gates, improvements. Size ≤ 8 points.
  Component AI, label `ai-repo` for this repo.
- Definition of Ready is checked before an agent writes code: clear goal,
  testable AC, component + priority + size, spec linked if contracts or flows
  change, dependencies named, open questions answered. **The agent never
  invents requirements**: an unready ticket goes to Blocked with its open
  questions.
- Definition of Done is unchanged: tests first and green, lint, code review
  (+ security review where the rules require), docs, deploy notes, merged to
  `development`, deployed to staging, QA passed.
- "The agent proposes, a person confirms." Slack and Jira text are untrusted
  input. `make e2e` never runs automatically. Credential files stay
  unreadable to agents (`.claude/hooks/block-secrets.sh`).

## Success measure
- Every SIM ticket created from Slack carries the `from-slack` label and a
  source-thread link, and has a recorded G1 approval in its thread. Unapproved
  drafts that became tickets: zero (weekly JQL check
  `labels = from-slack` against the approvals).
- For `agent-ready` tickets: share of agent runs that end in a PR versus
  Blocked, and median time from `agent-ready` to PR ready for review. Both are
  read from the Jira comments the pipeline writes (stage, outcome, duration,
  cost).
- Bot pushes to `development` / `staging` / `main`: zero (GitHub audit log;
  the rulesets reject them).
- Every merge to `development` produces a staging deploy run and an In QA
  transition; every production deploy has a recorded G4 approval
  (GitHub Environment deployment history).
- Monthly agent spend stays under the cap set on the Anthropic workspace; the
  per-run cost appears in each Jira comment.
- Kill switch drill: with `AGENTS_ENABLED=false`, no agent workflow starts
  (checked once per quarter).

## Open questions
Each needs the tech lead or an admin unless marked otherwise.
1. **Deploy facts (admin).** VM name(s), project and zone; whether a staging
   VM / staging backend / staging Qdrant exists at all; how the service runs
   on the VM (systemd unit or Docker; unit or container name, checkout path,
   run-as user, venv or image); how `.env` and the GCP service-account files
   are provisioned there; whether IAP TCP forwarding and OS Login are enabled;
   who the production approvers are.
2. **Staging branch meaning.** With staging deployed from `development`
   (product owner decision), what does the `staging` branch represent? Keep
   it as the release-candidate branch (release PR `development` → `staging`,
   then `staging` → `main`), or retire it?
3. **Push rights (tech lead).** Approve the narrow change in the design: in
   CI, a deterministic workflow step under a delivery-bot GitHub App pushes
   only `feature/SIM-*` / `fix/SIM-*` branches and opens PRs; the agent
   itself still cannot push, and merges stay human. This repo is stricter
   than PRACENG on purpose; confirm it stays so.
4. **G1 → agent-ready (product owner).** Should approving a Slack draft also
   make the ticket `agent-ready`, or must a person add that label after
   checking size and priority? The design defaults to "approval adds it" for
   the spec stage only, since G2 still guards code.
5. **G2 mechanism (tech lead).** The design uses the existing rule "only a
   human sets `Status: approved`", detected on push by an allow-listed
   approver. Alternative: a `design-approved` PR label. Which?
6. **Approver allow-lists.** Who may approve drafts (G1) and designs (G2)?
   Where is the Slack approver list kept (routine configuration, not the
   repo)?
7. **Jira statuses (Jira admin).** Do Spec, In QA and Blocked exist on the
   SIM board? This repo's docs and `/spec` still say "Spec Review", "Done
   (dev)" and "Released"; align to the PRACENG names?
8. **Spec location.** Keep `specs/requirements|technical-design|acceptance-criteria`
   here while the other three repos use `docs/sdlc/specs/`, or align later?
9. **Model and cost cap.** Which Claude model for each stage, the per-run
   turn limit, and the monthly spend limit on the Anthropic workspace used by
   CI? Should agent traffic go through Gate0 like the service's model calls
   (one governed layer), or stay direct?
10. **Org move order (SIM-72 / SIM-79).** Create the GitHub App, secrets,
    rulesets and environments after the move to the SimplerToday
    organisation, to avoid doing them twice?
11. **Routine platform.** Confirm the Claude cloud routine can read reactions
    and post thread replies through the Slack connector, and create issues
    through the Atlassian connector under a service identity rather than a
    person's account.
12. **PRACENG page.** Space id and parent page for "How we automate
    delivery"; the `confluence-docs` skill only maps the SimplerToday space.
