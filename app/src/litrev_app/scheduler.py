"""One step session at a time across all reviews (spec: 429s with ~6+ concurrent sessions). Wakes paused reviews
when their time comes, adopts sessions an earlier launch left running (they lead their own process group, so they
outlive the app), applies Run/Resume/Retry/Stop from the review page, and tells the person when a review needs
them (macOS notification + a `notify` event for the page)."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .events import StreamState
from .orchestrator import decide_next
from .outcome import classify
from .runner import Limits
from .store import RunState, Store, SID_RE, safe_name

log = logging.getLogger("litrev_app.scheduler")
SLUG_RE = re.compile(r"^[a-z0-9-]+$")


class Conflict(Exception):
    """The request doesn't fit the review's phase. The message is shown to the person."""


def notify_mac(title: str, body: str) -> None:
    """A macOS notification. `display notification` needs no Automation permission; the text goes in as argv,
    never into the script."""
    try:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                        title[:120], body[:240]], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def session_cmdline(pid: int) -> str:
    try:
        return subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def owns_session(cmdline: str, sid: str) -> bool:
    """True when the process command line carries `--session-id <sid>` exactly and sid is a UUID. state.json is
    editable by step sessions, so a substring match could make the app adopt (and later signal) another process."""
    if not sid or not SID_RE.match(sid):
        return False
    words = cmdline.split()
    return any(w == "--session-id" and i + 1 < len(words) and words[i + 1] == sid for i, w in enumerate(words))


def first_open_step(status: dict) -> int | None:
    """The first step with a todo or stale row. The publish row is the app's Publish button, not a step's work."""
    steps = [r["step"] for r in status.get("rows") or [] if isinstance(r.get("step"), int)
             and r.get("state") in ("todo", "stale") and r.get("key") != "publish"]
    return min(steps) if steps else None


class Orphan:
    """A session started by an earlier launch of the app: tail its events file until the process exits."""

    def __init__(self, review: Path, sid: str, pid: int, limits: Limits | None = None):
        self.review, self.sid, self.pid = review, sid, pid
        # a sid with path parts (state.json is session-writable, F1/F3): no events file, so it classifies interrupted
        self.path = Store(review).events_path(sid) if safe_name(sid) else None
        self.state = StreamState()
        self.stop_reason: str | None = None
        self._pos, self._buf, self._stopped_at = 0, b"", None
        self.limits = limits or Limits()
        self._last_growth = time.monotonic()
        try:                                   # the session's start: when its events file was created
            st = self.path.stat()
            self._born = getattr(st, "st_birthtime", st.st_mtime)
        except (OSError, AttributeError):
            self._born = time.time()

    def read(self, on_activity=None) -> None:
        """Bytes, like runner.Session.read_events: a live writer can stop inside a multi-byte character."""
        if self.path is None:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if chunk:
            self._last_growth = time.monotonic()
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            try:
                e = json.loads(line.decode("utf-8", "replace")) if line.strip() else None
            except ValueError:
                e = None
            for a in self.state.feed(e) if isinstance(e, dict) else []:
                if on_activity:
                    on_activity(a)

    def alive(self) -> bool:
        return pid_alive(self.pid)

    def stop(self, reason: str = "user") -> None:
        if self.stop_reason is None:
            self.stop_reason, self._stopped_at = reason, time.monotonic()
            self._signal(signal.SIGINT)

    def check(self, question_open: bool) -> None:
        """The runner's watchdog for an adopted session: no output for idle_s (unless a question is open), or older
        than hard_s, stops it (M1b review: without this a hung adopted session blocked every review)."""
        if self.stop_reason is not None:
            return
        if question_open:
            self._last_growth = time.monotonic()
        if time.monotonic() - self._last_growth > self.limits.idle_s or time.time() - self._born > self.limits.hard_s:
            self.stop("watchdog")

    def escalate(self) -> None:
        """Same ladder as runner.Session.stop: SIGINT, then SIGTERM after 60 s, then SIGKILL after 90 s."""
        if self._stopped_at is not None:
            waited = time.monotonic() - self._stopped_at
            if waited > 90:
                self._signal(signal.SIGKILL)
            elif waited > 60:
                self._signal(signal.SIGTERM)

    def _signal(self, sig) -> None:
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


class Scheduler:
    def __init__(self, engine, bus, *, clock=time.time, notify=notify_mac, ready=lambda: True, tick_s: float = 2.0,
                 alive=pid_alive, cmdline=session_cmdline, limits: Limits | None = None):
        self.engine, self.bus, self.clock, self.notify, self.ready = engine, bus, clock, notify, ready
        self.limits = limits or getattr(getattr(engine, "cfg", None), "limits", None) or Limits()
        self.tick_s, self.alive, self.cmdline = tick_s, alive, cmdline
        self.orphans: dict[str, Orphan] = {}
        self.current: tuple[str, float] | None = None       # (str(review), start) of the session this thread runs
        self._asked: set[str] = set()                        # reviews whose current session already notified a question
        self._lock = threading.RLock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None

    # ---- what exists -------------------------------------------------------------------------------------------
    def reviews(self) -> list[Path]:
        base = Path(self.engine.cfg.root) / "reviews"
        if not base.is_dir():
            return []
        return sorted(p for p in base.iterdir() if SLUG_RE.match(p.name) and (p / "meta.json").is_file())

    def title(self, review: Path) -> str:
        try:
            meta = json.loads((review / "meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
        return str(meta.get("seed") or review.name)[:80]

    def busy(self, review: Path) -> bool:
        k = str(review)
        return k in self.engine.live or k in self.orphans or (self.current is not None and self.current[0] == k)

    def own_pids(self) -> set[int]:
        pids = {os.getpid()}
        pids.update(s.pid for s in list(self.engine.live.values()))
        pids.update(o.pid for o in list(self.orphans.values()))
        return pids

    def started_at(self, review: Path) -> float | None:
        cur = self.current
        if cur and cur[0] == str(review):
            return cur[1]
        o = self.orphans.get(str(review))
        if o:
            try:
                st = o.path.stat()
                return getattr(st, "st_birthtime", st.st_mtime)
            except OSError:
                return None
        return None

    # ---- the loop ----------------------------------------------------------------------------------------------
    def launch(self) -> None:
        """Adopt or finish sessions from an earlier launch BEFORE the server answers requests (M1b review: a Stop in
        the window before the thread's first recover() was lost), then start the loop."""
        try:
            self.recover()
        except Exception:
            log.exception("recover failed")
        self._thread = threading.Thread(target=self._loop, name="litrev-scheduler", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stops scheduling. A session in progress keeps running on its own; the next launch adopts it."""
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.tick()
            except Exception:
                log.exception("scheduler tick failed")
                worked = None
            if not worked and not self._stop.is_set():
                self._wake.wait(1.0 if self.orphans else self.tick_s)
                self._wake.clear()

    def recover(self) -> list[str]:
        """At app start, a review left `running` with a session id belongs to a session from an earlier launch.
        Alive (and its command line carries that id) → adopt it. Dead → classify it from its events file now, which
        usually queues "continue step N": the one automatic resume."""
        out = []
        for r in self.reviews():
            st = Store(r).load()
            if st.phase != "running" or not st.sid or self.busy(r):
                continue
            if st.pid and self.alive(st.pid) and owns_session(self.cmdline(st.pid), st.sid):
                self.orphans[str(r)] = Orphan(r, st.sid, st.pid, self.limits)
            else:
                self._finish(r, st, Orphan(r, st.sid, 0, self.limits), "the app restarted while this session ran")
            out.append(r.name)
        return out

    def tick(self) -> str | None:
        """One scheduling step; returns the slug it ran a session for, or None. Never runs two sessions at once:
        while an adopted session lives, nothing new starts."""
        if self.orphans:
            self._poll_orphans()
            if self.orphans:
                return None
        if not self.ready():
            return None
        review = self._pick()
        if review is None:
            return None
        self._run(review)
        return review.name

    def _poll_orphans(self) -> None:
        for key, o in list(self.orphans.items()):
            o.read(self._on_activity(o.review))
            if o.alive():
                o.check(question_open=(Store(o.review).ask() or {}).get("status") == "open")
                o.escalate()
                continue
            del self.orphans[key]
            self._finish(o.review, Store(o.review).load(), o, "adopted after an app restart")

    def _pick(self) -> Path | None:
        now, due = self.clock(), []
        with self._lock:
            for r in self.reviews():
                store = Store(r)
                if self.busy(r) or not (store.dir / "state.json").exists() or store.publish_waiting():
                    continue                       # a publish awaiting approval holds the review's steps
                st = store.load()
                if st.phase == "paused" and st.paused_until is not None and st.paused_until <= now:
                    st = replace(st, phase="running", paused_until=None)
                    store.save(st)
                    self._publish_state(r, st)
                if st.phase == "running" and not st.sid:
                    due.append(((store.dir / "state.json").stat().st_mtime, r))
        return min(due)[1] if due else None

    def _run(self, review: Path) -> None:
        store = Store(review)
        before = store.load()
        self._asked.discard(review.name)
        self.current = (str(review), self.clock())
        self._publish_state(review, replace(before, message=f"Starting step {before.step}"))
        try:
            after = self.engine.run_one(review, self._on_activity(review))
        except Exception:                      # run_one has already marked the review failed
            log.exception("session for %s raised", review.name)
            after = store.load()
        finally:
            self.current = None
        self._after(review, before, after)

    def _finish(self, review: Path, st: RunState, o: Orphan, note: str) -> RunState:
        o.read()
        store = Store(review)
        out = classify(o.state, None, o.stop_reason, datetime.now().astimezone())
        if out.kind == "startup_failed":      # no event at all: the app quit around the start, not a broken session
            out.kind = "interrupted"
        report, status = store.report(st.sid), self.engine.litrev(review).status()
        ask, blocked = store.ask(), store.take_blocked(st.sid)
        nxt = decide_next(st, out, report, status, ask, blocked, self.clock())
        if out.kind in ("ok", "max_turns", "watchdog") and not (ask and ask.get("status") in ("open", "parked")):
            nxt = replace(nxt, answers=[])
        store.log_session({"sid": st.sid if safe_name(st.sid) else None, "step": st.step, "start": None, "end": self.clock(), "outcome": out.kind,
                           "exit": None, "stop": o.stop_reason, "cost_usd": out.cost_usd, "turns": out.num_turns,
                           "denials": out.denials, "resets_at": out.resets_at, "report": report,
                           "result": out.result_text[-500:], "next_phase": nxt.phase, "next_step": nxt.step,
                           "note": note})
        store.save(nxt)
        self._after(review, st, nxt)
        return nxt

    # ---- events and notifications -----------------------------------------------------------------------------
    def _on_activity(self, review: Path):
        slug = review.name

        def publish(a) -> None:
            self.bus.publish(slug, {"type": "activity", **a.to_dict()})
            if a.kind == "ask" and slug not in self._asked:
                q = Store(review).ask()
                if q and q.get("status") not in ("open", "parked"):
                    return                     # a replayed question that was already answered
                self._asked.add(slug)
                self._notify(review, "Claude has a question for you", a.detail or "Open the review to answer it")
        return publish

    def _publish_state(self, review: Path, st: RunState) -> None:
        self.bus.publish(review.name, {"type": "state", "phase": st.phase, "step": st.step, "message": st.message,
                                       "paused_until": st.paused_until})

    def _notify(self, review: Path, headline: str, detail: str) -> None:
        title, body = f"Litrev: {self.title(review)}", f"{headline}: {detail}".strip()[:240]
        self.bus.publish(review.name, {"type": "notify", "title": title, "body": body})
        try:
            self.notify(title, body)
        except Exception:
            log.exception("notification failed")

    def _after(self, review: Path, before: RunState, after: RunState) -> None:
        self._publish_state(review, after)
        if after.phase == "waiting_user" and review.name not in self._asked:
            self._notify(review, "Claude has a question for you", after.message or "Open the review to answer it")
        elif after.phase == "failed":
            self._notify(review, "A step stopped with a problem", after.message)
        elif after.phase == "done" and before.phase != "done":
            self._notify(review, "Your review is ready", "Open it in Litrev to read the hub")

    # ---- buttons -------------------------------------------------------------------------------------------------
    def run(self, review: Path) -> RunState:
        return self._transition(review, "run")

    def resume(self, review: Path) -> RunState:
        return self._transition(review, "resume")

    def retry(self, review: Path) -> RunState:
        return self._transition(review, "retry")

    def _transition(self, review: Path, how: str) -> RunState:
        with self._lock:
            if self.busy(review):
                raise Conflict("A session is already running for this review")
            store = Store(review)
            fresh = not (store.dir / "state.json").exists()
            st = store.load()
            if st.phase == "waiting_user":
                raise Conflict("Claude is waiting for your answer. Answer the question, or press Stop first.")
            if how == "run":
                if st.phase not in ("idle", "done"):
                    raise Conflict({"running": "This review is already queued to run",
                                    "paused": "This review is paused; use Resume to start it now",
                                    "failed": "This step failed; use Retry"}.get(st.phase, "This review can't run now"))
                if fresh or st.phase == "done":
                    status = self.engine.litrev(review).status()
                    if status.get("error") and not status.get("rows"):
                        raise Conflict(f"Couldn't read where the review stands: {status['error'][-200:]}")
                    step = first_open_step(status)
                    if step is None:
                        raise Conflict("Every step of this review is done")
                    st = replace(st, step=step)
                st = replace(st, phase="running", paused_until=None)
            elif how == "resume":
                if st.phase != "paused":
                    raise Conflict("Only a paused review can be resumed")
                st = replace(st, phase="running", paused_until=None)
            elif how == "retry":
                if st.phase != "failed":
                    raise Conflict("Only a failed step can be retried")
                st = replace(st, phase="running", retries=0, no_progress=0, last_digest="",
                             message=f"retry after: {st.message}"[:300])
            else:
                raise ValueError(how)
            ask = store.ask()
            if ask and ask.get("status") == "open":          # its session is gone: keep it answerable, not "open"
                store.put_ask({**ask, "status": "parked"})
            store.save(st)
        self._publish_state(review, st)
        self.wake()
        return st

    def stop(self, review: Path) -> RunState:
        with self._lock:
            o = self.orphans.get(str(review))
            st = Store(review).load()
            if o:
                o.stop()
            elif self.busy(review) or st.phase in ("running", "paused", "waiting_user"):
                self.engine.stop(review)
            else:
                raise Conflict("Nothing is running for this review")
            st = Store(review).load()
        self._publish_state(review, st)
        self.wake()
        return st
