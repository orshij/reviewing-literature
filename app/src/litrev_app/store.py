"""Per-review app state in <review>/cache/app/ (gitignored, ignored by skill mode)."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ask_mcp import read_json, write_json_atomic

SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")   # a Claude Code session id
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")     # a session id or chat tag as a file name: no path parts


def safe_name(s) -> bool:
    """A session id from state.json, chat.json, publish.json or sessions.jsonl may name a file only when it has no
    path parts: step and chat sessions can write those files, and the app process is not held to the session's
    permission rules (security F1-F3, 2026-10-21)."""
    return isinstance(s, str) and bool(NAME_RE.match(s))


@dataclass
class RunState:
    step: int = 0
    phase: str = "idle"              # idle | running | waiting_user | paused | failed | done
    auto: bool = True
    paused_until: float | None = None
    message: str = ""
    sid: str | None = None
    pid: int | None = None
    no_progress: int = 0
    last_digest: str = ""
    retries: int = 0
    cost_usd: float = 0.0
    answers: list = field(default_factory=list)   # recorded since the last session; the next brief carries them
    notes: str = ""                  # standing instruction for every session until the run ends (refresh, chat)


class Store:
    def __init__(self, review: Path):
        self.review = review
        self.dir = review / "cache" / "app"

    def load(self) -> RunState:
        d = read_json(self.dir / "state.json")
        if not isinstance(d, dict):
            return RunState()
        known = RunState.__dataclass_fields__
        return RunState(**{k: v for k, v in d.items() if k in known})

    def save(self, st: RunState) -> None:
        write_json_atomic(self.dir / "state.json", asdict(st))

    def log_session(self, rec: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "sessions.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def sessions(self) -> list[dict]:
        p = self.dir / "sessions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def ask(self) -> dict | None:
        return read_json(self.dir / "ask.json")

    def publish_waiting(self) -> bool:
        """The person is approving a publish in Terminal: no step, refresh or chat_rw may change the hub meanwhile
        (open item of the 2026-10-16 security scan). Read from disk, so it holds across an app restart."""
        d = read_json(self.dir / "publish.json")
        return isinstance(d, dict) and d.get("state") == "waiting_approval"

    def put_ask(self, d: dict) -> None:
        write_json_atomic(self.dir / "ask.json", d)

    def take_blocked(self, sid: str) -> dict | None:
        """blocked.json from the session that just ended, renamed so it is acted on once. A sid with path parts
        gives None and moves nothing."""
        if not safe_name(sid):
            return None
        p = self.dir / "blocked.json"
        d = read_json(p)
        if d is not None:
            p.rename(self.dir / f"blocked-{sid}.json")
        return d

    def report(self, sid: str) -> dict | None:
        return read_json(self.dir / f"report-{sid}.json") if safe_name(sid) else None

    def events_path(self, sid: str) -> Path:
        if not safe_name(sid):
            raise ValueError(f"not a session id: {sid!r}")
        return self.dir / "events" / f"{sid}.jsonl"

    def stderr_path(self, sid: str) -> Path:
        if not safe_name(sid):
            raise ValueError(f"not a session id: {sid!r}")
        return self.dir / "events" / f"{sid}.stderr"
