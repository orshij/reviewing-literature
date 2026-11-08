"""Publish feature (spec: Publish handoff). POST /api/reviews/<slug>/publish renders the hub if needed, writes
<appdir>/sessions/<sid>/publish.command and opens it in Terminal, where the person approves the publish in an
interactive Claude Code session. The .command is in the app's folder, new and owner-only: step and chat sessions can
write the review, and Terminal runs the file later as the person (security F4). A watcher reads the link back from
meta.json (Claude ran set-meta) or from the session transcript (the app runs set-meta itself), then re-renders so
REVIEW.md carries the link. State, in cache/app/publish.json: idle -> waiting_approval -> published | failed
(cancel -> idle)."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import locks, proc, terminal
from .ask_mcp import read_json, write_json_atomic
from .handoff import (URL_RE, MAX_HUB_BYTES, command_lines, find_transcript, meta_url, publish_prompt, publish_settings,
                      transcript_url)
from .store import SID_RE, Store

RENDER_FIRST = ("todo", "stale", "unknown")
APP_ATTR = "_litrev_publisher"


class PublishRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def projects_dir(env=None) -> Path:
    """Where Claude Code keeps session transcripts (~/.claude/projects, or under $CLAUDE_CONFIG_DIR)."""
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "projects"


class Publisher:
    def __init__(self, engine, *, bus=None, scheduler=None, opener: Callable[[Path], None] = terminal.open_command,
                 procs: Callable[[], list] = proc.list_processes,
                 lock_holders: Callable[[Path], list] = locks.lock_holders, projects: Path | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 poll_s: float = 2.0, grace_s: float = 30.0, start_timeout_s: float = 600.0,
                 timeout_s: float = 3600.0, autowatch: bool = True):
        self.engine, self.bus, self.scheduler, self.opener = engine, bus, scheduler, opener
        self.procs, self.lock_holders = procs, lock_holders
        self.projects = projects or projects_dir()
        self.clock, self.sleep, self.autowatch = clock, sleep, autowatch
        self.poll_s, self.grace_s, self.start_timeout_s, self.timeout_s = poll_s, grace_s, start_timeout_s, timeout_s
        self._lock = threading.Lock()          # state-file transitions
        self._tlock = threading.Lock()         # watcher threads
        self._threads: dict[str, threading.Thread] = {}

    @staticmethod
    def path(review: Path) -> Path:
        return review / "cache" / "app" / "publish.json"

    def load(self, review: Path) -> dict:
        d = read_json(self.path(review))
        return d if isinstance(d, dict) else {"state": "idle"}

    def save(self, review: Path, st: dict) -> None:
        write_json_atomic(self.path(review), st)

    def _meta(self, review: Path) -> dict:
        try:
            return self.engine.litrev(review).meta()
        except (OSError, ValueError):
            return {}

    def busy_reason(self, review: Path) -> str | None:
        """Why publishing now would collide with a writer, or None. Every app-side writer counts (security F4): a
        chat_rw message or a refresh (its fetch holds the writer lock before any session is live) could rewrite the
        hub or meta.json while the person approves."""
        if str(review) in getattr(self.engine, "live", {}) or (self.scheduler is not None
                                                              and self.scheduler.busy(review)):
            return "A step session is running on this review. Wait for it to finish (or stop it), then publish."
        writers = getattr(self.engine, "writers", None)
        who = writers.holder(str(review)) if writers is not None else None
        if who == "chat":
            return "Claude is answering a chat message that may change this review. Publish when it has finished."
        if who:
            return f"A {who} is writing this review. Publish when it has finished."
        if Store(review).load().phase == "running":
            return ("This review is running: a step is in progress or about to start. Publish when it is done, or "
                    "stop it first.")
        holders = self.lock_holders(review)
        if holders:
            return ("A Claude Code session outside the app is writing to this review (pid "
                    + ", ".join(str(p) for p in holders) + "). Publish when it has finished.")
        return None

    def public(self, review: Path, st: dict | None = None) -> dict:
        st = st or self.load(review)
        hub = review / "rendered" / "hub.html"
        out = {k: st.get(k) for k in ("state", "url", "message", "started_at", "finished_at", "sid")}
        out["state"] = out["state"] or "idle"
        out["hub_url"] = self._meta(review).get("hub_url")
        out["hub_bytes"] = hub.stat().st_size if hub.exists() else None
        out["max_bytes"] = MAX_HUB_BYTES
        out["busy"] = None if out["state"] == "waiting_approval" else self.busy_reason(review)
        return out

    def _emit(self, review: Path, st: dict) -> None:
        if self.bus is not None:
            try:
                self.bus.publish(review.name, {"type": "publish", **self.public(review, st)})
            except Exception:
                pass

    def start(self, review: Path) -> dict:
        cfg = self.engine.cfg
        with self._lock:
            if self.load(review).get("state") == "waiting_approval":
                raise PublishRefused(409, "A publish is already waiting for your approval in the Terminal window.")
            why = self.busy_reason(review)
            if why:
                raise PublishRefused(409, why)
            if not cfg.claude or not Path(cfg.claude).exists():
                raise PublishRefused(409, "Claude Code was not found. Open Settings > Doctor.")
            lit = self.engine.litrev(review)
            rows = {r.get("key"): r.get("state") for r in lit.status().get("rows") or []}
            hub = review / "rendered" / "hub.html"
            # an over-limit hub is rendered again: since skill 1.34.0 render trims the page to fit, or exits 1 and
            # names the parts that do not fit (the screened pool, not the paper cap, is what grows the page)
            if rows.get("render", "todo") in RENDER_FIRST or (hub.exists() and hub.stat().st_size > MAX_HUB_BYTES):
                p = lit.run(["render"])
                if p.returncode != 0:
                    raise PublishRefused(500, "Rendering the hub failed: " + (p.stderr or p.stdout).strip()[-600:])
            if not hub.exists():
                raise PublishRefused(409, "There is no rendered hub yet. Run the review to step 7 first.")
            size = hub.stat().st_size
            if size > MAX_HUB_BYTES:            # an older skill writes the page whatever its size
                raise PublishRefused(413, f"The hub is {size / 2**20:.1f} MB, and a claude.ai artifact can be at most "
                                          "16 MB. Press Update in the header: the new skill makes the hub fit and says "
                                          "what it left out. Then publish again.")
            hub_url = lit.meta().get("hub_url")
            sid = str(uuid.uuid4())
            litrev_py = Path(cfg.skill) / "scripts" / "litrev.py"
            settings = Path(cfg.appdir) / "sessions" / sid / "settings.json"
            write_json_atomic(settings, publish_settings(python=Path(cfg.python), litrev=litrev_py, review=review))
            prompt = publish_prompt(review=review, hub=hub, python=Path(cfg.python), litrev=litrev_py,
                                    hub_url=hub_url)
            cmd = terminal.write_command(settings.parent / "publish.command", f"publish the {review.name} hub",
                                         command_lines(root=Path(cfg.root), claude=Path(cfg.claude), sid=sid,
                                                       settings=settings, review=review, prompt=prompt), fresh=True)
            st = {"state": "waiting_approval", "sid": sid, "started_at": self.clock(), "url": None,
                  "prev_url": hub_url, "message": "Waiting for your approval in the Terminal window.",
                  "hub_bytes": size, "command": str(cmd), "seen_process": False, "url_seen_at": None,
                  "finished_at": None}
            try:
                self.opener(cmd)
            except Exception as e:             # keep waiting: the person can open the file by hand
                st["message"] = f"Could not open Terminal ({e}). Double-click {cmd} in Finder to continue."
            self.save(review, st)
        self._emit(review, st)
        if self.autowatch:
            self.ensure_watcher(review)
        return self.public(review, st)

    def poll_once(self, review: Path) -> dict:
        """One watcher step. Claude's own set-meta wins; a URL seen only in the transcript is stored by the app once
        the window has closed or `grace_s` has passed (so the two don't race for the review's lock). The litrev
        writes (set-meta, a render of seconds on a big review) run outside the state lock, so Cancel never waits."""
        with self._lock:
            st = self.load(review)
            if st.get("state") != "waiting_approval":
                return st
            now, sid = self.clock(), st.get("sid")
            if not (isinstance(sid, str) and SID_RE.match(sid)):    # publish.json is session-writable: never search with it
                st.update(state="failed", finished_at=now, message="This publish lost its session id. Close the "
                                                                   "Terminal window. Press Publish to try again.")
                self.save(review, st)
                self._emit(review, st)
                return st
            alive = any(sid in cmd for _, cmd in self.procs())
            if alive:
                st["seen_process"] = True
            url, found = meta_url(self._meta(review), st["started_at"]), None
            if url and not URL_RE.fullmatch(url):
                url = None                               # hub_url is writable by step sessions: never trust its shape
            tp = find_transcript(self.projects, sid)
            mine = transcript_url(tp, review / "rendered" / "hub.html", Path(self.engine.cfg.root)) if tp else None
            if url and tp is not None and url != mine:
                url = None     # stored by another session (M1c review): only this session's publish counts
            if not url:
                found = mine
                if found:
                    st["url_seen_at"] = st.get("url_seen_at") or now
                    if alive and now - st["url_seen_at"] < self.grace_s:
                        found = None                     # Claude may still store it itself
            if not url and not found:
                if st.get("seen_process") and not alive:
                    st.update(state="failed", finished_at=now, message="The Claude Code window closed before the hub "
                                                                       "was published. Press Publish to try again.")
                elif not st.get("seen_process") and now - st["started_at"] > self.start_timeout_s:
                    st.update(state="failed", finished_at=now, message=(
                        f"Claude Code did not start in Terminal. Double-click {st.get('command')} in Finder, or check "
                        "Settings > Doctor, then press Publish again."))
                elif now - st["started_at"] > self.timeout_s:
                    st.update(state="failed", finished_at=now, message="Nothing was published within an hour. Close "
                                                                       "the Terminal window and press Publish again.")
            self.save(review, st)
        if not url and not found:
            if st["state"] != "waiting_approval":
                self._emit(review, st)
            return st
        lit = self.engine.litrev(review)
        by_app = False
        if not url:
            p = lit.run(["set-meta", "--field", "hub_url", "--json", json.dumps(found)])
            if p.returncode != 0:
                return self._finish(review, state="failed", url=found, message=(
                    f"Published at {found}, but storing the link failed: " + (p.stderr or p.stdout).strip()[-200:]))
            url, by_app = found, True
        r = lit.run(["render"])                   # outputs.md: render again so REVIEW.md and the xlsx carry the link
        notes = ["Published. You can close the Terminal window."]
        if st.get("prev_url") and url != st["prev_url"]:
            notes.append(f"This is a new link; the old one ({st['prev_url']}) keeps showing the previous version.")
        if by_app:
            notes.append("Claude didn't store the link, so the app did.")
        if r.returncode != 0:
            notes.append("Re-rendering after the publish failed: " + (r.stderr or r.stdout).strip()[-200:])
        return self._finish(review, state="published", url=url, message=" ".join(notes))

    def _finish(self, review: Path, **updates) -> dict:
        """End a publish, unless it was cancelled while litrev ran."""
        with self._lock:
            st = self.load(review)
            if st.get("state") != "waiting_approval":
                return st
            st.update(updates, finished_at=self.clock())
            self.save(review, st)
        self._emit(review, st)
        return st

    def resume_all(self) -> list[str]:
        """At app start: watch every review whose publish was waiting when the previous launch stopped (M1c review:
        the watch otherwise resumed only when someone opened the Publish panel)."""
        out = []
        for f in sorted((Path(self.engine.cfg.root) / "reviews").glob("*/cache/app/publish.json")):
            review = f.parent.parent.parent
            if self.load(review).get("state") == "waiting_approval":
                self.ensure_watcher(review)
                out.append(review.name)
        return out

    def ensure_watcher(self, review: Path) -> None:
        """Start the watcher when this review waits for a publish and none runs (also after an app restart)."""
        key = str(review)
        with self._tlock:
            t = self._threads.get(key)
            if (t and t.is_alive()) or self.load(review).get("state") != "waiting_approval":
                return
            t = threading.Thread(target=self._watch, args=(review,), daemon=True, name=f"publish-{review.name}")
            self._threads[key] = t
            t.start()

    def _watch(self, review: Path) -> None:
        while True:
            try:
                st = self.poll_once(review)
            except Exception as e:
                with self._lock:
                    st = {**self.load(review), "state": "failed", "finished_at": self.clock(),
                          "message": f"Publish watcher error: {e}"}
                    self.save(review, st)
                self._emit(review, st)
            if st.get("state") != "waiting_approval":
                return
            self.sleep(self.poll_s)

    def join(self, review: Path, timeout: float | None = None) -> None:
        t = self._threads.get(str(review))
        if t:
            t.join(timeout)

    def cancel(self, review: Path) -> dict:
        with self._lock:
            st = self.load(review)
            if st.get("state") == "waiting_approval":
                st.update(state="idle", finished_at=self.clock(),
                          message="Cancelled. You can close the Terminal window.")
                self.save(review, st)
        self._emit(review, st)
        return self.public(review, st)


_make_lock = threading.Lock()


def publisher(app) -> Publisher:
    """One Publisher per app, created on first use. The scheduler says whether a step session (live, or adopted from
    an earlier launch) holds the review; lock holders that are the app's own processes don't count."""
    with _make_lock:
        p = getattr(app, APP_ATTR, None)
        if p is None:
            sched = getattr(app, "scheduler", None)
            holders = ((lambda r: locks.outside_writers(r, sched.own_pids())["lock"]) if sched is not None
                       else locks.lock_holders)
            p = Publisher(app.engine, bus=getattr(app, "bus", None), scheduler=sched, lock_holders=holders)
            setattr(app, APP_ATTR, p)
        return p


def on_start(app) -> None:
    """Server start hook (server.start_features): pick up publishes that were waiting before a restart."""
    publisher(app).resume_all()


def _review(app, slug: str) -> Path | None:
    r = app.engine.review_path(slug)
    return r if (r / "meta.json").exists() else None


def get_publish(app, req, slug):
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review '{slug}'."}, {}
    p = publisher(app)
    p.ensure_watcher(r)
    return 200, p.public(r), {}


def post_publish(app, req, slug):
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review '{slug}'."}, {}
    body = getattr(req, "json", None)
    body = body if isinstance(body, dict) else {}
    p = publisher(app)
    if body.get("action") == "cancel":
        return 200, p.cancel(r), {}
    try:
        return 202, p.start(r), {}
    except PublishRefused as e:
        return e.status, {**p.public(r), "error": e.message}, {}


def register(router) -> None:
    router.add("GET", r"^/api/reviews/(?P<slug>[a-z0-9-]+)/publish$", get_publish)
    router.add("POST", r"^/api/reviews/(?P<slug>[a-z0-9-]+)/publish$", post_publish)
