#!/usr/bin/env bash
# PreToolUse hook: refuse any tool call that would read, edit or print a
# credential file (.env, service-account JSON, Central Creds.txt).
#
# The deny rules in .claude/settings.json cover the Read/Edit tools; this hook
# also catches the same files reached through Bash (cat, head, cp, grep ...).
# Exit 2 blocks the call and shows stderr to Claude. Only file NAMES are
# matched; the files themselves are never opened here.
set -euo pipefail

input="$(cat)"
tool="$(jq -r '.tool_name // ""' <<<"$input")"

case "$tool" in
  Bash) target="$(jq -r '.tool_input.command // ""' <<<"$input")" ;;
  *)    target="$(jq -r '.tool_input.file_path // .tool_input.notebook_path // ""' <<<"$input")" ;;
esac

[ -z "$target" ] && exit 0

# For Bash, drop text that is data rather than a file access, so a commit
# message or heredoc that merely mentions ".env" is not blocked: heredoc bodies
# (the redirect target on the first line is kept) and git -m/--message values.
if [ "$tool" = "Bash" ]; then
  target="$(python3 -c '
import re, sys
c = sys.stdin.read()
c = re.sub(r"<<-?[ \t]*([\x27\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)",
           lambda m: m.group(0).split("\n", 1)[0], c, flags=re.S)
c = re.sub(r"(?:-m|--message)(?:=|\s+)(?:\"(?:\\.|[^\"\\])*\"|\x27[^\x27]*\x27)", "", c)
sys.stdout.write(c)
' <<<"$target")"
fi

# .env or .env.<suffix> as a path component (env.example and env/ do not match),
# plus the credential file families named in .claude/rules/security.md.
pattern='(^|[[:space:]/"'"'"'=<>|;&])\.env(\.[A-Za-z0-9_-]+)?($|[[:space:]"'"'"';&|>)])'
pattern+='|bns-ai-[^[:space:]]*\.json'
pattern+='|gcp-[^[:space:]]*\.json'
pattern+='|application-service-[^[:space:]]*\.json'
pattern+='|Creds[^/]*\.txt'

if grep -Eq "$pattern" <<<"$target"; then
  echo "Blocked by .claude/hooks/block-secrets.sh: credential files (.env, service-account JSON, Creds files) must not be read, edited or printed. Use env.example for the variable names." >&2
  exit 2
fi
exit 0
