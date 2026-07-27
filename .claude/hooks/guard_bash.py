#!/usr/bin/env python3
"""PreToolUse guard for Bash tool calls.

Fail-open by design: any surprise in the stdin payload exits 0 (allow).
Exit 2 (block, reason on stderr) only on a positive match:
- git push with --force / --force-with-lease / -f / a +refspec
- git push targeting main (explicit, HEAD:main, refs/heads/main, or bare push)
- --no-verify on any git command
- rm with combined recursive and force flags in any spelling
- word-boundary `claude` CLI invocation (subagents spawn via the Agent tool)
"""

from __future__ import annotations

import json
import re
import shlex
import sys

_GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_FORCE_LONG_OPTS = ("--force", "--force-with-lease", "--force-if-includes")


def _deny(reason: str) -> None:
    print(reason, file=sys.stderr)
    sys.exit(2)


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Return (subcommand, args after it), skipping global options."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg, args[i + 1 :]
    return None, []


def _check_git(args: list[str]) -> None:
    if "--no-verify" in args:
        _deny("Blocked: --no-verify bypasses hooks (CLAUDE.md safeguards).")

    subcommand, push_args = _git_subcommand(args)
    if subcommand != "push":
        return

    positional: list[str] = []
    for arg in push_args:
        if arg.startswith("--"):
            if any(arg == opt or arg.startswith(opt + "=") for opt in _FORCE_LONG_OPTS):
                _deny("Blocked: force push is forbidden (CLAUDE.md safeguards).")
            continue
        if arg.startswith("-") and len(arg) > 1:
            if "f" in arg[1:]:
                _deny("Blocked: force push (-f) is forbidden (CLAUDE.md safeguards).")
            continue
        positional.append(arg)

    if len(positional) < 2:
        _deny(
            "Blocked: bare `git push` may target main. Name the remote and a "
            "feature branch explicitly, e.g. `git push -u origin <branch>`."
        )

    for refspec in positional[1:]:
        if refspec.startswith("+"):
            _deny("Blocked: forced refspec (+) is forbidden (CLAUDE.md safeguards).")
        destination = refspec.rsplit(":", 1)[-1]
        if destination in ("main", "refs/heads/main"):
            _deny("Blocked: pushing to main is forbidden. Open a PR from a feature branch.")


def _check_rm(args: list[str]) -> None:
    recursive = False
    force = False
    for arg in args:
        if arg == "--recursive":
            recursive = True
        elif arg == "--force":
            force = True
        elif arg.startswith("-") and not arg.startswith("--"):
            flags = arg[1:]
            if "r" in flags or "R" in flags:
                recursive = True
            if "f" in flags:
                force = True
    if recursive and force:
        _deny("Blocked: `rm` with combined recursive+force flags is deny-listed (CLAUDE.md).")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        command = payload.get("tool_input", {}).get("command", "")
    except Exception:  # fail-open on any parse/shape surprise
        sys.exit(0)
    if not isinstance(command, str) or not command:
        sys.exit(0)

    for segment in re.split(r"\|\||&&|;|\||\n", command):
        try:
            tokens = shlex.split(segment, comments=True, posix=True)
        except ValueError:
            continue  # unparsable segment: fail-open
        if not tokens:
            continue
        name = tokens[0].rsplit("/", 1)[-1]
        if name == "claude":
            _deny("Blocked: direct `claude` CLI invocation. Subagents spawn via the Agent tool.")
        elif name == "git":
            _check_git(tokens[1:])
        elif name == "rm":
            _check_rm(tokens[1:])

    sys.exit(0)


if __name__ == "__main__":
    main()
