import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from litrev_app.bus import EventBus
from litrev_app.events import Activity
from litrev_app.scheduler import Conflict, Scheduler, first_open_step
from litrev_app.store import RunState, Store

SID1 = "0f9c2b9e-6a1d-4c59-9a43-3e8f1f6d2a10"
STEP0_DONE = {"rows": [{"step": 0, "key": "scope", "state": "done"}, {"step": 1, "key": "search", "state": "todo"}]}


class FakeEngine:
    """Stands in for M1a's Engine: run_one emits an activity (and optionally a question) and sets the next phase."""

    def __init__(self, root, outcomes=None, status=None, block=None):
        self.cfg = SimpleNamespace(root=root)
        self.live, self.calls, self.seen = {}, [], {}
        self.active = self.max_active = 0
        self.outcomes = outcomes or {}          # slug -> (phase, message, asks)
        self.status = status or STEP0_DONE
        self.block = block or {}                # slug -> Event that run_one waits on
        self._lock = threading.Lock()

    def run_one(self, review, on_activity=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(review.name)
            st = self.seen[review.name] = Store(review).load()
            on_activity(Activity("litrev", "Searching", "term x", "t1", False, "running"))
            phase, msg, asks = self.outcomes.get(review.name, ("done", "Review ready", False))
            if asks:
                on_activity(Activity("ask", "Question for you", "Right scope?", "a1", False, "running"))
            if review.name in self.block:
                self.block[review.name].wait(5)
            nxt = replace(st, phase=phase, message=msg, sid=None, pid=None)
            Store(review).save(nxt)
            return nxt
        finally:
            with self._lock:
                self.active -= 1

    def stop(self, review):
        Store(review).save(replace(Store(review).load(), phase="idle", message="Stopped"))

    def litrev(self, review):
        return SimpleNamespace(status=lambda: self.status)


def make(root, slug, state, age=0.0):
    r = root / "reviews" / slug
    r.mkdir(parents=True)
    (r / "meta.json").write_text(json.dumps({"slug": slug, "seed": f"Seed {slug}"}))
    if state is not None:
        Store(r).save(state)
        t = time.time() - age
        os.utime(Store(r).dir / "state.json", (t, t))
    return r


def sched(engine, **kw):
    notes = []
    return Scheduler(engine, EventBus(), notify=lambda t, b: notes.append((t, b)), **kw), notes


def drain(sub):
    return list(iter(lambda: sub.get(0.05), None))


def test_tick_runs_the_longest_waiting_due_review(tmp_path):
    make(tmp_path, "a", RunState(phase="running"), age=10)
    make(tmp_path, "b", RunState(phase="running"), age=20)
    make(tmp_path, "idle", RunState(phase="idle"))
    make(tmp_path, "cli-made", None)                          # made in Claude Code, never run here
    make(tmp_path, "Bad_Slug", RunState(phase="running"))     # not addressable by the app: skipped
    eng = FakeEngine(tmp_path)
    s, _ = sched(eng)
    sub = s.bus.subscribe("b")
    assert (s.tick(), s.tick(), s.tick()) == ("b", "a", None)
    assert eng.calls == ["b", "a"]
    assert [e["type"] for e in drain(sub)] == ["state", "activity", "state", "notify"]   # done -> "review is ready"
    assert not (tmp_path / "reviews" / "cli-made" / "cache").exists()


def test_one_session_at_a_time_across_reviews(tmp_path):
    a = make(tmp_path, "a", RunState(phase="running"), age=20)
    make(tmp_path, "b", RunState(phase="running"), age=10)
    gate = threading.Event()
    eng = FakeEngine(tmp_path, block={"a": gate})
    s, _ = sched(eng, tick_s=0.05)
    s.launch()
    try:
        time.sleep(0.4)
        assert eng.calls == ["a"] and eng.max_active == 1 and s.busy(a)
        with pytest.raises(Conflict):
            s.run(a)
        gate.set()
        deadline = time.time() + 5
        while eng.calls != ["a", "b"] and time.time() < deadline:
            time.sleep(0.05)
        assert eng.calls == ["a", "b"] and eng.max_active == 1
    finally:
        s.shutdown()


def test_paused_review_resumes_after_paused_until(tmp_path):
    r = make(tmp_path, "p", RunState(step=2, phase="paused", paused_until=100.0, message="Paused: Claude usage limit"))
    now = [50.0]
    eng = FakeEngine(tmp_path)
    s, _ = sched(eng, clock=lambda: now[0])
    assert s.tick() is None and Store(r).load().phase == "paused" and eng.calls == []
    now[0] = 150.0
    assert s.tick() == "p"
    seen = eng.seen["p"]
    assert (seen.step, seen.phase, seen.paused_until) == (2, "running", None)


def test_run_resume_retry_stop_follow_the_phase(tmp_path):
    eng = FakeEngine(tmp_path, status={"rows": [{"step": 0, "key": "scope", "state": "done"},
                                                {"step": 3, "key": "datasets", "state": "stale"},
                                                {"step": 7, "key": "publish", "state": "todo"}]})
    s, _ = sched(eng)
    cli = make(tmp_path, "cli", None)
    st = s.run(cli)
    assert (st.step, st.phase) == (3, "running") and Store(cli).load().phase == "running"
    with pytest.raises(Conflict, match="queued"):
        s.run(cli)
    assert s.stop(cli).phase == "idle"
    paused = make(tmp_path, "paused", RunState(step=4, phase="paused", paused_until=9e9))
    assert s.resume(paused).phase == "running" and Store(paused).load().paused_until is None
    failed = make(tmp_path, "failed", RunState(step=5, phase="failed", no_progress=2, last_digest="x",
                                               message="No progress in two sessions"))
    st = s.retry(failed)
    assert (st.phase, st.no_progress, st.last_digest) == ("running", 0, "") and "No progress" in st.message
    with pytest.raises(Conflict):
        s.retry(paused)
    done = make(tmp_path, "done", RunState(step=7, phase="done"))
    eng.status = {"rows": [{"step": 7, "key": "publish", "state": "todo"}]}
    assert first_open_step(eng.status) is None
    with pytest.raises(Conflict, match="done"):
        s.run(done)
    with pytest.raises(Conflict):
        s.stop(done)
    waiting = make(tmp_path, "waiting", RunState(step=1, phase="waiting_user"))
    with pytest.raises(Conflict, match="answer"):
        s.run(waiting)
    stopped = make(tmp_path, "stopped", RunState(step=1, phase="idle", message="Stopped"))
    Store(stopped).put_ask({"id": "q", "status": "open", "kind": "scope", "question": "Q?"})
    s.run(stopped)
    assert Store(stopped).ask()["status"] == "parked"


def test_notifications_for_questions_failures_and_done(tmp_path):
    make(tmp_path, "q", RunState(phase="running"), age=30)
    make(tmp_path, "f", RunState(phase="running"), age=20)
    make(tmp_path, "d", RunState(phase="running"), age=10)
    eng = FakeEngine(tmp_path, outcomes={"q": ("waiting_user", "Right scope?", True),
                                         "f": ("failed", "Session failed (error): boom", False),
                                         "d": ("done", "Review ready", False)})
    s, notes = sched(eng)
    glob = s.bus.subscribe("*")
    while s.tick():
        pass
    assert len(notes) == 3            # the question once (not again when its session parks), failed, done
    assert notes[0][0] == "Litrev: Seed q" and "question" in notes[0][1]
    assert "boom" in notes[1][1] and "ready" in notes[2][1]
    types = [e["type"] for e in drain(glob)]
    assert types.count("notify") == 3 and "state" in types and "activity" not in types


def test_dead_session_from_an_earlier_launch_is_finished_and_queued(tmp_path):
    r = make(tmp_path, "r", RunState(step=0, phase="running", sid="S1", pid=999999))
    store = Store(r)
    store.events_path("S1").parent.mkdir(parents=True, exist_ok=True)
    store.events_path("S1").write_text(
        json.dumps({"type": "system", "subtype": "init", "model": "m"}) + "\n" +
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
                    "total_cost_usd": 0.25, "result": "done"}) + "\n")
    (store.dir / "report-S1.json").write_text(json.dumps({"step": 0, "outcome": "done", "summary": "scope"}))
    eng = FakeEngine(tmp_path)
    s, _ = sched(eng, alive=lambda pid: False)
    assert s.recover() == ["r"]
    st = store.load()
    assert (st.step, st.phase, st.sid, st.cost_usd) == (1, "running", None, 0.25)
    assert store.sessions()[-1]["outcome"] == "ok" and "restarted" in store.sessions()[-1]["note"]
    r2 = make(tmp_path, "r2", RunState(step=2, phase="running", sid="S2", pid=999998))
    assert s.recover() == ["r2"]
    st2 = Store(r2).load()
    assert (st2.phase, st2.sid) == ("running", None) and "interrupted" in st2.message
    assert s.tick() == "r" and s.tick() == "r2"           # the one automatic resume each


