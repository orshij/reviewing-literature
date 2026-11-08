"""What happens after each step session (decide_next), and the engine that runs one session for a review."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import checkpoints
from .briefs import step_brief
from .claude_cmd import WEB_STEPS, child_env, step_argv, write_session_files
from .litrev_cli import Litrev, init_review
from .outcome import Outcome, classify
from .runner import Limits, Session, SessionSpec, run_session
from .store import RunState, Store
from .writers import WriterLocks

LAST_STEP = 7          # step 8 (the narrated video) runs only for a review the person opted in: see last_step()
PAUSE_DEFAULT_S = 1800
BLOCKED_RETRY_S = 600


def last_step(status: dict) -> int:
    """8 when `status` has step-8 rows (litrev shows them only after `presentation` opted the review in), else 7."""
    return 8 if any(r.get("step") == 8 for r in status.get("rows") or []) else LAST_STEP


def status_digest(status: dict) -> str:
    rows = [(r.get("key"), r.get("state"), r.get("detail")) for r in status.get("rows") or []]
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()[:16]


def _iso_ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def decide_next(st: RunState, o: Outcome, report: dict | None, status: dict, ask: dict | None,
                blocked: dict | None, now: float) -> RunState:
    """The session's report is the completion signal; `status` is the cross-check (spec: Outcome, watchdog, resume)."""
    n = replace(st, sid=None, pid=None, cost_usd=st.cost_usd + (o.cost_usd or 0.0), paused_until=None)
    rep = report or {}
    if o.kind == "usage_limit":
        return replace(n, phase="paused", paused_until=o.resets_at or now + PAUSE_DEFAULT_S,
                       message="Paused: Claude usage limit; resumes automatically")
    if o.kind == "transient":
        r = st.retries + 1
        return replace(n, phase="paused", retries=r, paused_until=now + min(60 * 2 ** (r - 1), PAUSE_DEFAULT_S),
                       message=f"Paused: temporary Claude error ({o.result_text[:120]})")
    if o.kind == "auth":
        return replace(n, phase="failed", message="Claude Code is not logged in with your Claude account. "
                                                  "Open Settings > Doctor to log in, then Resume.")
    if o.kind == "stopped":
        return replace(n, phase="idle", message="Stopped")
    if o.kind in ("startup_failed", "error"):
        return replace(n, phase="failed", message=f"Session failed ({o.kind}): {o.result_text[:300]}")
    n.retries = 0
    if ask and ask.get("status") in ("open", "parked"):
        return replace(n, phase="waiting_user", message=ask.get("question", ""))
    if blocked:
        return replace(n, phase="paused", paused_until=_iso_ts(blocked.get("retry_after")) or now + PAUSE_DEFAULT_S,
                       message=f"Paused: {blocked.get('reason', 'blocked')}")
    if rep.get("outcome") == "blocked":      # reported blocked without blocked.json (e.g. the review's lock was held)
        return replace(n, phase="paused", paused_until=now + BLOCKED_RETRY_S,
                       message=f"Paused: {rep.get('summary') or 'blocked'}"[:300])
    if o.limit_seen and rep.get("outcome") != "done":
        return replace(n, phase="paused", paused_until=o.resets_at or now + PAUSE_DEFAULT_S,
                       message="Paused: Claude usage limit (a subagent hit it); resumes automatically")
    open_rows = [r for r in status.get("rows") or [] if r.get("step") == st.step and r.get("state") in ("todo", "stale")
                 and r.get("key") != "publish"]
    digest = status_digest(status)
    if rep.get("outcome") == "done" and not open_rows:
        if st.step >= last_step(status):
            return replace(n, phase="done", no_progress=0, last_digest=digest, notes="",
                           message="Video ready: open the Video tab; publish the hub with Publish" if st.step >= 8
                           else "Review ready: open the hub, then Publish")
        return replace(n, step=st.step + 1, phase="running", no_progress=0, last_digest=digest, message="")
    stalled = st.no_progress + 1 if digest == st.last_digest else 0
    why = f"{rep.get('outcome', 'no report')}: {rep.get('summary', '') or o.kind}"[:300]
    if stalled >= 2:
        return replace(n, phase="failed", no_progress=stalled, last_digest=digest,
                       message=f"No progress in two sessions ({why})")
    return replace(n, phase="running", no_progress=stalled, last_digest=digest, message=f"continue: {why}")


