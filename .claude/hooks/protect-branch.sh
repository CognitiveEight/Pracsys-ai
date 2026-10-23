#!/usr/bin/env bash
# PreToolUse hook (Bash): enforce the git rules in docs/development-workflow.md.
#
# Blocks:
#   - git commit on development, staging or main (work happens on a ticket branch);
#   - git commit that skips the pre-commit hook (gitleaks, ruff): --no-verify
#     or any prefix of it, -n in any short-flag cluster, core.hooksPath set by
#     -c / --config-env / git config, or GIT_CONFIG_* overrides;
#   - git push of any kind (a human pushes).
# The command is parsed by _git_commit_guard.py (shlex: quoting, git's global
# options, option values such as commit messages); a command it cannot parse
# that mentions git and commit is blocked. Exit 2 blocks the call and shows
# stderr to Claude.
set -euo pipefail

input="$(cat)"
cmd="$(jq -r '.tool_input.command // ""' <<<"$input")"

# Only commands that mention git matter; everything else passes untouched.
grep -q 'git' <<<"$cmd" || exit 0

push_msg="Blocked by .claude/hooks/protect-branch.sh: agents never push. Leave the branch local; a human pushes and opens the PR."
if grep -Eq '(^|[;&|[:space:]])git[[:space:]]+push([[:space:]]|$)' <<<"$cmd"; then
  echo "$push_msg" >&2
  exit 2
fi

guard="$(dirname "${BASH_SOURCE[0]}")/_git_commit_guard.py"
rc=0
if command -v python3 >/dev/null 2>&1 && [ -f "$guard" ]; then
  printf '%s' "$cmd" | python3 -I "$guard" || rc=$?
elif grep -q 'commit' <<<"$cmd"; then
  rc=40  # cannot check: fail closed
fi

case "$rc" in
  0)
    exit 0
    ;;
  10)
    repo="${CLAUDE_PROJECT_DIR:-.}"
    branch="$(git -C "$repo" symbolic-ref --short -q HEAD 2>/dev/null || echo "")"
    case "$branch" in
      development|staging|main)
        echo "Blocked by .claude/hooks/protect-branch.sh: no direct commits to '$branch'. Create a ticket branch first: git switch -c feature/<KEY>-<n>-short-name development" >&2
        exit 2
        ;;
    esac
    exit 0
    ;;
  20)
    echo "Blocked by .claude/hooks/protect-branch.sh: never bypass the pre-commit hook (gitleaks, ruff). Fix what it reports instead." >&2
    exit 2
    ;;
  30)
    echo "$push_msg" >&2
    exit 2
    ;;
  *)
    echo "Blocked by .claude/hooks/protect-branch.sh: could not parse a command that commits; split it into simpler commands." >&2
    exit 2
    ;;
esac
