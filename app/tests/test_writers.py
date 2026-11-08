import json
import sys
import threading
import time

import pytest

from m2help import FAKE, FAST, HAS_SKILL, make_app, mini_review, scenario
from litrev_app import briefs, runner
from litrev_app.orchestrator import decide_next
from litrev_app.outcome import Outcome
from litrev_app.runner import Session, SessionSpec, run_session
from litrev_app.store import RunState, Store
from litrev_app.writers import Busy, WriterLocks, review_busy, wake_scheduler

needs_skill = pytest.mark.skipif(not HAS_SKILL, reason="needs the skill")


def test_one_writer_per_review_and_waiting():
    w = WriterLocks()
    assert w.acquire("/r/a", "chat") and w.holder("/r/a") == "chat"
    assert not w.acquire("/r/a", "step") and w.acquire("/r/b", "step")
    threading.Timer(0.3, lambda: w.release("/r/a", "chat")).start()
    t0 = time.monotonic()
    assert w.acquire("/r/a", "step", timeout=5) and time.monotonic() - t0 >= 0.25
    w.release("/r/a", "chat")                      # not the holder: ignored
    assert w.holder("/r/a") == "step"


def test_freeze_waits_for_everyone_and_blocks_new_sessions():
    w = WriterLocks()
    w.start_reading()
    assert "read-only" in w.freeze()
    w.stop_reading()
    assert w.acquire("/r/a", "refresh")
    assert "refresh on a" in w.freeze()
    w.release("/r/a", "refresh")
    assert w.freeze() is None and w.frozen
    assert w.freeze() == "an update is already running"
    assert not w.acquire("/r/a", "step", timeout=0.1)
    with pytest.raises(Busy):
        w.start_reading()
    w.thaw()
    assert w.acquire("/r/a", "step")


def test_wake_scheduler_is_optional():
    class S:
        n = 0

        def wake(self):
            S.n += 1
    wake_scheduler(type("A", (), {"scheduler": S()})())
    wake_scheduler(type("A", (), {"scheduler": None})())
    assert S.n == 1


def test_run_session_feeds_on_event_and_the_app_hooks(tmp_path, monkeypatch):
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps({"events": [{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}]}))
    seen, hooked = [], []
    monkeypatch.setattr(runner, "EVENT_HOOKS", [hooked.append, lambda e: 1 / 0])   # a broken hook is ignored
    spec = SessionSpec("S1", [sys.executable, str(FAKE), "-p", "x", "--session-id", "S1"],
                       {"FAKE_CLAUDE_SCENARIO": str(sc), "PATH": "/usr/bin:/bin"}, tmp_path,
                       tmp_path / "ev" / "S1.jsonl", tmp_path / "ev" / "S1.stderr")
    st, code, _ = run_session(Session(spec, caffeinate=False), None, FAST, on_event=seen.append)
    assert code == 0 and [e["type"] for e in seen] == ["system", "rate_limit_event", "result"]
    assert hooked == seen and st.rate_limit == {"status": "allowed"}


def test_decide_next_clears_standing_notes_only_when_the_run_ends():
    done = decide_next(RunState(step=7, phase="running", notes="no select"), Outcome("ok"), {"outcome": "done"},
                       {"rows": [{"step": 7, "key": "render", "state": "done"}]}, None, None, 1.0)
    assert done.phase == "done" and done.notes == ""
    mid = decide_next(RunState(step=4, phase="running", notes="no select"), Outcome("ok"), {"outcome": "done"},
                      {"rows": []}, None, None, 1.0)
    assert (mid.step, mid.notes) == (5, "no select")


def test_rerun_note_forbids_the_step1_detours():
    n = briefs.rerun_note(4, "the refresh of 2026-10-10 added P003")
    assert n.startswith("Steps 4-7 run again after the refresh of 2026-10-10 added P003")
    assert all(w in n for w in ("select", "snowball", "coverage"))


@needs_skill
def test_queue_from_and_the_brief(tmp_path):
    app = make_app(tmp_path)
    r = mini_review(app)
    st = app.engine.queue_from(r, 4, message="refresh 2026-10-10: steps 4-7 for P003", notes="Do not run select.")
    assert (st.step, st.phase, st.notes, st.auto) == (4, "running", "Do not run select.", True)
    assert Store(r).load() == st
    b = app.engine.brief(r, st, "S1")
    assert "Standing instruction for this run: Do not run select." in b and "steps 4-7 for P003" in b
    assert review_busy(app.engine, r) == "the review is running its steps"


