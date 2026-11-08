import json
import sys
from pathlib import Path

from litrev_app.runner import Limits, Session, SessionSpec, run_session

FAKE = Path(__file__).parent / "fake_claude.py"


def spec(tmp_path, scenario, sid="S1"):
    sc = tmp_path / f"{sid}.scenario.json"; sc.write_text(json.dumps(scenario))
    env = {"FAKE_CLAUDE_SCENARIO": str(sc), "PATH": "/usr/bin:/bin"}
    return SessionSpec(sid, [sys.executable, str(FAKE), "-p", "x", "--session-id", sid], env, tmp_path,
                       tmp_path / "events" / f"{sid}.jsonl", tmp_path / "events" / f"{sid}.stderr")


FAST = Limits(init_s=5, idle_s=5, hard_s=30, poll_s=0.05, grace_int=2, grace_term=2)


def test_success_session_streams_activities(tmp_path):
    acts = []
    s = Session(spec(tmp_path, {"events": [{"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "python3 /s/scripts/litrev.py status --json --review /r"}}]}}]}),
        caffeinate=False)
    st, code, reason = run_session(s, acts.append, FAST)
    assert code == 0 and reason is None and len(st.results) == 1
    assert [a.kind for a in acts] == ["start", "litrev", "result"]
    assert (tmp_path / "events" / "S1.jsonl").read_text().count("\n") == 3


def test_watchdog_stops_a_session_that_never_starts(tmp_path):
    s = Session(spec(tmp_path, {"no_init": True, "hang": True}), caffeinate=False)
    st, code, reason = run_session(s, None, Limits(init_s=0.5, idle_s=60, hard_s=60, poll_s=0.05, grace_int=2, grace_term=2))
    assert reason == "watchdog" and code == 130 and not st.inits


def test_idle_watchdog_waits_while_a_question_is_open(tmp_path):
    s = Session(spec(tmp_path, {"hang": True}), caffeinate=False)
    open_q = {"v": True}
    import threading, time
    threading.Timer(1.0, lambda: open_q.update(v=False)).start()
    t0 = time.monotonic()
    st, code, reason = run_session(s, None, Limits(init_s=5, idle_s=0.5, hard_s=60, poll_s=0.05, grace_int=2, grace_term=2),
                                   question_open=lambda: open_q["v"])
    assert reason == "watchdog" and time.monotonic() - t0 >= 1.0


def test_user_stop(tmp_path):
    s = Session(spec(tmp_path, {"hang": True}), caffeinate=False)
    import threading
    threading.Timer(0.5, lambda: s.stop("user")).start()
    st, code, reason = run_session(s, None, FAST)
    assert reason == "user" and code == 130


def test_read_events_waits_for_a_line_cut_inside_a_multibyte_character(tmp_path):
    sp = SessionSpec("S5", [sys.executable, "-c", "import time; time.sleep(30)"], {"PATH": "/usr/bin:/bin"}, tmp_path,
                     tmp_path / "events" / "S5.jsonl", tmp_path / "events" / "S5.stderr")
    s = Session(sp, caffeinate=False, limits=FAST)
    try:
        line = (json.dumps({"type": "assistant", "text": "You've hit your session limit · resets 9:30pm"},
                           ensure_ascii=False) + "\n").encode()
        cut = line.index("·".encode()) + 1                      # the writer has flushed half of a 2-byte character
        with open(sp.events_path, "ab") as f:
            f.write(line[:cut])
        assert s.read_events() == []
        with open(sp.events_path, "ab") as f:
            f.write(line[cut:])
        assert s.read_events() == [json.loads(line)]
    finally:
        s.stop("user")
        s.proc.wait(timeout=10)
        s.close()