ORPHAN = r'''
import json, pathlib, sys, time
ev, flag, report = map(pathlib.Path, sys.argv[1:4])
ev.parent.mkdir(parents=True, exist_ok=True)
with open(ev, "a") as f:
    f.write(json.dumps({"type": "system", "subtype": "init", "model": "fake"}) + "\n")
    f.write(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_use",
        "id": "b1", "name": "Bash", "input": {"command": "python3 /s/scripts/litrev.py search --term x --review /r"}}]}}) + "\n")
    f.flush()
    try:
        while not flag.exists():
            time.sleep(0.05)
    except KeyboardInterrupt:
        sys.exit(130)
    report.write_text(json.dumps({"step": 0, "outcome": "done", "summary": "s"}))
    f.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
                        "total_cost_usd": 0.5, "result": "ok"}) + "\n")
'''


def orphan(tmp_path, r, sid):
    store = Store(r)
    p = subprocess.Popen([sys.executable, "-c", ORPHAN, str(store.events_path(sid)), str(tmp_path / f"go-{sid}"),
                          str(store.dir / f"report-{sid}.json"), "--session-id", sid], start_new_session=True)
    store.save(replace(store.load(), pid=p.pid))
    deadline = time.time() + 5
    while (not store.events_path(sid).exists() or store.events_path(sid).read_text().count("\n") < 2) \
            and time.time() < deadline:
        time.sleep(0.05)
    return p


