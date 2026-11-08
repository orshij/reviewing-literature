"""Writers the app didn't start: a process holding the review's litrev lock, or a Claude Code session whose working
folder is in the review.

Two facts from this Mac shape it: `lsof -c claude` matches nothing (lsof names the CLI after its executable, the
version, e.g. 2.1.295), so Claude Code is found by `ps` command name; and Claude Code runs each Bash command in its
own process group, so "ours" means the app's pid or a session pid is an ancestor, not a shared process group."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

PS, LSOF = "/bin/ps", "/usr/sbin/lsof"


def _out(run, argv: list[str]) -> str:
    try:
        p = run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout or ""


def process_table(run=subprocess.run) -> dict[int, tuple[int, str]]:
    table = {}
    for line in _out(run, [PS, "-axo", "pid=,ppid=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return table


def lock_holders(review: Path, run=subprocess.run) -> list[int]:
    lock = review / "cache" / "litrev.lock"
    if not lock.exists():
        return []
    return [int(x) for x in _out(run, [LSOF, "-t", str(lock)]).split() if x.isdigit()]


def cwds(pids, run=subprocess.run) -> dict[int, str]:
    pids = list(pids)
    if not pids:
        return {}
    out, cur = {}, None
    for line in _out(run, [LSOF, "-a", "-p", ",".join(map(str, pids)), "-d", "cwd", "-Fpn"]).splitlines():
        if line.startswith("p") and line[1:].isdigit():
            cur = int(line[1:])
        elif line.startswith("n") and cur is not None:
            out[cur] = line[1:]
    return out


def is_ours(pid: int, table: dict, own_pids: set) -> bool:
    seen = set()
    while pid and pid not in seen:
        if pid in own_pids:
            return True
        seen.add(pid)
        pid = table.get(pid, (0, ""))[0]
    return False


def outside_writers(review: Path, own_pids: set, run=subprocess.run) -> dict:
    """{"lock": [pid], "claude": [pid], "message": str | None}. Own = the app process and its sessions (each session
    is a child of the app, or, after an app restart, the pid recorded in the review's state)."""
    table = process_table(run)
    lock = [p for p in lock_holders(review, run) if p in table and not is_ours(p, table, own_pids)]
    rv = os.path.realpath(review)
    claude_pids = [p for p, (_, comm) in table.items() if os.path.basename(comm) == "claude"]
    inside = lambda d: d == rv or d.startswith(rv + os.sep)
    claude = sorted(p for p, d in cwds(claude_pids, run).items()
                    if inside(os.path.realpath(d)) and not is_ours(p, table, own_pids))
    msg = None
    if lock:
        msg = ("A Claude Code session is working on this review right now. The app waits for each of its "
               "commands to finish before writing.")
    elif claude:
        msg = ("A Claude Code session is open in this review. If it changes the review while the app runs a step, "
               "both write to the same files.")
    return {"lock": lock, "claude": claude, "message": msg}


class OutsideCache:
    """`ps` + two `lsof` calls cost ~0.2 s; the review page asks often, so answers are reused for `ttl` seconds."""

    def __init__(self, ttl: float = 5.0, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def get(self, review: Path, own_pids: set, run=subprocess.run) -> dict:
        key = str(review)
        with self._lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        val = outside_writers(review, own_pids, run)
        with self._lock:
            self._cache[key] = (self.clock(), val)
        return val