@dataclass
class AppConfig:
    root: Path
    skill: Path
    claude: Path
    python: Path
    appdir: Path
    parallel: int = 3
    step5_model: str | None = None
    model: str | None = None
    effort: str | None = None
    mailto: str | None = None
    keys: dict = field(default_factory=dict)
    wait_s: float = 1800
    limits: Limits = field(default_factory=Limits)
    caffeinate: bool = True


class Engine:
    def __init__(self, cfg: AppConfig, session_factory=Session, clock=time.time):
        self.cfg, self.session_factory, self.clock = cfg, session_factory, clock
        self.live: dict[str, Session] = {}
        self.writers = WriterLocks()      # M2: one app-side writer per review (step, refresh, chat_rw)
        self.writer_wait_s = 1200.0       # how long a step waits for a chat message or a refresh to finish

    def review_path(self, slug: str) -> Path:
        return self.cfg.root / "reviews" / slug

    def env(self, review: Path | None = None) -> dict:
        """The app's own litrev calls: env(). A Claude Code session for a review: env(review) (security F1)."""
        return child_env(os.environ, litrev_home=self.cfg.root, venv_bin=self.cfg.python.parent,
                         claude_dir=self.cfg.claude.parent, concurrency=self.cfg.parallel, mailto=self.cfg.mailto,
                         keys=self.cfg.keys, review=review)

    def litrev(self, review: Path) -> Litrev:
        return Litrev(self.cfg.python, self.cfg.skill, review, self.env())

    def create_review(self, slug: str, *, seed: str, purpose: str, cap: int, years: int) -> Path:
        r = self.review_path(slug)
        p = init_review(self.cfg.python, self.cfg.skill, r, slug=slug, purpose=purpose, seed=seed, cap=cap,
                        years=years, env=self.env())
        if p.returncode != 0:
            raise RuntimeError(f"init failed: {(p.stderr or p.stdout)[-300:]}")
        Store(r).save(RunState(phase="running"))
        return r

    def brief(self, review: Path, st: RunState, sid: str) -> str:
        store, meta = Store(review), self.litrev(review).meta()
        first = st.step == 0 and not store.sessions()
        return step_brief(skill=self.cfg.skill, review=review, root=self.cfg.root, step=st.step, sid=sid,
                          parallel=self.cfg.parallel,
                          new={"seed": meta.get("seed", ""), "purpose": meta.get("purpose", "")} if first else None,
                          cont=None if first or not st.message else st.message, answers=st.answers, notes=st.notes)

    def run_one(self, review: Path, on_activity=None) -> RunState:
        """One step session as the review's only app-side writer. While a chat message or a refresh writes the
        review (or an update runs), wait up to writer_wait_s; if it still can't start, return the state unchanged
        and start nothing (the scheduler tries again)."""
        key, store = str(review), Store(review)
        if not self.writers.acquire(key, "step"):
            st = store.load()
            waiting = f"Waiting for the {self.writers.holder(key) or 'update'} to finish"
            store.save(replace(st, message=waiting))
            ok = self.writers.acquire(key, "step", timeout=self.writer_wait_s)
            cur = store.load()
            if cur.message == waiting:
                cur = replace(cur, message=st.message)
                store.save(cur)
            if ok and ((cur.phase, cur.step) != (st.phase, st.step) or cur.sid):    # Stopped (or moved) meanwhile
                self.writers.release(key, "step")
                return cur
            if not ok:
                return cur
        try:
            return self._run_one_locked(review, on_activity)
        finally:
            self.writers.release(key, "step")

    def _run_one_locked(self, review: Path, on_activity=None) -> RunState:
        """Run one session for the review's current step, then apply decide_next. Never leaves phase 'running'
        with a dead session: any exception marks the review failed and re-raises."""
        store, lit = Store(review), self.litrev(review)
        st = store.load()
        sid = str(uuid.uuid4())
        litrev_py = self.cfg.skill / "scripts" / "litrev.py"
        try:
            files = write_session_files(self.cfg.appdir / "sessions" / sid, profile="step", litrev=litrev_py,
                                        skill=self.cfg.skill, review=review, python=self.cfg.python, sid=sid,
                                        wait_s=int(self.cfg.wait_s) if self.cfg.wait_s >= 1 else self.cfg.wait_s,
                                        step5_model=self.cfg.step5_model, web=st.step in WEB_STEPS)
            argv = step_argv(self.cfg.claude, self.brief(review, st, sid), sid, files, skill=self.cfg.skill,
                             review=review, litrev=litrev_py, model=self.cfg.model, effort=self.cfg.effort)
            if self.cfg.claude.suffix == ".py":           # tests: the fake claude is a script
                argv = [str(self.cfg.python), *argv]
            spec = SessionSpec(sid, argv, self.env(review), review, store.events_path(sid), store.stderr_path(sid))
            session = self.session_factory(spec, caffeinate=self.cfg.caffeinate, limits=self.cfg.limits)
            self.live[str(review)] = session
            store.save(replace(st, phase="running", sid=sid, pid=session.pid))
            t0 = self.clock()
            state, code, reason = run_session(
                session, on_activity, self.cfg.limits,
                question_open=lambda: (store.ask() or {}).get("status") == "open")
            outcome = classify(state, code, reason, datetime.now().astimezone())
            report, status = store.report(sid), lit.status()
            ask, blocked = store.ask(), store.take_blocked(sid)
            nxt = decide_next(st, outcome, report, status, ask, blocked, self.clock())
            if outcome.kind in ("ok", "max_turns", "watchdog"):
                nxt = replace(nxt, answers=[] if not (ask and ask.get("status") in ("open", "parked")) else nxt.answers)
            store.log_session({"sid": sid, "step": st.step, "start": t0, "end": self.clock(), "outcome": outcome.kind,
                               "exit": code, "stop": reason, "cost_usd": outcome.cost_usd, "turns": outcome.num_turns,
                               "denials": outcome.denials, "resets_at": outcome.resets_at, "report": report,
                               "result": outcome.result_text[-500:], "next_phase": nxt.phase, "next_step": nxt.step})
            store.save(nxt)
            return nxt
        except BaseException as e:
            cur = store.load()
            store.save(replace(cur, phase="failed", sid=None, pid=None, message=f"App error: {e}"))
            raise
        finally:
            self.live.pop(str(review), None)

    def answer(self, review: Path, answer: dict) -> checkpoints.RecordResult:
        """Record the person's answer to the open (or parked) question, then release the waiting session or queue
        the next one with the answer in its brief."""
        store = Store(review)
        ask = store.ask()
        if not ask or ask.get("status") not in ("open", "parked"):
            return checkpoints.RecordResult(False, error="no open question")
        lit = self.litrev(review)
        res = checkpoints.record(lit.run, ask["kind"], answer, lit.meta())
        if not res.ok:
            return res
        parked = ask["status"] == "parked"
        store.put_ask({**ask, "status": "recorded", "answer": answer, "reply": res.reply, "recorded_at": self.clock()})
        st = store.load()
        st = replace(st, answers=[*st.answers, f"{ask['kind']}: {res.summary}"])
        if ask["kind"] != "refresh" and (parked or st.phase == "waiting_user"):   # refresh.py queues steps 4-7
            st = replace(st, phase="running", message="")
        store.save(st)
        return res

    def rerun_start(self, review: Path, step: int) -> int:
        """Where "run steps `step`-7" starts: at the app run's own step when that run stopped or failed before `step`
        (M2 review: a review stopped at step 2 skipped steps 2-3). Step 0-1 records don't count: a review made in
        Claude Code has none, and select/snowball/coverage are not rerun after hand-screened papers."""
        st = Store(review).load()
        return st.step if st.phase != "done" and 2 <= st.step < step else step

    def queue_from(self, review: Path, step: int, *, message: str, notes: str = "") -> RunState:
        """Run steps `step`..7 (or ..8 when the review is opted in to the video) next (approved refresh papers, a
        change made in chat, Make video); the scheduler starts them.
        `notes` reaches every session of this run through the brief and is cleared when the run is done."""
        store = Store(review)
        nxt = replace(store.load(), step=step, phase="running", auto=True, paused_until=None, no_progress=0,
                      last_digest="", retries=0, sid=None, pid=None, message=message, notes=notes)
        store.save(nxt)
        return nxt

    def stop(self, review: Path) -> None:
        s = self.live.get(str(review))
        if s:
            s.stop("user")
        else:
            st = Store(review).load()
            Store(review).save(replace(st, phase="idle", message="Stopped"))
