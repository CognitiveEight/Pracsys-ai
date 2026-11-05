#!/usr/bin/env python3
"""Classify a shell command for protect-branch.sh (SIM-153 security audit A4).

    printf '%s' "$cmd" | python3 .claude/hooks/_git_commit_guard.py

Exit codes: 0 nothing to check; 10 a git commit (the caller checks the
branch); 20 the commit would skip the pre-commit hook; 30 a git push; 40 the
command cannot be parsed and mentions git and commit (fail closed).

The command is split with ``shlex`` into shell segments on ``;``, ``&&``,
``||``, ``|``, ``&``, parentheses and newlines, so quoted text is one word and
a commit message is never read as a flag. In each segment, leading
``NAME=value`` assignments and wrapper commands (``env``, ``command``, ...)
are skipped, then git's global options (``-c k=v``, ``-C dir``,
``--no-pager``, ``--git-dir=...``), then the subcommand. For ``commit`` the
options are read with their values (``-m``, ``-F``, ``-C``, ``-c``, ``-t``
and the long forms take one; ``-m<text>`` is attached), so ``-n`` in any
short cluster and any ``--no-v*`` prefix of ``--no-verify`` are found and
nothing else is. ``core.hooksPath`` set through ``-c``, ``--config-env`` or
``git config``, and any ``GIT_CONFIG_*`` assignment or export in a command
that commits, count as skipping the hook. Text inside ``$(...)``, backticks,
``bash -c`` / ``sh -c`` and ``eval`` is checked the same way. Heredoc bodies
are data (commit messages), except when they are fed to a shell.
Standard library only.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import List, Tuple

OK, COMMIT, BYPASS, PUSH, UNPARSED = 0, 10, 20, 30, 40

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "time", "nice", "timeout", "xargs",
             "sudo", "doas", "stdbuf"}
#: git global options that take the next word as their value (when not written `--opt=value`).
_GLOBAL_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                      "--config-env", "--attr-source", "--list-cmds"}
#: `git commit` short options whose value is the rest of the cluster or the next word.
_COMMIT_SHORT_VALUE = set("mFCct")
#: `git commit` short options whose (optional) value can only be attached.
_COMMIT_SHORT_ATTACHED = set("uS")
_COMMIT_LONG_VALUE = {"--message", "--file", "--reuse-message", "--reedit-message", "--template",
                      "--author", "--date", "--cleanup", "--fixup", "--squash", "--trailer",
                      "--pathspec-from-file", "--untracked-files"}


def _strip_heredocs(text: str) -> str:
    """Drop heredoc bodies (data), unless the heredoc feeds a shell or eval."""
    out: List[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        match = _HEREDOC.search(line)
        i += 1
        if not match or "<<<" in line:
            continue
        words = re.findall(r"[A-Za-z_./-]+", line[:match.start()])
        if any(os.path.basename(w) in _SHELLS | {"eval", "source", "."} for w in words):
            continue
        end = match.group(2)
        while i < len(lines) and lines[i].strip() != end:
            i += 1
        i += 1  # the terminator line
    return "\n".join(out)


def _tokens(text: str) -> List[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.commenters = ""
    lex.whitespace_split = True
    return list(lex)


def _segments(tokens: List[str]) -> List[List[str]]:
    segments: List[List[str]] = [[]]
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok and set(tok) <= set("();<>|&\n"):
            if set(tok) <= set("<>&") and ("<" in tok or ">" in tok):
                skip_next = True  # a redirection and its target
                continue
            segments.append([])
            continue
        segments[-1].append(tok)
    return [s for s in segments if s]


def _hooks_path(setting: str) -> bool:
    return setting.split("=", 1)[0].strip().lower() == "core.hookspath"


def _commit_flags(args: List[str]) -> int:
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name.startswith("--no-v"):
                return BYPASS
            if name in _COMMIT_LONG_VALUE and "=" not in arg:
                i += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            for pos, char in enumerate(arg[1:], start=1):
                if char == "n":
                    return BYPASS
                if char in _COMMIT_SHORT_ATTACHED:
                    break
                if char in _COMMIT_SHORT_VALUE:
                    if pos == len(arg) - 1:
                        i += 1  # the value is the next word
                    break
    return COMMIT


def _git(args: List[str], git_config_env: bool) -> int:
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            break
        name, has_value, value = arg.partition("=")
        if arg.startswith("-c") and arg != "-c" and not arg.startswith("--"):
            name, value, has_value = "-c", arg[2:], "="
        if name in _GLOBAL_WITH_VALUE and not has_value:
            value = args[i + 1] if i + 1 < len(args) else ""
            i += 1
        if name in ("-c", "--config-env") and _hooks_path(value):
            return BYPASS
        i += 1
    if i >= len(args):
        return OK
    sub, rest = args[i], args[i + 1:]
    if sub == "push":
        return PUSH
    if sub == "config":
        return BYPASS if any(_hooks_path(a) for a in rest) else OK
    if sub == "commit":
        return BYPASS if git_config_env else _commit_flags(rest)
    return OK


def _segment(words: List[str], nested: List[str], exported: List[bool]) -> int:
    git_config_env = False
    i = 0
    while i < len(words) and _ASSIGNMENT.match(words[i]):
        git_config_env |= words[i].startswith("GIT_CONFIG")
        i += 1
    rest = words[i:]
    if not rest:
        return OK
    head = os.path.basename(rest[0])
    if head in ("export", "declare", "typeset", "readonly"):
        if any(w.lstrip("-").startswith("GIT_CONFIG") for w in rest[1:]):
            exported[0] = True
        return OK
    if head == "eval":
        nested.append(" ".join(rest[1:]))
        return OK
    if head in _SHELLS:
        for pos, word in enumerate(rest[1:-1], start=1):
            if word.startswith("-") and "c" in word[1:]:
                nested.append(rest[pos + 1])
        return OK
    if head in _WRAPPERS:
        for pos, word in enumerate(rest[1:], start=1):
            if _ASSIGNMENT.match(word):
                git_config_env |= word.startswith("GIT_CONFIG")
            elif os.path.basename(word) == "git":
                return _git(rest[pos + 1:], git_config_env)
            elif os.path.basename(word) in _SHELLS:
                return _segment(rest[pos:], nested, exported)
        return OK
    if head == "git":
        return _git(rest[1:], git_config_env)
    return OK


def _substitutions(text: str) -> List[str]:
    """The bodies of every backtick pair and ``$(...)`` in the raw text (quoted or not)."""
    found = re.findall(r"`([^`]*)`", text)
    start = text.find("$(")
    while start >= 0:
        depth, end = 1, start + 2
        while end < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
        found.append(text[start + 2:end - 1] if depth == 0 else text[start + 2:])
        start = text.find("$(", start + 2)
    return found


def classify(text: str, depth: int = 0) -> Tuple[int, bool]:
    """``(verdict, commits)``: the strongest verdict found, and whether anything commits."""
    text = _strip_heredocs(text)
    try:
        tokens = _tokens(text)
    except ValueError:
        if re.search(r"\bgit\b", text) and "commit" in text:
            return UNPARSED, True
        return OK, False
    verdict, commits = OK, False
    exported = [False]
    nested: List[str] = _substitutions(text)
    for words in _segments(tokens):
        result = _segment(words, nested, exported)
        commits |= result in (COMMIT, BYPASS)
        verdict = max(verdict, result)
    if depth < 4:
        for inner in nested:
            result, inner_commits = classify(inner, depth + 1)
            commits |= inner_commits
            verdict = max(verdict, result)
    if exported[0] and commits:
        verdict = max(verdict, BYPASS)
    return verdict, commits


def main() -> int:
    return classify(sys.stdin.read())[0]


if __name__ == "__main__":
    sys.exit(main())
