#!/usr/bin/env python3
"""Decide whether this launch was started by a person at a terminal.

A restored session may inherit its recorded launch grant (permission mode,
approval, sandbox) only when the launch is top-level. Launch records are
same-uid files, so this guards against a grant carried over by accident or by
an agent's own command, not against a hostile same-uid process. A command that
an agent types into a terminal pane is out of scope.

Top-level requires all of:
- stdin is a terminal whose foreground process group is ours (a detached,
  backgrounded or setsid'd process is not);
- no agent marker in the environment (markers can only withhold);
- a complete process ancestry with no agent executable, ending at pid 1 within
  MAX_HOPS.
Anything unknown is not top-level.

CLI: harness_launch_context.py top-level [--pstable FILE] [--start-pid PID]
prints `top_level=1` (exit 0) or `top_level=0 reason=<reason>` (exit 1).
"""
import os
import subprocess
import sys

MAX_HOPS = 64
AGENT_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "HARNESS_SESSION_ID",
    # A dispatcher that strips HARNESS_SESSION_ID can still signal a nested launch.
    "HARNESS_PARENT_SESSION_ID",
)
# `node` covers an npm-installed Claude Code, whose executable is node.
AGENT_EXECUTABLES = ("claude", "codex", "node")


def process_table(path=None):
    """pid -> (ppid, command) from a fixture file or the live `ps`; None if unreadable."""
    try:
        if path:
            with open(path, encoding="utf-8") as stream:
                text = stream.read()
        else:
            text = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,comm="], capture_output=True,
                                  text=True, timeout=5, check=True).stdout
        table = {}
        for line in text.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                table[int(parts[0])] = (int(parts[1]), parts[2].strip())
        return table
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ancestry_reason(table, start_pid, max_hops=MAX_HOPS):
    """None when the ancestry is complete and agent-free, else the reason."""
    if table is None:
        return "ps_unreadable"
    pid = start_pid
    for _ in range(max_hops):
        if pid == 1:
            return None
        if pid not in table:
            return "pid_missing"
        ppid, command = table[pid]
        if os.path.basename(command) in AGENT_EXECUTABLES:
            return "agent_ancestor"
        pid = ppid
    return "hop_limit"


def terminal_reason(fd=0):
    try:
        if not os.isatty(fd):
            return "no_tty"
        if os.tcgetpgrp(fd) != os.getpgrp():
            return "background"
    except OSError:
        return "no_tty"
    return None


def launch_top_level(environ=None, table=None, start_pid=None, tty_reason=None):
    """(True, None) for a top-level launch, else (False, reason)."""
    environ = os.environ if environ is None else environ
    for name in AGENT_MARKERS:
        if environ.get(name):
            return False, "env:" + name
    reason = terminal_reason() if tty_reason is None else tty_reason or None
    if reason:
        return False, reason
    reason = ancestry_reason(process_table() if table is None else table,
                             os.getpid() if start_pid is None else start_pid)
    if reason:
        return False, reason
    return True, None


def main(argv):
    if not argv or argv[0] != "top-level" or len(argv) % 2 == 0:
        print("usage: harness_launch_context.py top-level [--pstable FILE] [--start-pid PID]", file=sys.stderr)
        return 2
    pstable, start_pid = None, None
    for flag, value in zip(argv[1::2], argv[2::2]):
        if flag == "--pstable":
            pstable = value
        elif flag == "--start-pid" and value.isdigit():
            start_pid = int(value)
        else:
            return 2
    table = process_table(pstable)
    if table is None:
        table = {}
        ok, reason = False, "ps_unreadable"
    else:
        ok, reason = launch_top_level(table=table, start_pid=start_pid)
    print("top_level=1" if ok else "top_level=0 reason=" + reason)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
