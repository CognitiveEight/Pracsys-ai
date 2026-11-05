#!/usr/bin/env bash
# PostToolUse hook (Edit|Write|MultiEdit): run ruff on the Python file just
# changed, with the rules from pyproject.toml, so an unused import or an
# undefined name is fixed in the same step instead of failing `make lint` later.
#
# Exit 2 feeds ruff's findings back to Claude. Exits 0 (silently) when the file
# is not Python, sits in an excluded tree, or ruff is not installed.
set -euo pipefail

input="$(cat)"
file="$(jq -r '.tool_input.file_path // ""' <<<"$input")"

case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

repo="${CLAUDE_PROJECT_DIR:-.}"
if [ -x "$repo/env/bin/ruff" ]; then
  ruff="$repo/env/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then
  ruff="$(command -v ruff)"
else
  exit 0
fi

# --force-exclude keeps pyproject's extend-exclude (env, keel, docs, ...) in force
# even though the path is passed explicitly.
if ! out="$(cd "$repo" && "$ruff" check --force-exclude --quiet "$file" 2>&1)"; then
  echo "ruff found problems in $file (rules: pyproject.toml). Fix them before continuing:" >&2
  echo "$out" >&2
  exit 2
fi
exit 0
