# Security and data (always applies)

- Never read, print, log or commit `.env`, `bns-ai-*.json`, `gcp-*.json`,
  `application-service-*.json`, `Central Creds.txt` or any other credential
  file. If one shows up in `git status` as staged, unstage it and say so.
- Never put case text, party/participant/client names, phone numbers, IDs or
  objective text into code, tests, specs, commit messages or logs. Tests use
  fictional data.
- The pre-commit hook runs gitleaks. Never bypass it with `--no-verify`.
- Do not push, force-push, merge or delete branches; a human does that.
- Workspace deletion is a compliance erasure (CLAUDE.md "Deleting a workspace
  must erase every copy"). Never weaken the purge or its retry/escalation.