@needs_skill
def test_a_step_waits_for_the_chat_writer(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"events": [{"$report": {"step": 7, "summary": "s"}}]})
    app = make_app(tmp_path)
    r = mini_review(app)
    eng = app.engine
    assert eng.writers.acquire(str(r), "chat")
    t = threading.Thread(target=eng.run_one, args=(r,))
    t.start()
    time.sleep(0.5)
    assert Store(r).sessions() == [] and Store(r).load().message == "Waiting for the chat to finish"
    released = time.time()
    eng.writers.release(str(r), "chat")
    t.join(60)
    assert Store(r).sessions()[-1]["start"] >= released and eng.writers.holder(str(r)) is None


@needs_skill
def test_a_step_gives_up_without_starting(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"events": []})
    app = make_app(tmp_path)
    r = mini_review(app)
    eng = app.engine
    eng.writer_wait_s = 0.3
    assert eng.writers.acquire(str(r), "refresh")
    st = eng.run_one(r)
    assert Store(r).sessions() == [] and (st.step, st.phase, st.message) == (7, "done", "")


@needs_skill
def test_stop_while_a_step_waits_for_the_writer_starts_nothing(tmp_path, monkeypatch):
    """M2 review: Stop pressed while the step waited for a chat; the wait ended, the old message came back over
    "Stopped" and a paid session started anyway."""
    scenario(monkeypatch, tmp_path, {"events": [{"$report": {"step": 4, "outcome": "done", "summary": "s"}}]})
    app = make_app(tmp_path)
    r = mini_review(app)
    eng = app.engine
    Store(r).save(RunState(step=4, phase="running", message="continue: handoff"))
    assert eng.writers.acquire(str(r), "chat")
    t = threading.Thread(target=eng.run_one, args=(r,))
    t.start()
    time.sleep(0.5)
    eng.stop(r)                                            # no live session yet: idle, "Stopped"
    eng.writers.release(str(r), "chat")
    t.join(30)
    st = Store(r).load()
    assert Store(r).sessions() == [] and (st.phase, st.message) == ("idle", "Stopped")
    assert eng.writers.holder(str(r)) is None


@needs_skill
def test_steps_n_to_7_start_where_an_unfinished_run_stopped(tmp_path):
    """M2 review: a review stopped at step 2 got "RUN FROM STEP 4" from chat; steps 2-3 were skipped and it ended
    "Review ready"."""
    app = make_app(tmp_path)
    r = mini_review(app)
    assert app.engine.rerun_start(r, 4) == 4                       # done: steps 4-7
    Store(r).save(RunState(step=2, phase="idle", message="Stopped"))
    assert app.engine.rerun_start(r, 4) == 2
    Store(r).save(RunState(step=3, phase="failed", message="x"))
    assert app.engine.rerun_start(r, 4) == 3
    Store(r).save(RunState(step=0, phase="idle"))                  # made in Claude Code: the app has no record
    assert app.engine.rerun_start(r, 4) == 4
    Store(r).save(RunState(step=2, phase="idle", message="Stopped"))
    from m2help import FakeRouter
    from litrev_app import chat
    rt = FakeRouter()
    chat.register(rt)
    code, body, _ = rt.call(app, "POST", "/api/reviews/t/chat/run-steps", {"step": 4})
    assert code == 200 and body["state"]["step"] == 2 and "Steps 2-7" in body["state"]["notes"]


@needs_skill
def test_a_waiting_publish_makes_the_chat_read_only(tmp_path):
    """While the person approves a publish in Terminal, nothing may change the hub (open item of the 2026-10-16
    security scan): a chat message then runs read-only."""
    app = make_app(tmp_path)
    r = mini_review(app)
    p = Store(r).dir / "publish.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    assert review_busy(app.engine, r) is None
    p.write_text(json.dumps({"state": "waiting_approval"}))
    assert "publish" in review_busy(app.engine, r)
    p.write_text(json.dumps({"state": "published"}))
    assert review_busy(app.engine, r) is None