def test_alive_orphan_is_adopted_then_finished(tmp_path):
    r = make(tmp_path, "r", RunState(step=0, phase="running", sid=SID1))
    make(tmp_path, "other", RunState(phase="running"), age=100)
    p = orphan(tmp_path, r, SID1)
    try:
        eng = FakeEngine(tmp_path)
        s, _ = sched(eng)
        assert s.recover() == ["r"] and s.busy(r) and p.pid in s.own_pids()
        assert s.tick() is None and s.tick() is None
        assert eng.calls == []                            # nothing else starts while the adopted session lives
        assert any(e.get("title") == "Searching" for e in s.bus.recent("r"))
        (tmp_path / f"go-{SID1}").write_text("go")
        p.wait(5)
        assert s.tick() == "other"                        # the adopted one is finished first, then the queue moves
        st = Store(r).load()
        assert (st.step, st.phase) == (1, "running") and Store(r).sessions()[-1]["note"].startswith("adopted")
        assert s.tick() == "r" and eng.calls == ["other", "r"]
    finally:
        if p.poll() is None:
            p.kill()


def test_stop_an_adopted_session(tmp_path):
    r = make(tmp_path, "r", RunState(step=1, phase="running", sid=SID1))
    p = orphan(tmp_path, r, SID1)
    try:
        s, _ = sched(FakeEngine(tmp_path))
        s.recover()
        s.stop(r)
        assert p.wait(5) == 130
        assert s.tick() is None
        st = Store(r).load()
        assert (st.phase, st.message, st.sid) == ("idle", "Stopped", None) and not s.busy(r)
    finally:
        if p.poll() is None:
            p.kill()


def test_adoption_needs_the_exact_session_id_argument(tmp_path):
    # M1b review: state.json is editable by a step session; a sid that is only a substring of some process's command
    # line (or not a UUID) must not make the app adopt and later signal that process group
    r = make(tmp_path, "r", RunState(step=0, phase="running", sid="python"))
    p = orphan(tmp_path, r, SID1)
    try:
        Store(r).save(replace(Store(r).load(), sid="python", pid=p.pid))   # "python" is in its command line
        s, _ = sched(FakeEngine(tmp_path))
        s.recover()
        assert not s.busy(r)                              # finished from its (empty) events file, not adopted
        assert p.poll() is None                           # and never signalled
    finally:
        p.kill()


