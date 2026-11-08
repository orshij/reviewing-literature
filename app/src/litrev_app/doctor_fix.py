"""Fixes the Doctor page can start: log in (Terminal window), update Claude Code (Terminal window), build the
archived-leaderboards index (background download, then `litrev.py pwc-index`)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from . import terminal
from .doctor_checks import Inputs, Runner, clean_env, pwc_dir, run_cmd

PWC_SETS = {"evaluation-tables": "eval", "links-between-paper-and-code": "links", "datasets": "ds"}  # litrev's globs
PWC_LIST_URL = "https://huggingface.co/api/datasets/pwc-archive/{name}/parquet"
UA = {"User-Agent": "litrev-app (reviewing-literature)"}


class Jobs:
    """Background jobs by key: {'state': running|done|failed, 'progress', 'error'}; one run per key at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def start(self, key: str, fn: Callable[[Callable[[str], None]], object]) -> bool:
        with self._lock:
            if self._state.get(key, {}).get("state") == "running":
                return False
            self._state[key] = {"state": "running", "progress": "starting", "error": ""}

        def progress(msg: str) -> None:
            with self._lock:
                self._state[key]["progress"] = msg

        def body() -> None:
            try:
                fn(progress)
                end = {"state": "done", "progress": "", "error": ""}
            except Exception as e:                  # shown on the Doctor page
                end = {"state": "failed", "progress": "", "error": str(e)[:300]}
            with self._lock:
                self._state[key] = end

        t = threading.Thread(target=body, daemon=True, name=f"doctor-{key}")
        self._threads[key] = t
        t.start()
        return True

    def join(self, key: str, timeout: float | None = None) -> None:
        t = self._threads.get(key)
        if t:
            t.join(timeout)


JOBS = Jobs()


def get_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read().decode())


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def build_pwc(inp: Inputs, progress: Callable[[str], None], *, run: Runner = run_cmd, get_json=get_json,
              download=download) -> dict:
    """sources.md: download the pwc-archive parquet shards into the pwc cache, then run `litrev.py pwc-index`
    (which refuses to run without `eval_*.parquet`). A shard already on disk is not fetched again."""
    if not inp.skill:
        raise RuntimeError("the skill was not found; fix that first")
    cache = pwc_dir(inp)
    cache.mkdir(parents=True, exist_ok=True)
    files = []
    for name, prefix in PWC_SETS.items():
        shards = (get_json(PWC_LIST_URL.format(name=name)).get("default") or {}).get("train") or []
        if not shards:
            raise RuntimeError(f"Hugging Face lists no parquet shards for pwc-archive/{name}")
        files += [(u, cache / f"{prefix}_{i}.parquet") for i, u in enumerate(shards)]
    for n, (url, dest) in enumerate(files, 1):
        if dest.exists() and dest.stat().st_size > 0:
            continue
        progress(f"downloading file {n} of {len(files)} ({dest.name})")
        part = dest.with_name(dest.name + ".part")
        download(url, part)
        part.replace(dest)
    progress("indexing (a minute or two)")
    env = {**clean_env(inp.env), "LITREV_HOME": str(inp.root), "LITREV_PWC_CACHE": str(cache)}
    p = run([inp.python, inp.skill / "scripts" / "litrev.py", "pwc-index"], env=env, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError(f"pwc-index failed: {(p.stderr or p.stdout).strip()[-300:]}")
    out = p.stdout.strip().splitlines()
    return json.loads(out[-1]) if out else {}


def install_voice(inp: Inputs, progress: Callable[[str], None], *, run: Runner = run_cmd) -> dict:
    """`litrev.py voice-setup` from the app's own process (a session may not install packages): the venv, the pinned
    packages, then one spoken and heard sentence so the models download now."""
    if not inp.skill:
        raise RuntimeError("the skill was not found; fix that first")
    progress("installing Kokoro and mlx-whisper, then downloading the models (a few minutes)")
    home = Path.home()
    env = {**clean_env(inp.env), "LITREV_HOME": str(inp.root),
           "PATH": os.pathsep.join([inp.env.get("PATH", ""), str(home / ".local" / "bin"), "/opt/homebrew/bin",
                                    "/usr/local/bin", "/usr/bin", "/bin"])}
    p = run([inp.python, inp.skill / "scripts" / "litrev.py", "voice-setup"], env=env, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError(f"voice-setup failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return {"ok": True}


def fix(check_id: str, inp: Inputs, *, opener: Callable[[Path], None] = terminal.open_command, jobs: Jobs = JOBS,
        run: Runner = run_cmd, get_json=get_json, download=download) -> dict:
    """Start the fix for one check: {'started': bool, 'message': str}. KeyError when the app has no fix for it."""
    if check_id in ("auth", "claude") and not inp.claude:
        return {"started": False, "message": "Install Claude Code first: run install.sh again."}
    if check_id == "auth":
        cmd = terminal.write_command(inp.appdir / "login.command", "log in to Claude Code", [
            f"cd {terminal.q(inp.home)}", terminal.claude_line(inp.claude, ["auth", "login", "--claudeai"])])
        opener(cmd)
        return {"started": True, "message": "A Terminal window opened. Log in there (a browser page asks you to "
                                            "confirm), then press Check again."}
    if check_id == "claude":
        cmd = terminal.write_command(inp.appdir / "update-claude.command", "update Claude Code",
                                     [terminal.claude_line(inp.claude, ["update"])])
        opener(cmd)
        return {"started": True, "message": "A Terminal window opened to update Claude Code. When it finishes, "
                                            "press Check again."}
    if check_id == "video":
        started = jobs.start("video", lambda progress: install_voice(inp, progress, run=run))
        return {"started": started, "message": "Installing the voice tools in the background (a few minutes)."
                if started else "Already installing."}
    if check_id == "pwc":
        started = jobs.start("pwc", lambda progress: build_pwc(inp, progress, run=run, get_json=get_json,
                                                               download=download))
        return {"started": started, "message": "Building the archived-leaderboards index in the background."
                if started else "Already building."}
    raise KeyError(check_id)
