"""Full command lines of running processes, and the Claude Code sessions the app started. (Writers holding a
review's litrev lock come from M1b's locks.lock_holders.)"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def list_processes() -> list[tuple[int, str]]:
    """(pid, full command line) of every process (`ps -axww`)."""
    try:
        out = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    procs = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            procs.append((int(pid), cmd.strip()))
    return procs


def app_sessions(appdir: Path, procs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Claude Code processes the app started: their argv names a settings file under <appdir>/sessions/ (step
    sessions and publish windows alike)."""
    mark = str(appdir / "sessions") + "/"
    return [(pid, cmd) for pid, cmd in procs if mark in cmd and pid != os.getpid()]
