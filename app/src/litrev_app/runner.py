"""Run one Claude Code session as a child process: stdout to an events file, tail it, enforce the watchdog."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .events import Activity, StreamState

EVENT_HOOKS: list[Callable[[dict], None]] = []   # app-wide observers of every stream event (M2: the usage meter)


@dataclass
class SessionSpec:
    sid: str
    argv: list
    env: dict
    cwd: Path
    events_path: Path
    stderr_path: Path


@dataclass
class Limits:
    init_s: float = 120          # no system/init by then -> stop (spike S8: init arrives in 3-6 s)
    idle_s: float = 1200         # no output for 20 min of awake time, unless a question is open
    hard_s: float = 8 * 3600
    poll_s: float = 0.5
    grace_int: float = 60        # SIGINT -> wait -> SIGTERM -> wait -> SIGKILL
    grace_term: float = 30


class Session:
    """A running session in its own process group. `stop()` ends it (SIGINT, then SIGTERM, then SIGKILL)."""

    def __init__(self, spec: SessionSpec, caffeinate: bool = True, limits: Limits | None = None):
        self.spec, self.limits = spec, limits or Limits()
        spec.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._out, self._err = open(spec.events_path, "w"), open(spec.stderr_path, "w")
        self.proc = subprocess.Popen(spec.argv, cwd=spec.cwd, env=spec.env, stdout=self._out, stderr=self._err,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self._caff = None
        if caffeinate and Path("/usr/bin/caffeinate").exists():     # idle sleep only; lid-close sleep still happens
            self._caff = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(self.proc.pid)])
        self._pos, self._buf = 0, b""
        self.stop_reason: str | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def read_events(self) -> list[dict]:
        # Bytes, not text: a live writer can stop mid-way through a multi-byte character, and a text-mode read()
        # raises UnicodeDecodeError on it. Only complete lines are decoded.
        with open(self.spec.events_path, "rb") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        out = []
        for line in lines:
            if line.strip():
                try:
                    out.append(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    pass
        return out

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        threading.Thread(target=self._stop_sequence, daemon=True).start()

    def _stop_sequence(self) -> None:
        for sig, wait in ((signal.SIGINT, self.limits.grace_int), (signal.SIGTERM, self.limits.grace_term),
                          (signal.SIGKILL, None)):
            if self.proc.poll() is not None:
                return
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                self.proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def close(self) -> None:
        for f in (self._out, self._err):
            f.close()
        if self._caff and self._caff.poll() is None:
            self._caff.terminate()


def run_session(session: Session, on_activity: Callable[[Activity], None] | None = None, limits: Limits | None = None,
                question_open: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep,
                on_event: Callable[[dict], None] | None = None) -> tuple[StreamState, int | None, str | None]:
    """Tail the session until it exits. Returns (stream state, exit code, stop reason: 'user', 'watchdog' or None)."""
    limits = limits or session.limits
    session.limits = limits
    st = StreamState()
    t0 = last = clock()

    def drain() -> bool:
        evs = session.read_events()
        for e in evs:
            for hook in (*EVENT_HOOKS, *((on_event,) if on_event else ())):
                try:
                    hook(e)
                except Exception:          # an observer must never break the session tail
                    pass
            for a in st.feed(e):
                if on_activity:
                    on_activity(a)
        return bool(evs)

    while True:
        got = drain()
        now = clock()
        if got or question_open():
            last = now
        if session.proc.poll() is not None:
            drain()
            session.close()
            return st, session.proc.returncode, session.stop_reason
        if session.stop_reason is None and (
                (not st.inits and now - t0 > limits.init_s) or now - last > limits.idle_s or now - t0 > limits.hard_s):
            session.stop("watchdog")
        sleep(limits.poll_s)
