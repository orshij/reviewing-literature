import json
import os
import threading
import time
from pathlib import Path

import pytest

from m2help import FakeRouter, HAS_SKILL, argv_log, make_app, mini_review, review_env_log, scenario
from litrev_app import chat
from litrev_app.store import RunState, Store

FX = Path(__file__).parent / "fixtures" / "streams"
needs_skill = pytest.mark.skipif(not HAS_SKILL, reason="needs the skill")


def RESULT(cost, text):
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": cost,
            "result": text}


def _router():
    rt = FakeRouter()
    chat.register(rt)
    return rt


def _send(app, r, text, rt=None):
    n = len(app.bus.events)
    code, body, _ = (rt or _router()).call(app, "POST", f"/api/reviews/{r.name}/chat", {"text": text})
    return code, body, n


def _done(app, since):
    return app.bus.wait_for(lambda e: e.get("type") == "chat" and e.get("event") == "done", since=since)


def test_stream_text_follows_the_spike_fixture():
    ev = [json.loads(line) for line in (FX / "chat-1.jsonl").read_text().splitlines() if line.strip()]
    streamed = "".join(t for t in map(chat.stream_text, ev) if t)
    assert streamed and streamed == "".join(t for e in ev for t in chat.assistant_texts(e))
    sub = {"type": "stream_event", "parent_tool_use_id": "toolu_1",
           "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a subagent's words"}}}
    assert chat.stream_text(sub) is None
    assert chat.assistant_texts({"type": "assistant", "parent_tool_use_id": "toolu_1",
                                 "message": {"content": [{"type": "text", "text": "x"}]}}) == []


def test_message_cost_per_run_and_cumulative():
    assert chat.message_cost(0.05, None, cumulative=True) == 0.05
    assert chat.message_cost(0.08, 0.05, cumulative=True) == pytest.approx(0.03)
    assert chat.message_cost(0.02, 0.05, cumulative=True) == 0.02        # a smaller total is a fresh session
    assert chat.message_cost(0.02, 0.05, cumulative=False) == 0.02
    assert chat.message_cost(None, 0.05) is None


def test_run_from_line():
    assert chat.run_from("Added P101.\nRUN FROM STEP 4\n") == 4
    assert chat.strip_run_from("Added P101.\nRUN FROM STEP 4\n") == "Added P101."
    assert chat.run_from("run from step 4 is what I would do") is None


@needs_skill
def test_first_message_creates_the_session_and_the_next_resumes_it(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"runs": [
        {"events": [{"$text": "P007 is over the cap."}], "result": RESULT(0.05, "P007 is over the cap.")},
        {"events": [{"$text": "Added P007.\nRUN FROM STEP 4"}], "result": RESULT(0.02, "Added P007.")}]})
    app = make_app(tmp_path)
    r = mini_review(app)
    rt = _router()
    code, body, n = _send(app, r, "Why isn't P007 in the review?", rt)
    assert code == 202 and body["profile"] == "chat_rw"
    done = _done(app, n)
    deltas = "".join(e["text"] for _, e in app.bus.events[n:] if e.get("event") == "delta")
    assert deltas == "P007 is over the cap." and done["message"]["text"] == "P007 is over the cap."
    _, _, n = _send(app, r, "Add it.", rt)
    _done(app, n)
    first, second = argv_log(tmp_path)
    sid = first[first.index("--session-id") + 1]
    assert second[second.index("--resume") + 1] == sid and "--session-id" not in second
    assert "--no-session-persistence" not in first + second
    allow = json.loads(Path(second[second.index("--settings") + 1]).read_text())["permissions"]["allow"]
    assert any(a.startswith("Edit(") for a in allow)
    assert review_env_log(tmp_path) == [os.path.realpath(r)] * 2          # security F1: chat_rw is confined too
    h = rt.call(app, "GET", f"/api/reviews/{r.name}/chat")[1]
    assert h["sid"] == sid and [m["role"] for m in h["messages"]] == ["user", "assistant", "user", "assistant"]
    last = h["messages"][-1]
    assert last["text"] == "Added P007." and last["run_from"] == 4 and last["profile"] == "chat_rw"
    assert last["cost_usd"] == pytest.approx(0.02) and h["cost_usd"] == pytest.approx(0.07)
    assert h["running"] is False and h["mode"] == "read_write" and app.engine.writers.holder(str(r)) is None


@needs_skill
def test_read_only_while_the_review_runs_its_steps(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"events": [{"$text": "I can only read right now. RUN FROM STEP 4"}]})
    app = make_app(tmp_path)
    r = mini_review(app)
    Store(r).save(RunState(step=5, phase="running"))
    code, body, n = _send(app, r, "Add P007")
    assert code == 202 and body["profile"] == "chat_ro"
    done = _done(app, n)
    argv = argv_log(tmp_path)[-1]
    allow = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())["permissions"]["allow"]
    assert not any(a.startswith(("Edit", "Write")) for a in allow)
    assert "Mode: read-only" in argv[argv.index("--append-system-prompt") + 1]
    assert "step 5 starts next" in argv[argv.index("--append-system-prompt") + 1]
    assert done["message"]["run_from"] is None and done["mode"] == "read_only"
    assert review_env_log(tmp_path)[-1] == os.path.realpath(r)          # security F1: chat_ro is confined too


