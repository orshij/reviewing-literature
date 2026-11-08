import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from litrev_app.orchestrator import AppConfig, Engine, decide_next
from litrev_app.outcome import Outcome
from litrev_app.runner import Limits
from litrev_app.store import RunState, Store

SKILL = Path(os.environ.get("LITREV_SKILL", Path.home() / ".claude/skills/reviewing-literature"))
FAKE = Path(__file__).parent / "fake_claude.py"
ROWS0_DONE = {"rows": [{"step": 0, "key": "scope", "state": "done"}, {"step": 0, "key": "taxonomy", "state": "done"},
                       {"step": 1, "key": "search", "state": "todo"}]}
ROWS0_TODO = {"rows": [{"step": 0, "key": "scope", "state": "done"}, {"step": 0, "key": "taxonomy", "state": "todo"}]}
ST0 = RunState(step=0, phase="running")


def test_done_report_and_clean_rows_advance():
    n = decide_next(ST0, Outcome("ok"), {"outcome": "done"}, ROWS0_DONE, None, None, 100.0)
    assert (n.step, n.phase, n.no_progress) == (1, "running", 0)
    last = decide_next(RunState(step=7, phase="running"), Outcome("ok"), {"outcome": "done"},
                       {"rows": [{"step": 7, "key": "publish", "state": "todo"}]}, None, None, 100.0)
    assert last.phase == "done"                                      # publish is the app's job, not the step's


def test_done_report_with_open_rows_continues_and_stalls_after_two():
    n = decide_next(ST0, Outcome("ok"), {"outcome": "done", "summary": "s"}, ROWS0_TODO, None, None, 100.0)
    assert (n.step, n.phase) == (0, "running")
    n2 = decide_next(n, Outcome("ok"), {"outcome": "incomplete"}, ROWS0_TODO, None, None, 100.0)
    n3 = decide_next(n2, Outcome("ok"), {"outcome": "incomplete"}, ROWS0_TODO, None, None, 100.0)
    assert n3.phase == "failed" and "No progress" in n3.message


def test_pauses_waits_and_failures():
    assert decide_next(ST0, Outcome("usage_limit", resets_at=999.0), None, ROWS0_TODO, None, None, 100.0).paused_until == 999.0
    assert decide_next(ST0, Outcome("usage_limit"), None, ROWS0_TODO, None, None, 100.0).paused_until == 1900.0
    lim = decide_next(ST0, Outcome("ok", limit_seen=True, resets_at=500.0), {"outcome": "incomplete"}, ROWS0_TODO, None, None, 100.0)
    assert (lim.phase, lim.paused_until) == ("paused", 500.0)
    t = decide_next(ST0, Outcome("transient", result_text="Overloaded"), None, ROWS0_TODO, None, None, 100.0)
    assert t.phase == "paused" and t.paused_until == 160.0 and t.retries == 1
    assert decide_next(ST0, Outcome("auth"), None, ROWS0_TODO, None, None, 100.0).phase == "failed"
    assert decide_next(ST0, Outcome("stopped"), None, ROWS0_TODO, None, None, 100.0).phase == "idle"
    w = decide_next(ST0, Outcome("ok"), {"outcome": "parked"}, ROWS0_TODO, {"status": "parked", "question": "Q?"}, None, 100.0)
    assert (w.phase, w.message) == ("waiting_user", "Q?")
    b = decide_next(ST0, Outcome("ok"), {"outcome": "blocked"}, ROWS0_TODO, None, {"reason": "OpenAlex credits", "retry_after": None}, 100.0)
    assert b.phase == "paused" and b.paused_until == 1900.0 and "OpenAlex" in b.message


def _engine(tmp_path, scenario, wait_s=30):
    sc = tmp_path / "scenario.json"; sc.write_text(json.dumps(scenario))
    os.environ["FAKE_CLAUDE_SCENARIO"] = str(sc)
    cfg = AppConfig(root=tmp_path, skill=SKILL, claude=FAKE, python=Path(sys.executable), appdir=tmp_path / "appdir",
                    wait_s=wait_s, limits=Limits(init_s=10, idle_s=30, hard_s=120, poll_s=0.05, grace_int=2, grace_term=2),
                    caffeinate=False)
    eng = Engine(cfg)
    r = eng.create_review("t", seed="Depth Anything", purpose="test", cap=12, years=3)
    return eng, r


