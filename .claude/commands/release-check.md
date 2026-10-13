---
description: Pre-release checklist for promoting development -> staging -> main. Checks only; never deploys.
argument-hint: [target: staging | main]
---

Prepare the release check for promotion to: **$ARGUMENTS** (if empty, use
`staging`; anything other than `staging` or `main` is an error).
This command never deploys, pushes, merges or tags. A human does that.

Run and report:
1. `git log --oneline <target>..development` — the commits being promoted,
   and the Jira keys found in them. Flag commits with no key.
2. `make lint` and `make test` — summary lines.
3. `CHANGELOG.md` — is there an entry for every promoted change? Are socket
   payload changes called out?
4. Changed areas (`git diff --stat <target>..development`): if socket
   handlers, panels or document processing changed, `make e2e` is required.

Then print this checklist for the human release owner, ticking what the
steps above proved and leaving the rest open:

- [ ] `make lint` and `make test` green
- [ ] `make e2e` run and green (required when handlers, panels or ingestion changed; real, billed calls)
- [ ] CHANGELOG updated; payload changes communicated to backend/frontend
- [ ] PRs labelled `needs-human-review` approved by the tech lead
- [ ] Gate0 spend reconciled on staging (`scripts/reconcile_gate0_spend.py --by-feature`, run by the release owner with their own admin key)
- [ ] `requirements.lock.txt` matches what was tested
- [ ] Rollback path known (previous image tag / feature flag)

Do not run `make e2e` or the Gate0 reconciliation yourself.