@needs_skill
def test_one_message_at_a_time(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"events": [{"$sleep": 1.0}, {"$text": "done"}]})
    app = make_app(tmp_path)
    r = mini_review(app)
    code1, _, n = _send(app, r, "first")
    code2, body2, _ = _send(app, r, "second")
    assert code1 == 202 and code2 == 409 and "previous message" in body2["error"]
    _done(app, n)


@needs_skill
def test_a_step_started_mid_message_waits_for_it(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"runs": [{"events": [{"$sleep": 1.0}, {"$text": "changed"}]},
                                              {"events": [{"$report": {"step": 7, "summary": "s"}}]}]})
    app = make_app(tmp_path)
    r = mini_review(app)
    code, body, n = _send(app, r, "add a paper")
    assert body["profile"] == "chat_rw"
    time.sleep(0.2)
    t = threading.Thread(target=app.engine.run_one, args=(r,))
    t.start()
    done = _done(app, n)
    t.join(60)
    assert Store(r).sessions()[-1]["start"] >= done["message"]["ts"]


@needs_skill
def test_missing_transcript_starts_a_new_conversation(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"resume_missing": True, "events": [{"$text": "Fresh answer."}],
                                     "result": RESULT(0.03, "Fresh answer.")})
    app = make_app(tmp_path)
    r = mini_review(app)
    old = "11111111-1111-1111-1111-111111111111"
    chat.ChatStore(r).save({"sid": old, "messages": [{"role": "user", "text": "old", "ts": 1.0}], "cost_usd": 0.4,
                            "last_total_usd": 0.4})
    _, _, n = _send(app, r, "hello again")
    done = _done(app, n)
    a, b = argv_log(tmp_path)
    assert a[a.index("--resume") + 1] == old and "--session-id" in b
    assert "new conversation" in done["message"]["text"] and "Fresh answer." in done["message"]["text"]
    assert done["message"]["cost_usd"] == pytest.approx(0.03)
    assert chat.ChatStore(r).load()["sid"] == b[b.index("--session-id") + 1]


@needs_skill
def test_routes_run_steps_and_refusals(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, {"events": []})
    app = make_app(tmp_path)
    r = mini_review(app)
    rt = _router()
    assert rt.call(app, "GET", "/api/reviews/nope/chat")[0] == 404
    assert rt.call(app, "POST", f"/api/reviews/{r.name}/chat", {"text": "   "})[0] == 400
    Store(r).save(RunState(step=5, phase="running"))
    assert rt.call(app, "POST", f"/api/reviews/{r.name}/chat/run-steps", {"step": 4})[0] == 409
    Store(r).save(RunState(step=7, phase="done"))
    assert rt.call(app, "POST", f"/api/reviews/{r.name}/chat/run-steps", {"step": 9})[0] == 400
    code, body, _ = rt.call(app, "POST", f"/api/reviews/{r.name}/chat/run-steps", {"step": 4})
    st = Store(r).load()
    assert code == 200 and (st.step, st.phase) == (4, "running") and "do not run select" in st.notes.lower()
    assert app.scheduler.woken == 1
    Store(r).save(RunState(step=7, phase="done"))
    assert app.engine.writers.freeze() is None
    assert rt.call(app, "POST", f"/api/reviews/{r.name}/chat", {"text": "hi"})[0] == 409
    app.engine.writers.thaw()
    assert rt.call(app, "POST", f"/api/reviews/{r.name}/chat/reset")[0] == 200


def test_chat_is_a_registered_feature():
    from litrev_app import server
    assert "litrev_app.chat" in server.FEATURES


@needs_skill
def test_a_chat_sid_that_is_not_a_uuid_starts_a_new_conversation(tmp_path, monkeypatch):
    """Security F2: chat.json is writable by step and chat sessions; its sid builds <appdir>/sessions/chat-<sid>."""
    scenario(monkeypatch, tmp_path, {"events": [{"$text": "Fresh answer."}], "result": RESULT(0.02, "Fresh answer.")})
    app = make_app(tmp_path)
    r = mini_review(app)
    chat.ChatStore(r).save({"sid": "../../evil", "messages": [{"role": "user", "text": "old", "ts": 1.0}],
                            "cost_usd": 0.1, "last_total_usd": 0.1})
    _, _, n = _send(app, r, "hello")
    _done(app, n)
    (a,) = argv_log(tmp_path)
    assert "--resume" not in a and "--session-id" in a
    assert not list(tmp_path.rglob("evil")) and not (tmp_path / "appdir" / "sessions" / "chat-..").exists()
    assert chat.ChatStore(r).load()["sid"] == a[a.index("--session-id") + 1]
