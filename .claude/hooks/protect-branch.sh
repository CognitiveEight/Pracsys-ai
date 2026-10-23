#!/usr/bin/env bash
# PreToolUse hook (Bash): enforce the git rules in docs/development-workflow.md.
#
# Blocks:
#   - git commit on development, staging or main (work happens on a ticket branch);
#   - git commit --no-verify / -n (the pre-commit hook runs gitleaks);
#   - git push of any kind (a human pushes).
# Exit 2 blocks the call and shows stderr to Claude.
set -euo pipefail

input="$(cat)"
cmd="$(jq -r '.tool_input.command // ""' <<<"$input")"

# Only git commands matter; everything else passes untouched.
grep -Eq '(^|[;&|[:space:]])git[[:space:]]' <<<"$cmd" || exit 0

if grep -Eq '(^|[;&|[:space:]])git[[:space:]]+push([[:space:]]|$)' <<<"$cmd"; then
  echo "Blocked by .claude/hooks/protect-branch.sh: agents never push. Leave the branch local; a human pushes and opens the PR." >&2
  exit 2
fi

if grep -Eq '(^|[;&|[:space:]])git[[:space:]]+commit([[:space:]]|$)' <<<"$cmd"; then
  # Flags only: the command on one line with quoted text (commit messages)
  # removed, then each `git commit ...` segment up to the next ; & | . A
  # short-flag cluster containing n (-n, -nm, -an, -anm) is --no-verify, and
  # git accepts any unambiguous prefix of --no-verify.
  flags="$(tr '\n' ' ' <<<"$cmd" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g" \
    | grep -oE 'git[[:space:]]+commit([[:space:]]+[^;&|]*)?' || true)"
  if grep -Eq '(^|[[:space:]])(--no-v[a-z]*|-[A-Za-z]*n[A-Za-z]*)([[:space:]=]|$)' <<<"$flags"; then
    echo "Blocked by .claude/hooks/protect-branch.sh: never bypass the pre-commit hook (gitleaks, ruff). Fix what it reports instead." >&2
    exit 2
  fi

  repo="${CLAUDE_PROJECT_DIR:-.}"
  branch="$(git -C "$repo" symbolic-ref --short -q HEAD 2>/dev/null || echo "")"
  case "$branch" in
    development|staging|main)
      echo "Blocked by .claude/hooks/protect-branch.sh: no direct commits to '$branch'. Create a ticket branch first: git switch -c feature/<KEY>-<n>-short-name development" >&2
      exit 2
      ;;
  esac
fi
exit 0