STEP0 = {"events": [{"$ask": {"kind": "scope", "question": "Right scope?", "context": "c",
                              "proposal": {"in": ["monocular depth"], "out": ["stereo"], "protocol": []}}},
                    {"$write": {"path": "{review}/data/seed_taxonomy/P001.json", "content": "{}"}},
                    {"$report": {"step": 0, "summary": "scope and taxonomy"}}]}


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_step0_with_an_answer_in_session_advances_to_step1(tmp_path):
    eng, r = _engine(tmp_path, STEP0)
    store = Store(r)
    def answer_when_asked():
        while (store.ask() or {}).get("status") != "open":
            time.sleep(0.05)
        res = eng.answer(r, {"in": ["monocular depth"], "out": ["stereo"], "protocol": [], "notes": "fine"})
        assert res.ok, res.error
    threading.Thread(target=answer_when_asked).start()
    st = eng.run_one(r)
    assert (st.step, st.phase) == (1, "running"), st.message
    assert json.loads((r / "meta.json").read_text())["scope"]["in"] == ["monocular depth"]
    assert store.ask()["status"] == "delivered"
    rec = store.sessions()[-1]
    assert rec["outcome"] == "ok" and rec["step"] == 0 and rec["report"]["outcome"] == "done"


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_parked_question_answered_later_goes_into_the_next_brief(tmp_path):
    eng, r = _engine(tmp_path, STEP0, wait_s=0.5)
    st = eng.run_one(r)
    assert st.phase == "waiting_user" and st.message == "Right scope?"
    res = eng.answer(r, {"in": ["monocular depth"], "out": [], "protocol": [], "notes": ""})
    assert res.ok
    st = Store(r).load()
    assert st.phase == "running" and st.answers and st.answers[0].startswith("scope:")
    brief = eng.brief(r, Store(r).load(), "S-next")
    assert "Answered since the last session" in brief and "monocular depth" in brief


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_run_one_never_leaves_running_on_an_exception(tmp_path):
    eng, r = _engine(tmp_path, STEP0)
    def boom(spec, **kw):
        raise OSError("cannot start claude")
    eng.session_factory = boom
    with pytest.raises(OSError):
        eng.run_one(r)
    st = Store(r).load()
    assert st.phase == "failed" and "cannot start claude" in st.message and st.sid is None


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_step_sessions_carry_the_review_and_the_apps_own_litrev_calls_do_not(tmp_path, monkeypatch):
    # security F1: litrev.py confines a session to $LITREV_APP_REVIEW; the app runs init itself, so its own calls
    # must not carry it, not even one inherited from the app's environment
    monkeypatch.setenv("LITREV_APP_REVIEW", "/elsewhere")
    eng, r = _engine(tmp_path, STEP0)
    seen = []
    def capture(spec, **kw):
        seen.append(spec)
        raise OSError("stop here")
    eng.session_factory = capture
    with pytest.raises(OSError):
        eng.run_one(r)
    assert seen[0].env["LITREV_APP_REVIEW"] == os.path.realpath(r)
    assert "LITREV_APP_REVIEW" not in eng.env() and "LITREV_APP_REVIEW" not in eng.litrev(r).env


def test_blocked_report_without_blocked_file_pauses_instead_of_failing():
    # M0 review: a session that could not get the review's lock reported `blocked` without blocked.json; the engine
    # fell through to the no-progress check and failed the run after two sessions
    n = decide_next(ST0, Outcome("ok"), {"outcome": "blocked", "summary": "review locked by enrich pid 4242"},
                    ROWS0_TODO, None, None, 100.0)
    assert n.phase == "paused" and n.paused_until == 700.0 and "locked" in n.message


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_only_steps_3_to_5_get_webfetch(tmp_path):
    eng, r = _engine(tmp_path, STEP0)
    seen = []
    def capture(spec, **kw):
        seen.append(spec)
        raise OSError("stop here")
    eng.session_factory = capture
    allowed = {}
    for step in (1, 3, 6):
        Store(r).save(replace(Store(r).load(), step=step, phase="running", sid=None))
        with pytest.raises(OSError):
            eng.run_one(r)
        argv = seen[-1].argv
        s = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        allowed[step] = "WebFetch" in s["permissions"]["allow"]
    assert allowed == {1: False, 3: True, 6: False}
