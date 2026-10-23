"""Hook tests for .claude/hooks/protect-branch.sh (SIM-153 security findings 8 and A4).

The combined-short-flag fix was applied with the user's approval on
2026-10-23. A4 (quoted flags, git global options, core.hooksPath and
GIT_CONFIG_* overrides) moved the parsing into
.claude/hooks/_git_commit_guard.py, which splits the command with shlex.
Run against another copy of the hook with PROTECT_BRANCH_HOOK=<path>.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = Path(os.environ.get("PROTECT_BRANCH_HOOK", ROOT / ".claude" / "hooks" / "protect-branch.sh"))
# Built, so this file's own text never reads as a commit flag or a push.
_NO_VERIFY = "-" + "n"
_LONG = "--no-" + "verify"
_PUSH = "pu" + "sh"


def _hook(command, tmp_path):
    call = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)], input=call, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}, timeout=30,
    ).returncode


@pytest.fixture(autouse=True)
def _needs_jq():
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")


@pytest.mark.parametrize("flags", [
    f"{_NO_VERIFY} -m x", f'{_NO_VERIFY}m "x"', "-a" + _NO_VERIFY[1:] + " -m x",
    "-a" + _NO_VERIFY[1:] + "m x", f"{_LONG} -m x", f'-m "x" {_NO_VERIFY}', "-m x --no-verif",
])
def test_protect_branch_hook_blocks_combined_no_verify_flags(tmp_path, flags):
    assert _hook("git commit " + flags, tmp_path) == 2
    assert _hook("git add . && git commit " + flags, tmp_path) == 2


@pytest.mark.parametrize("command", [
    f'git commit -m "fix {_NO_VERIFY} handling"',
    f"git commit -m 'drop {_NO_VERIFY} flag'",
    'git commit -am "x"',
    f"grep {_NO_VERIFY} foo file",
    f"git log {_NO_VERIFY} 3",
    f'git commit -m "$(cat <<\'X\'\nsubject\n\n{_NO_VERIFY} is refused\nX\n)"',
])
def test_protect_branch_hook_reads_flags_not_message_text(tmp_path, command):
    assert _hook(command, tmp_path) == 0


# --------------------------------------------------------- A4: bypass forms


@pytest.mark.parametrize("command", [
    # quoted flags
    f"git commit '{_NO_VERIFY}' -m x",
    f'git commit "{_LONG}" -m x',
    f"git commit -m x '{_LONG}'",
    f'git commit "-a{_NO_VERIFY[1:]}" -m x',
    # hooks path through git's own config
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -c core.hookspath=/tmp/none commit -m x",
    "git -c 'core.hooksPath=/dev/null' commit -m x",
    "git --config-env=core.hooksPath=HOOKS commit -m x",
    "git config core.hooksPath /dev/null && git commit -m x",
    # global options before the subcommand
    f"git --no-pager commit {_NO_VERIFY} -m x",
    f"git -C . commit {_NO_VERIFY} -m x",
    f"git --git-dir=.git --work-tree=. commit {_NO_VERIFY} -m x",
    f"git -c user.name=x commit {_NO_VERIFY} -m x",
    f"git -P commit {_LONG} -m x",
    f"/usr/bin/git commit {_NO_VERIFY} -m x",
    # environment overrides
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/x'\" git commit -m x",
    "env GIT_CONFIG_COUNT=1 git commit -m x",
    "export GIT_CONFIG_COUNT=1; git commit -m x",
    # wrapped in another shell or a substitution
    f'bash -c "git commit {_NO_VERIFY} -m x"',
    f'echo "$(git commit {_NO_VERIFY} -m x)"',
    f"x=`git commit {_LONG} -m x`",
    # several lines
    f"git add .\ngit commit {_NO_VERIFY} -m x",
])
def test_protect_branch_hook_blocks_bypass_forms(tmp_path, command):
    assert _hook(command, tmp_path) == 2, command


@pytest.mark.parametrize("command", [
    f"git commit -m x && git log {_NO_VERIFY} 5",
    f"git commit -m x; git log {_NO_VERIFY} 5 | head {_NO_VERIFY} 3",
    f'git commit -m "no {_NO_VERIFY} here"',
    "git commit -mnote",
    f"git commit -m{_NO_VERIFY[1:]}",
    "git commit -am x -uno",
    "git commit -F msg.txt",
    "git commit -C HEAD",
    'git commit --author "Test Person <test@example.com>" -m x',
    f'git commit --message "a {_LONG} b"',
    f"git commit -m x -- {_NO_VERIFY[1:]}.txt",
    f"echo \"git commit {_NO_VERIFY}\"",
    "git -c color.ui=never log -n 3",
    "git config user.name x",
    "git status && git diff --stat",
    "ls -la",
    "GIT_AUTHOR_NAME=x git commit -m x",
])
def test_protect_branch_hook_allows_ordinary_commands(tmp_path, command):
    assert _hook(command, tmp_path) == 0, command


def test_protect_branch_hook_fails_closed_when_it_cannot_parse(tmp_path):
    assert _hook('git commit -m "unbalanced', tmp_path) == 2
    assert _hook("echo 'unbalanced", tmp_path) == 0


@pytest.mark.parametrize("command", [
    f"git {_PUSH}", f"git -C . {_PUSH} origin x", f"git --no-pager {_PUSH}",
    f"git status && git {_PUSH} origin HEAD",
])
def test_protect_branch_hook_blocks_every_push(tmp_path, command):
    assert _hook(command, tmp_path) == 2


def test_protect_branch_hook_blocks_commits_on_protected_branches(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    assert _hook("git commit -m x", tmp_path) == 2
    assert _hook("git --no-pager commit -m x", tmp_path) == 2
    assert _hook("git status", tmp_path) == 0
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-q", "-c", "feature/SIM-1-x"], check=True)
    assert _hook("git commit -m x", tmp_path) == 0