def test_adopted_session_gets_the_idle_watchdog(tmp_path):
    # M1b review: an adopted session had no idle/hard limit, so a hang blocked every review until Stop
    from litrev_app.runner import Limits
    r = make(tmp_path, "r", RunState(step=1, phase="running", sid=SID1))
    p = orphan(tmp_path, r, SID1)
    try:
        s, _ = sched(FakeEngine(tmp_path), limits=Limits(init_s=5, idle_s=0.5, hard_s=60, poll_s=0.05, grace_int=2, grace_term=2))
        s.recover()
        deadline = time.time() + 5
        while p.poll() is None and time.time() < deadline:
            s.tick(); time.sleep(0.1)
        assert p.wait(5) == 130
        s.tick()
        assert Store(r).sessions()[-1]["outcome"] == "watchdog" and not s.busy(r)
    finally:
        if p.poll() is None:
            p.kill()


def test_launch_adopts_before_serving(tmp_path):
    # M1b review: recover() ran in the scheduler thread while HTTP already served; a Stop in that window was lost
    r = make(tmp_path, "r", RunState(step=1, phase="running", sid=SID1))
    p = orphan(tmp_path, r, SID1)
    try:
        s, _ = sched(FakeEngine(tmp_path))
        s.launch()
        assert s.busy(r)                                  # adopted synchronously, before launch() returns
        s.shutdown()
    finally:
        p.kill()


def test_replayed_questions_do_not_notify_again(tmp_path):
    # M1b review: adopting a session replays its events; an already-answered question notified on every restart
    r = make(tmp_path, "r", RunState(step=0, phase="running", sid=SID1))
    s, notes = sched(FakeEngine(tmp_path))
    Store(r).put_ask({"id": "q1", "kind": "scope", "status": "delivered", "question": "Right scope?"})
    s._on_activity(r)(Activity("ask", "Question for you", "Right scope?", "a1", False, "running"))
    assert notes == []
    s._asked.clear()
    Store(r).put_ask({"id": "q2", "kind": "scope", "status": "open", "question": "Right scope?"})
    s._on_activity(r)(Activity("ask", "Question for you", "Right scope?", "a2", False, "running"))
    assert len(notes) == 1


EVIL = "../" * 6 + "outside/x"      # from <review>/cache/app/report-.. : five levels up is tmp_path


def _traversal_review(tmp_path):
    """A review whose state.json names a session id with path parts, as a prompt-injected step session could write
    it (security F1/F3), with the decoy folders the traversal needs and files outside the review to reach."""
    r = make(tmp_path, "r", RunState(step=0, phase="running", sid=EVIL, pid=999999))
    store = Store(r)
    for d in ("report-..", "blocked-..", "events/.."):
        (store.dir / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.json").write_text(json.dumps({"step": 0, "outcome": "done", "summary": "s3cret"}))
    (store.dir / "blocked.json").write_text(json.dumps({"reason": "chosen by the session"}))
    return r, store


def test_store_never_builds_a_path_from_a_session_id_with_path_parts(tmp_path):
    r, store = _traversal_review(tmp_path)
    assert (store.dir / f"report-{EVIL}.json").resolve() == (tmp_path / "outside" / "x.json").resolve()
    assert store.report(EVIL) is None
    assert store.take_blocked(EVIL) is None and (store.dir / "blocked.json").exists()
    assert not (tmp_path / "outside" / "x.json").with_name("x.json").read_text().startswith("{\"reason\"")
    for bad in (EVIL, "", None, "a/b", ".hidden", "-x"):
        with pytest.raises(ValueError):
            store.events_path(bad)
    assert store.events_path(SID1).name == f"{SID1}.jsonl" and store.events_path("chat-0f9c2b9e-001").parent.name == "events"


def test_recovery_of_a_session_id_with_path_parts_reads_nothing_outside(tmp_path):
    r, store = _traversal_review(tmp_path)
    s, _ = sched(FakeEngine(tmp_path), alive=lambda pid: False)
    assert s.recover() == ["r"]
    st = store.load()
    assert (st.phase, st.sid) == ("running", None) and "interrupted" in st.message
    log = (store.dir / "sessions.jsonl").read_text()
    assert "s3cret" not in log and json.loads(log.splitlines()[-1])["sid"] is None
    assert (store.dir / "blocked.json").exists() and not (tmp_path / "outside" / "x.json").read_text().count("chosen")


def test_a_waiting_publish_holds_the_reviews_steps(tmp_path):
    r = make(tmp_path, "r", RunState(step=1, phase="running"))
    pj = Store(r).dir / "publish.json"
    pj.write_text(json.dumps({"state": "waiting_approval"}))
    eng = FakeEngine(tmp_path)
    s, _ = sched(eng)
    assert s.tick() is None and eng.calls == []
    pj.write_text(json.dumps({"state": "published"}))
    assert s.tick() == "r"
