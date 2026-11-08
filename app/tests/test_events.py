import json
from pathlib import Path

from litrev_app.events import StreamState, litrev_title, LIMIT_TEXT_RE

FX = Path(__file__).parent / "fixtures" / "streams"


def _replay(name):
    st, acts = StreamState(), []
    for line in (FX / name).read_text().splitlines():
        if line.strip():
            acts += st.feed(json.loads(line))
    return st, acts


def test_litrev_title_humanises_commands():
    assert litrev_title('python3 /x/scripts/litrev.py search --term "depth anything" --category concept --review /r') == (
        "Searching", "term depth anything")
    assert litrev_title("python3 /x/scripts/litrev.py status --json --review /r") == ("Checking where the review stands", "")
    assert litrev_title("python3 /x/scripts/litrev.py fulltext P001 P002 --review /r") == ("Fetching full texts", "P001 P002")
    assert litrev_title("ls -la") is None
    # a teammate's feed (2026-10-13): "P034 2>&1", and a chained `ls` shown as the snowball's detail
    assert litrev_title("python3 /x/litrev.py paper P034 --no-search 2>&1") == ("Looking up paper records", "P034")
    assert litrev_title("python3 /x/litrev.py snowball --review /r; ls /r/data") == ("Following citations (snowball)", "")
    assert litrev_title("python3 /x/litrev.py snowball --review /r\nls /r/data") == ("Following citations (snowball)", "")
    assert litrev_title("python3 /x/litrev.py screen P009 --review /r && python3 /x/litrev.py select --review /r") == (
        "Recording a screening decision", "P009")
    assert litrev_title("python3 /x/litrev.py validate --review /r 2>/dev/null | tail -3") == ("Validating the review", "")
    assert litrev_title('python3 /x/litrev.py search --term "a; b" --review /r > /tmp/o') == ("Searching", "term a; b")


def test_s2a_stream_two_results_denials_and_a_subagent():
    st, acts = _replay("s2a-matrix.jsonl")
    assert len(st.results) == 2 and len(st.inits) == 2           # backgrounded subagent re-enters the main agent
    assert len(st.denials) == 4
    kinds = [a.kind for a in acts]
    assert kinds.count("start") == 1 and "agent_start" in kinds and "agent_end" in kinds and kinds.count("denied") == 4
    assert st.running_agents == {}
    assert st.rate_limit and st.rate_limit["rateLimitType"] == "five_hour" and st.limit_rejected is None
    assert any(a.kind == "litrev" and a.title == "Checking where the review stands" for a in acts)


def test_s7_stream_cost_is_the_last_results():
    st, _ = _replay("s7-agents.jsonl")
    assert st.cost_usd == st.results[-1]["total_cost_usd"] and st.cost_usd > 0


def test_rejected_rate_limit_event_is_recorded():
    st = StreamState()
    acts = st.feed({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1790101800,
                                                                     "rateLimitType": "five_hour"}})
    assert st.limit_rejected["resetsAt"] == 1790101800 and acts[0].kind == "limit"


def test_synthetic_limit_message_is_recorded():
    line = json.loads((Path(__file__).parent / "fixtures" / "limit_transcript_line.json").read_text())
    st = StreamState()
    st.feed({"type": "assistant", "message": line["message"], "error": line.get("error"), "parent_tool_use_id": None})
    assert st.limit_rejected and "resets" in st.limit_rejected["text"]
    assert LIMIT_TEXT_RE.search("You've hit your weekly limit · resets Oct 4 at 12am (Asia/Jerusalem)")


def test_ask_tool_use_and_its_result():
    st = StreamState()
    a = st.feed({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__litrev_app__ask_user", "input": {"kind": "scope", "question": "Is this right?"}}]}})
    assert a[0].kind == "ask" and a[0].detail == "Is this right?" and a[0].status == "running"
    b = st.feed({"type": "user", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "User answered; ..."}]}]}})
    assert b[0].kind == "ask_done" and b[0].tool_use_id == "t1"


def test_denied_commands_read_as_a_quiet_skip():
    # live check 2026-10-11: a red "Blocked by the app's rules" row alarmed the user although nothing was wrong
    st = StreamState()
    a = st.feed({"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "x"})[0]
    assert a.kind == "denied" and a.status == "ok" and "Skipped" in a.title


def test_progress_keeps_the_subagents_name_and_shows_its_current_action():
    # live check 2026-10-11: a row created from a progress update kept "Running Verify nuScenes quote" as its title
    st = StreamState()
    st.feed({"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": "u1", "task_type": "local_agent",
             "subagent_type": "litrev-reader", "description": "Survey taxonomy P002"})
    a = st.feed({"type": "system", "subtype": "task_progress", "task_id": "t1", "tool_use_id": "u1",
                 "subagent_type": "litrev-reader", "description": "Running Verify nuScenes quote"})[0]
    assert a.title == "litrev-reader: Survey taxonomy P002" and a.detail == "Running Verify nuScenes quote"


def test_background_shell_commands_are_not_counted_as_subagents():
    # spike S2b and the live check: task_type local_bash tasks showed up as "subagent: <command>"
    st = StreamState()
    a = st.feed({"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": "u2", "task_type": "local_bash",
                 "description": "Search arXiv for term Q010"})[0]
    assert a.kind == "bg_command" and not a.subagent and st.running_agents == {}
    e = st.feed({"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "u2", "status": "completed",
                 "summary": "done"})[0]
    assert e.kind == "bg_command" and e.status == "ok"
