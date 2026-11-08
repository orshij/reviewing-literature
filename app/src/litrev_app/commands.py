"""Subcommands cli.py hands over: `litrev-app doctor [--json]` and `litrev-app sessions [--json]`."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import proc
from .ask_mcp import read_json
from .doctor_checks import app_paths
from .store import Store


def server_pid(appdir: Path) -> int | None:
    """The app server's pid from server.json (M1b), if that process is alive."""
    d = read_json(appdir / "server.json")
    try:
        pid = int(d.get("pid")) if isinstance(d, dict) else None
        if pid:
            os.kill(pid, 0)
        return pid
    except PermissionError:
        return pid
    except (TypeError, ValueError, ProcessLookupError):
        return None


def active_reviews(root: Path) -> list[str]:
    """Reviews whose app state says a step is running or about to start (phase 'running')."""
    return [s.parents[2].name for s in sorted((root / "reviews").glob("*/cache/app/state.json"))
            if Store(s.parents[2]).load().phase == "running"]


def sessions_main(argv: list[str], *, procs=proc.list_processes, appdir: Path | None = None,
                  root: Path | None = None) -> int:
    """Exit 0 when nothing runs, 3 (and list it) when a Claude Code session the app started is alive, or when the app
    server is up and a review is mid-run (its scheduler would start the next step). update.sh refuses on 3: a plugin
    update moves the skill folder out from under a running session."""
    if (appdir is None) != (root is None):
        raise ValueError("pass both appdir and root, or neither")
    if appdir is None:
        root, appdir, _ = app_paths()
    live = proc.app_sessions(appdir, procs())
    busy = active_reviews(root) if server_pid(appdir) else []
    if "--json" in argv:
        print(json.dumps({"sessions": [{"pid": p, "command": c} for p, c in live], "running_reviews": busy}))
    else:
        for pid, cmd in live:
            print(f"session pid {pid}: {cmd[:160]}")
        for slug in busy:
            print(f"review {slug}: a step is running or about to start")
        if not live and not busy:
            print("No app session is running.")
    return 3 if live or busy else 0


def dispatch(argv: list[str]) -> int | None:
    """Run `doctor` or `sessions`; None for every other subcommand (cli.py handles those)."""
    if not argv:
        return None
    if argv[0] == "doctor":
        from .doctor import cli_main
        return cli_main(argv[1:])
    if argv[0] == "sessions":
        return sessions_main(argv[1:])
    return None
