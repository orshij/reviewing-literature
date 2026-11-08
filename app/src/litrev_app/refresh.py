"""Refresh (M2): check a finished review for new papers and add the ones the person approves.

`refresh --dry-run` (request count, shown first) -> the person confirms -> `refresh` (network, minutes, under the
app's writer lock) -> a short step-profile session that screens the new candidates and asks once with kind
"refresh" -> the choice is recorded through litrev (screen included / excluded, decide --kind screen --by user, a cap
raise when the approvals go over the cap) -> steps 4-7 queued for the approved papers, then Publish."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import briefs
from .ask_mcp import read_json, write_json_atomic
from .claude_cmd import step_argv, write_session_files
from .outcome import classify
from .runner import Limits, Session, SessionSpec, run_session
from .store import Store
from .writers import wake_scheduler

REFRESH_LIMITS = Limits(init_s=120, idle_s=1200, hard_s=3 * 3600)
REFRESH_MAX_TURNS = 150
BUSY_PHASES = ("running", "waiting_user", "paused")
SHOWN_ACTIVITY = {"litrev", "litrev_done", "agent_start", "agent_end", "denied", "limit", "ask", "ask_done"}
_SVC_LOCK = threading.Lock()


class RefreshError(Exception):
    def __init__(self, code: int, message: str, outputs: list | None = None):
        super().__init__(message)
        self.code, self.outputs = code, outputs or []


def _s(v) -> str:
    if isinstance(v, dict):
        v = v.get("value")
    return v if isinstance(v, str) else ""


def parse_refresh_report(text: str) -> dict[str, list[str]]:
    """{paper id: found-by paths} from rendered/REFRESH.md (litrev write_refresh_report), in the report's order."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in (text or "").splitlines():
        m = re.match(r"^### (P\d+) - ", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur and line.startswith("- Found by: "):
            v = line[len("- Found by: "):].strip()
            out[cur] = [] if v == "no recorded path" else [x.strip() for x in v.split(", ") if x.strip()]
    return out


def _papers(review: Path) -> dict:
    d = read_json(review / "data" / "papers.json")
    return d if isinstance(d, dict) else {}


def cap_usage(review: Path) -> dict:
    """Included papers counted against the cap (validate's rule: not seminal, taxonomy source or seed) and the cap."""
    used = sum(1 for p in _papers(review).values() if p.get("status") == "included" and not p.get("seminal")
               and not p.get("taxonomy_source") and not p.get("seed"))
    meta = read_json(review / "meta.json") or {}
    return {"used": used, "max": int((meta.get("caps") or {}).get("max_papers") or 0)}


def new_candidates(review: Path, date: str | None) -> list[str]:
    """Unscreened candidates the refresh of `date` added, in the report's order (most paths first)."""
    if not date:
        return []
    ids = {pid for pid, p in _papers(review).items()
           if p.get("discovered_by_refresh") == date and p.get("status") == "candidate"}
    rep = review / "rendered" / "REFRESH.md"
    order = list(parse_refresh_report(rep.read_text(errors="replace"))) if rep.exists() else []
    return [pid for pid in order if pid in ids] + sorted(ids - set(order))


def _failure(o, report: dict | None = None, blocked: dict | None = None) -> str:
    if blocked or (report or {}).get("outcome") == "blocked":       # app-mode.md: e.g. the review was locked
        why = (blocked or {}).get("reason") or (report or {}).get("summary") or "blocked"
        return f"Blocked: {why}. Use Retry screening in a few minutes."
    if o.kind == "usage_limit":
        when = datetime.fromtimestamp(o.resets_at).strftime("%a %H:%M") if o.resets_at else "later"
        return f"Claude usage limit reached; use Retry screening after {when}."
    if o.kind == "stopped":
        return "Stopped. Use Retry screening to let Claude read the new papers again."
    return f"Claude ended without a recommendation ({o.kind}). {o.result_text[-200:]}".strip()


class RefreshService:
    def __init__(self, engine, bus, *, limits: Limits = REFRESH_LIMITS, clock=time.time, wake=lambda: None):
        self.engine, self.bus, self.limits, self.clock, self.wake = engine, bus, limits, clock, wake
        self._lock = threading.RLock()          # orders "session ended" against "answer recorded"
        self.screening: set[str] = set()
        self.threads: dict[str, threading.Thread] = {}

    # -- state -----------------------------------------------------------------------------------------------
    def _file(self, review: Path) -> Path:
        return review / "cache" / "app" / "refresh.json"

    def load(self, review: Path) -> dict:
        d = read_json(self._file(review))
        return d if isinstance(d, dict) else {"stage": "idle"}

    def _save(self, review: Path, **changes) -> dict:
        with self._lock:
            d = {**self.load(review), **changes, "updated_at": self.clock()}
            write_json_atomic(self._file(review), d)
        self.bus.publish(review.name, {"type": "refresh", "state": d})
        return d

    def running_keys(self) -> list[str]:
        return [k for k, t in list(self.threads.items()) if t.is_alive()]

    def busy_reason(self, review: Path) -> str | None:
        key = str(review)
        t = self.threads.get(key)
        if t and t.is_alive():
            return "a refresh is already running for this review"
        if self.engine.writers.frozen:
            return "the app is updating"
        who = self.engine.writers.holder(key)
        if who == "chat":
            return "Claude is answering a chat message that may change this review"
        if who:
            return f"a {who} is writing this review"
        if key in self.engine.live:
            return "a session is running on this review"
        if Store(review).publish_waiting():
            return "a publish is waiting for your approval in Terminal; finish or cancel it first"
        ph = Store(review).load().phase
        if ph in BUSY_PHASES:
            return f"the review is {ph.replace('_', ' ')}; refresh a review that isn't mid-run"
        if (Store(review).ask() or {}).get("status") in ("open", "parked"):
            return "a question is waiting for your answer"
        return None

    def candidates(self, review: Path, ask: dict | None, date: str | None) -> list[dict]:
        P = _papers(review)
        rep = review / "rendered" / "REFRESH.md"
        paths = parse_refresh_report(rep.read_text(errors="replace")) if rep.exists() else {}
        new = set(new_candidates(review, date)) | set(self.load(review).get("new") or [])
        prop = {}
        if ask and ask.get("kind") == "refresh":       # only this refresh's new papers (M2 review: an included paper
            prop = {x["id"]: x for x in (ask.get("proposal") or {}).get("papers") or []      # left unticked was excluded)
                    if isinstance(x, dict) and x.get("id") in P and x.get("id") in new}
        ids = list(prop) + [pid for pid in new_candidates(review, date) if pid not in prop]
        out = []
        for pid in ids:
            p, r = P.get(pid) or {}, prop.get(pid) or {}
            out.append({"id": pid, "title": _s(p.get("title")) or str(r.get("title") or ""),
                        "year": p.get("year") or r.get("year"), "url": _s(p.get("url")),
                        "abstract": _s(p.get("abstract"))[:800],
                        "paths": paths.get(pid) or [str(x) for x in r.get("paths") or []],
                        "recommend": r.get("recommend"), "why": str(r.get("why") or ""), "status": p.get("status")})
        return out

    def state(self, review: Path) -> dict:
        d = self.load(review)
        store = Store(review)
        ask = store.ask() or {}
        q = ask if ask.get("kind") == "refresh" and ask.get("status") in ("open", "parked") else None
        st = store.load()
        stage = d.get("stage", "idle")
        return {**d, "stage": stage,
                "busy": None if stage in ("fetching", "screening") else self.busy_reason(review),
                "run": {"step": st.step, "phase": st.phase, "message": st.message},
                "cap": cap_usage(review),
                "question": {"id": q.get("id"), "question": q.get("question", ""), "context": q.get("context", ""),
                             "status": q.get("status")} if q else None,
                "candidates": self.candidates(review, q, d.get("date")) if q else [],
                "publish_ready": stage == "queued" and st.phase == "done"}

    # -- actions ---------------------------------------------------------------------------------------------
    def plan(self, review: Path) -> dict:
        why = self.busy_reason(review)
        if why:
            raise RefreshError(409, f"Not now: {why}.")
        p = self.engine.litrev(review).run(["refresh", "--dry-run"])
        try:
            plan = json.loads(p.stdout) if p.returncode == 0 else None
        except ValueError:
            plan = None
        if not isinstance(plan, dict):
            raise RefreshError(502, "`refresh --dry-run` failed: "
                                    f"{((p.stderr or '') + (p.stdout or '')).strip()[-300:]}")
        reps = plan.get("replays") or []
        summary = {"requests": int(plan.get("requests", len(reps))),
                   "keyword": sum(1 for x in reps if x.get("kind") == "refresh-keyword"),
                   "citing": sum(1 for x in reps if x.get("kind") == "refresh-citing"),
                   "skipped": len(plan.get("skipped") or []),
                   "replays": [{"label": x.get("label"), "source": x.get("source"), "since": x.get("since")}
                               for x in reps[:200]]}
        self._save(review, stage="planned", plan=summary, message="", planned_at=self.clock())
        return self.state(review)

    def _spawn(self, review: Path, target) -> None:
        t = threading.Thread(target=target, args=(review,), daemon=True)
        self.threads[str(review)] = t
        t.start()

    def start(self, review: Path) -> dict:
        d = self.load(review)
        if d.get("stage") != "planned":
            raise RefreshError(409, "Find new papers first: it shows how many searches the check will run.")
        why = self.busy_reason(review)
        if why:
            raise RefreshError(409, f"Not now: {why}.")
        if not self.engine.writers.acquire(str(review), "refresh"):
            raise RefreshError(409, "Not now: another job is writing this review.")
        try:
            self._save(review, stage="fetching", started_at=self.clock(), sid=None, new=[], result=None,
                       message=f"Checking {(d.get('plan') or {}).get('requests', '?')} searches for new papers")
            self._spawn(review, self._fetch_then_screen)
        except BaseException:
            self.engine.writers.release(str(review), "refresh")
            raise
        return self.state(review)

    def rescreen(self, review: Path) -> dict:
        d = self.load(review)
        new = [pid for pid in new_candidates(review, d.get("date")) if pid in set(d.get("new") or [])]
        if d.get("stage") != "failed" or not new:
            raise RefreshError(409, "There is nothing to screen again.")
        why = self.busy_reason(review)
        if why:
            raise RefreshError(409, f"Not now: {why}.")
        if not self.engine.writers.acquire(str(review), "refresh"):
            raise RefreshError(409, "Not now: another job is writing this review.")
        try:
            self._save(review, stage="screening", new=new, message=f"Claude is reading the {len(new)} new paper(s)")
            self._spawn(review, self._screen_only)
        except BaseException:
            self.engine.writers.release(str(review), "refresh")
            raise
        return self.state(review)

    def answer(self, review: Path, body: dict) -> dict:
        body = body or {}
        store = Store(review)
        ask = store.ask() or {}
        if ask.get("kind") != "refresh" or ask.get("status") not in ("open", "parked"):
            raise RefreshError(409, "There is no open refresh question.")
        d = self.load(review)
        date = d.get("date") or ((read_json(review / "meta.json") or {}).get("last_refresh") or {}).get("date")
        cands = [c["id"] for c in self.candidates(review, ask, date)]
        approve = list(dict.fromkeys(str(x) for x in body.get("approve") or []))
        unknown = [x for x in approve if x not in cands]
        if unknown:
            raise RefreshError(400, f"Not among the new papers: {', '.join(unknown)}")
        cap = cap_usage(review)
        after, raise_to = cap["used"] + len(approve), None
        if after > cap["max"]:
            if not body.get("raise_cap"):
                raise RefreshError(409, f"Adding {len(approve)} paper(s) takes the review to {after} papers, over its "
                                        f"cap of {cap['max']}. Tick 'raise the cap' or approve fewer.")
            raise_to = after
        full = {"approve": approve, "reject": [c for c in cands if c not in approve], "raise_cap_to": raise_to,
                "date": date, "note": str(body.get("note") or "").strip()}
        with self._lock:
            res = self.engine.answer(review, full)
            if not res.ok:
                raise RefreshError(422, res.error, res.outputs)
            if str(review) not in self.screening:
                self._finish(review, store.ask() or {})
            else:
                self._save(review, message="Recorded. Claude is finishing its session; steps 4-7 follow.")
        return self.state(review)

    # -- background work -------------------------------------------------------------------------------------
    def _fetch_then_screen(self, review: Path) -> None:
        try:
            lit = self.engine.litrev(review)
            p = lit.run(["refresh"])
            if p.returncode != 0:
                self._save(review, stage="failed",
                           message=f"`refresh` failed: {((p.stderr or '') + (p.stdout or '')).strip()[-400:]}")
                return
            last = lit.meta().get("last_refresh") or {}
            date = last.get("date")
            new = new_candidates(review, date)
            result = {"replays": last.get("replays"), "failed": last.get("failed"), "new": len(new),
                      "line": (p.stdout or "").strip()[-300:]}
            if not new:
                self._save(review, stage="done", date=date, result=result, new=[],
                           message="Nothing new since the last check.")
                return
            self._save(review, stage="screening", date=date, result=result, new=new,
                       message=f"Claude is reading the {len(new)} new paper(s)")
            self._screen(review, date, new)
        except Exception as e:
            self._save(review, stage="failed", message=f"App error: {e}")
        finally:
            self.engine.writers.release(str(review), "refresh")

    def _screen_only(self, review: Path) -> None:
        try:
            d = self.load(review)
            self._screen(review, d["date"], d["new"])
        except Exception as e:
            self._save(review, stage="failed", message=f"App error: {e}")
        finally:
            self.engine.writers.release(str(review), "refresh")

    def _activity(self, review: Path, a) -> None:
        if a.kind in SHOWN_ACTIVITY:
            self.bus.publish(review.name, {"type": "refresh", "activity": a.to_dict()})

    def _screen(self, review: Path, date: str, new: list[str]) -> None:
        cfg, key, store = self.engine.cfg, str(review), Store(review)
        sid = str(uuid.uuid4())
        litrev = cfg.skill / "scripts" / "litrev.py"
        files = write_session_files(cfg.appdir / "sessions" / sid, profile="step", litrev=litrev, skill=cfg.skill,
                                    review=review, python=cfg.python, sid=sid,
                                    wait_s=int(cfg.wait_s) if cfg.wait_s >= 1 else cfg.wait_s,
                                    step5_model=cfg.step5_model)
        brief = briefs.refresh_brief(skill=cfg.skill, review=review, root=cfg.root, sid=sid, parallel=cfg.parallel,
                                     date=date, ids=new)
        argv = step_argv(cfg.claude, brief, sid, files, skill=cfg.skill, review=review, litrev=litrev,
                         model=cfg.model, effort=cfg.effort, max_turns=REFRESH_MAX_TURNS)
        if cfg.claude.suffix == ".py":           # tests: the fake claude is a script
            argv = [str(cfg.python), *argv]
        spec = SessionSpec(sid, argv, self.engine.env(review), review, store.events_path(sid), store.stderr_path(sid))
        with self._lock:
            self.screening.add(key)
        self._save(review, sid=sid)
        t0 = self.clock()
        try:
            session = Session(spec, caffeinate=cfg.caffeinate, limits=self.limits)
            self.engine.live[key] = session
            state, code, reason = run_session(session, lambda a: self._activity(review, a), self.limits,
                                              question_open=lambda: (store.ask() or {}).get("status") == "open")
            outcome = classify(state, code, reason, datetime.now().astimezone())
        except BaseException:
            with self._lock:
                self.screening.discard(key)
            raise
        finally:
            self.engine.live.pop(key, None)
        report, blocked = store.report(sid), store.take_blocked(sid)
        store.log_session({"sid": sid, "step": "refresh", "start": t0, "end": self.clock(), "outcome": outcome.kind,
                           "exit": code, "stop": reason, "cost_usd": outcome.cost_usd, "turns": outcome.num_turns,
                           "denials": outcome.denials, "resets_at": outcome.resets_at, "report": report,
                           "result": outcome.result_text[-500:], "next_phase": None, "next_step": None})
        with self._lock:
            self.screening.discard(key)
            rs = store.load()
            store.save(replace(rs, cost_usd=rs.cost_usd + (outcome.cost_usd or 0.0)))
            ask = store.ask() or {}
            mine = ask.get("kind") == "refresh" and ask.get("sid") == sid
            if mine and ask.get("status") in ("recorded", "delivered"):
                self._finish(review, ask)
            elif mine and ask.get("status") in ("open", "parked"):
                if ask["status"] == "open":
                    store.put_ask({**ask, "status": "parked"})
                self._save(review, stage="waiting_user", message="Claude's recommendations are ready: choose below.")
            else:
                self._save(review, stage="failed", message=_failure(outcome, report, blocked))

    def _finish(self, review: Path, ask: dict) -> dict:
        """Once per answered refresh question (callers hold self._lock): queue steps 4-7 for the approved papers."""
        d = self.load(review)
        if d.get("finished_ask") == ask.get("id"):
            return d
        a = ask.get("answer") or {}
        approve, reject = list(a.get("approve") or []), list(a.get("reject") or [])
        if approve:
            why = f"the refresh of {d.get('date')} added {', '.join(approve)}"
            n = self.engine.rerun_start(review, 4)
            self.engine.queue_from(review, n, message=f"refresh {d.get('date')}: steps {n}-7 for {', '.join(approve)}",
                                   notes=briefs.rerun_note(n, why))
            self.wake()
            return self._save(review, stage="queued", approved=approve, rejected=reject, finished_ask=ask.get("id"),
                              message=f"Steps {n}-7 are queued for {len(approve)} new paper(s).")
        return self._save(review, stage="done", approved=[], rejected=reject, finished_ask=ask.get("id"),
                          message=f"No papers added; {len(reject)} left out.")


def on_start(app) -> None:
    """Server start hook (server.start_features): a refresh's stage outlives its thread, which died with the previous
    launch (M2 review: the tab showed "Claude is reading…" forever). A waiting refresh question brings the answer form
    back; otherwise the stage fails, with Retry screening or Find new papers."""
    svc = service(app)
    for f in sorted((Path(app.engine.cfg.root) / "reviews").glob("*/cache/app/refresh.json")):
        review = f.parent.parent.parent
        if svc.load(review).get("stage") not in ("fetching", "screening") or str(review) in svc.running_keys():
            continue
        ask = Store(review).ask() or {}
        if ask.get("kind") == "refresh" and ask.get("status") in ("open", "parked"):
            svc._save(review, stage="waiting_user", message="Claude's recommendations are ready: choose below.")
        else:
            svc._save(review, stage="failed", message="Litrev stopped while this refresh was running. Retry the "
                                                       "screening, or find new papers again.")


def service(app) -> RefreshService:
    with _SVC_LOCK:
        svc = getattr(app, "refresh_service", None)
        if svc is None:
            svc = RefreshService(app.engine, app.bus, wake=lambda: wake_scheduler(app))
            app.refresh_service = svc
        return svc


def _handler(fn):
    def handle(app, req, slug):
        r = app.engine.review_path(slug)
        if not (r / "meta.json").exists():
            return 404, {"error": f"No review named {slug}"}, {}
        try:
            return 200, fn(service(app), r, req.json or {}), {}
        except RefreshError as e:
            return e.code, {"error": str(e), "outputs": e.outputs}, {}
    return handle


def register(router) -> None:
    base = r"^/api/reviews/(?P<slug>[a-z0-9-]+)/refresh"
    router.add("GET", base + "$", _handler(lambda s, r, b: s.state(r)))
    router.add("POST", base + "/plan$", _handler(lambda s, r, b: s.plan(r)))
    router.add("POST", base + "/start$", _handler(lambda s, r, b: s.start(r)))
    router.add("POST", base + "/screen$", _handler(lambda s, r, b: s.rescreen(r)))
    router.add("POST", base + "/answer$", _handler(lambda s, r, b: s.answer(r, b)))
