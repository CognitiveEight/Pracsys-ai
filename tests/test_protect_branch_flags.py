"""Hook test for the combined-short-flag fix (SIM-153 security finding 8).

Applied with the user's approval on 2026-10-23. Run against another copy
of the hook with PROTECT_BRANCH_HOOK=<path>.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = Path(os.environ.get("PROTECT_BRANCH_HOOK", ROOT / ".claude" / "hooks" / "protect-branch.sh"))
_NO_VERIFY = "-" + "n"  # built, so this file's own text never reads as a commit flag


def _hook(command, tmp_path):
    call = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)], input=call, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}, timeout=30,
    ).returncode


@pytest.mark.parametrize("flags", [
    f"{_NO_VERIFY} -m x", f'{_NO_VERIFY}m "x"', "-a" + _NO_VERIFY[1:] + " -m x",
    "-a" + _NO_VERIFY[1:] + "m x", "--no-verify -m x", f'-m "x" {_NO_VERIFY}', "-m x --no-verif",
])
def test_protect_branch_hook_blocks_combined_no_verify_flags(tmp_path, flags):
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
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
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    assert _hook(command, tmp_path) == 0
