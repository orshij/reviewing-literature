"""Claude usage meter (M2). Every rate_limit_event any app session streams (spike S3: rate_limit_info.status,
resetsAt, rateLimitType and unifiedWindows.{five_hour, seven_day}.{utilization 0-1, resetsAt}) updates one app-wide
record in <appdir>/usage.json, served at GET /api/usage. A window whose reset time has passed is shown as unknown."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from . import runner
from .ask_mcp import read_json, write_json_atomic

WARN_WEEKLY = 0.8
WINDOWS = ("five_hour", "seven_day")


def _when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


class Meter:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        self.info: dict | None = None
        self.at: float | None = None
        if path:
            self._load()

    def _load(self) -> None:
        d = read_json(self.path) if self.path else None
        if isinstance(d, dict) and isinstance(d.get("info"), dict):
            self.info, self.at = d["info"], d.get("at")

    def _write(self) -> None:
        if self.path and self.info is not None:
            write_json_atomic(self.path, {"info": self.info, "at": self.at})

    def bind(self, path: Path) -> None:
        with self._lock:
            if self.path == path:
                return
            self.path = path
            if self.info is None:
                self._load()
            else:
                self._write()

    def observe(self, e: dict) -> None:
        if not isinstance(e, dict) or e.get("type") != "rate_limit_event":
            return
        info = e.get("rate_limit_info")
        if not isinstance(info, dict):
            return
        with self._lock:
            self.info, self.at = info, self.clock()
            self._write()

    def _window(self, name: str, now: float) -> dict:
        w = ((self.info or {}).get("unifiedWindows") or {}).get(name) or {}
        u, r = w.get("utilization"), w.get("resetsAt")
        if r is not None and r <= now:
            return {"utilization": None, "resets_at": r, "reset": True}
        return {"utilization": u, "resets_at": r, "reset": False}

    def snapshot(self) -> dict:
        with self._lock:
            now, info = self.clock(), self.info or {}
            out = {"known": self.info is not None, "observed_at": self.at, "status": info.get("status"),
                   **{w: self._window(w, now) for w in WINDOWS}, "limited": None}
            if info.get("status") == "rejected" and (info.get("resetsAt") or 0) > now:
                out["limited"] = {"type": info.get("rateLimitType"), "resets_at": info.get("resetsAt")}
        out["warn_new_review"] = self._warning(out)
        return out

    def warning(self) -> str | None:
        return self.snapshot()["warn_new_review"]

    @staticmethod
    def _warning(s: dict) -> str | None:
        if s.get("limited"):
            return (f"Claude's usage limit is reached until {_when(s['limited']['resets_at'])}. A new review would "
                    "wait until then.")
        wk = s["seven_day"]
        if wk["utilization"] is not None and wk["utilization"] > WARN_WEEKLY:
            when = f" (resets {_when(wk['resets_at'])})" if wk.get("resets_at") else ""
            return (f"Your weekly Claude usage is at {round(wk['utilization'] * 100)}%{when}. A new review uses a lot "
                    "of it (about 30 dollars plus 0.75 per paper, API-equivalent) and may pause until the reset.")
        return None


METER = Meter()


def meter(app) -> Meter:
    METER.bind(Path(app.cfg.appdir) / "usage.json")
    return METER


def get_usage(app, req):
    return 200, meter(app).snapshot(), {}


def register(router) -> None:
    if METER.observe not in runner.EVENT_HOOKS:
        runner.EVENT_HOOKS.append(METER.observe)
    router.add("GET", r"^/api/usage$", get_usage)
