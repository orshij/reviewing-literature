"""Shared helpers for the M2 tests: an App stand-in with M1b's contract (cfg, settings, engine, scheduler, bus), a
router that records routes and dispatches like M1b's (regex with named groups), a mini review made with the real
litrev, and the static-script checks."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from litrev_app.orchestrator import AppConfig, Engine
from litrev_app.runner import Limits
from litrev_app.store import RunState, Store

SKILL = Path(os.environ.get("LITREV_SKILL", Path.home() / ".claude/skills/reviewing-literature"))
HAS_SKILL = (SKILL / "scripts" / "litrev.py").exists()
FAKE = Path(__file__).parent / "fake_claude.py"
FAST = Limits(init_s=10, idle_s=30, hard_s=120, poll_s=0.05, grace_int=2, grace_term=2)
DATE = "2026-10-10"
APP_DIR = Path(__file__).resolve().parents[1]
STATIC = APP_DIR / "src" / "litrev_app" / "static"


class FakeBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._cv = threading.Condition()

    def publish(self, slug: str, event: dict) -> None:
        with self._cv:
            self.events.append((slug, event))
            self._cv.notify_all()

    def subscribe(self, slug: str):
        raise NotImplementedError("tests read FakeBus.events")

    def wait_for(self, pred, timeout: float = 20.0, since: int = 0) -> dict:
        end = time.monotonic() + timeout
        with self._cv:
            while True:
                for _, e in self.events[since:]:
                    if pred(e):
                        return e
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no matching event; last: {self.events[-3:]}")
                self._cv.wait(left)


class FakeScheduler:
    def __init__(self):
        self.woken = 0

    def wake(self) -> None:
        self.woken += 1


def Req(method: str, path: str, body=None):
    return SimpleNamespace(method=method, path=path, query={}, json=body, headers={})


class FakeRouter:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        self.routes.append((method, re.compile(pattern), handler))

    def call(self, app, method: str, path: str, body=None):
        for m, rx, handler in self.routes:
            hit = rx.match(path)
            if m == method and hit:
                return handler(app, Req(method, path, body), **hit.groupdict())
        raise LookupError(f"{method} {path}")


def scenario(monkeypatch, tmp_path: Path, sc: dict) -> Path:
    """Point the fake claude at `sc` and log every invocation's argv to tmp_path/argv.jsonl."""
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(sc))
    p.with_name(p.name + ".count").unlink(missing_ok=True)
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(p))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "argv.jsonl"))
    return p


def argv_log(tmp_path: Path) -> list[list[str]]:
    f = tmp_path / "argv.jsonl"
    return [json.loads(line)["argv"] for line in f.read_text().splitlines()] if f.exists() else []


def review_env_log(tmp_path: Path) -> list[str | None]:
    """Each invocation's LITREV_APP_REVIEW (security F1: litrev.py confines the session to that review)."""
    f = tmp_path / "argv.jsonl"
    return [json.loads(line)["env"]["LITREV_APP_REVIEW"] for line in f.read_text().splitlines()] if f.exists() else []


def make_app(tmp_path: Path, **cfg):
    c = AppConfig(root=tmp_path, skill=SKILL, claude=FAKE, python=Path(sys.executable), appdir=tmp_path / "appdir",
                  limits=FAST, caffeinate=False, **cfg)
    return SimpleNamespace(cfg=c, settings={}, engine=Engine(c), scheduler=FakeScheduler(), bus=FakeBus())


def import_papers(app, review: Path, records: list[dict]) -> list[str]:
    f = review / "cache" / f"import-{time.monotonic_ns()}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(records))
    p = app.engine.litrev(review).run(["import", "--table", "papers", "--file", str(f)])
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def candidate(letter: str, n: int, date: str = DATE) -> dict:
    """A new paper as `refresh` stores it: an unscreened candidate stamped with the refresh date."""
    return {"title": f"New paper {letter}", "year": 2026, "ids": {"arxiv": f"2610.000{10 + n}"},
            "status": "candidate", "discovered_by_refresh": date, "abstract": f"About {letter}.",
            "url": f"https://arxiv.org/abs/2610.000{10 + n}"}


def mini_review(app, slug: str = "t", cap: int = 2, included: int = 2) -> Path:
    """A finished-looking review: `included` papers included (at the cap by default), RunState step 7 done."""
    r = app.engine.create_review(slug, seed="Depth Anything", purpose="test review", cap=cap, years=3)
    if included:
        import_papers(app, r, [{"title": f"Old paper {i}", "year": 2025, "ids": {"arxiv": f"2501.0000{i}"},
                                "status": "included"} for i in range(1, included + 1)])
    Store(r).save(RunState(step=7, phase="done"))
    return r


def check_script(name: str) -> None:
    """An M2 script never parses HTML, is loaded from index.html's feature-scripts block, and is valid JS."""
    src = (STATIC / name).read_text()
    for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert api not in src, f"{name} uses {api}"
    html = (STATIC / "index.html").read_text()
    assert "<!-- feature scripts -->" in html and name in html.split("<!-- feature scripts -->", 1)[1]
    node = shutil.which("node")
    if node:
        p = subprocess.run([node, "--check", str(STATIC / name)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


def ships(rel: str) -> bool:
    """True when pyproject's package-data for litrev_app covers `rel` (e.g. 'static/chat.js')."""
    import tomllib
    data = tomllib.loads((APP_DIR / "pyproject.toml").read_text())
    pats = data["tool"]["setuptools"]["package-data"]["litrev_app"]
    return any(fnmatch.fnmatch(rel, pat) for pat in pats)
