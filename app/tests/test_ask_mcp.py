import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def server(review, wait):
    env = {**os.environ, "LITREV_APP_REVIEW": str(review), "LITREV_APP_SID": "S1", "LITREV_APP_WAIT_S": str(wait),
           "PYTHONPATH": str(SRC)}
    p = subprocess.Popen([sys.executable, "-m", "litrev_app.ask_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env=env)
    def rpc(msg):
        p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
        while True:
            r = json.loads(p.stdout.readline())
            if r.get("id") == msg["id"]:
                return r
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})["result"]["serverInfo"]["name"] == "litrev_app"
    return p, rpc


def call(rpc, args, i=3):
    return rpc({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "ask_user", "arguments": args}})["result"]["content"][0]["text"]


SCOPE = {"kind": "scope", "question": "Right scope?", "context": "c", "proposal": {"in": ["a"], "out": [], "protocol": []}}


def test_lists_one_tool(tmp_path):
    p, rpc = server(tmp_path, 5)
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["ask_user"]
    p.stdin.close(); p.wait(5)


def test_invalid_proposal_is_refused_without_writing(tmp_path):
    p, rpc = server(tmp_path, 5)
    assert call(rpc, {"kind": "scope", "question": "q", "proposal": {"in": []}}).startswith("INVALID:")
    assert not (tmp_path / "cache" / "app" / "ask.json").exists()
    p.stdin.close(); p.wait(5)


def test_recorded_answer_is_returned_and_marked_delivered(tmp_path):
    p, rpc = server(tmp_path, 30)
    ask = tmp_path / "cache" / "app" / "ask.json"
    def answer():
        while not ask.exists():
            time.sleep(0.05)
        d = json.loads(ask.read_text()); assert d["status"] == "open" and d["kind"] == "scope" and d["sid"] == "S1"
        d.update(status="recorded", reply="User answered; already recorded with: scope. Continue.")
        ask.write_text(json.dumps(d))
    threading.Thread(target=answer).start()
    assert call(rpc, SCOPE).startswith("User answered; already recorded with: scope")
    assert json.loads(ask.read_text())["status"] == "delivered"
    p.stdin.close(); p.wait(5)


def test_no_answer_parks(tmp_path):
    p, rpc = server(tmp_path, 1)
    assert call(rpc, SCOPE).startswith("PARKED:")
    assert json.loads((tmp_path / "cache" / "app" / "ask.json").read_text())["status"] == "parked"
    p.stdin.close(); p.wait(5)


def test_the_wait_is_wall_clock_time(tmp_path):
    """Live M2 run: the Mac slept during a question; the wait counted on time.monotonic (which stops in sleep),
    Claude Code's own 1800 s idle timer counted wall time and aborted the call first."""
    import io
    from litrev_app.ask_mcp import Server
    t = [1000.0]

    def clock():
        t[0] += 700.0                  # each look at the clock: 700 s of wall time (a sleeping Mac)
        return t[0]
    out = io.StringIO()
    Server(tmp_path, "S1", 1800, out=out, clock=clock).call(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ask_user", "arguments": SCOPE}})
    assert "PARKED:" in out.getvalue()
    assert json.loads((tmp_path / "cache" / "app" / "ask.json").read_text())["status"] == "parked"


def test_only_scope_and_refresh_are_asked_the_rest_is_left_to_claude(tmp_path):
    """The user (2026-10-12): fewer questions in the app. A boundary/off-topic/cap/KPI/free question stopped the run
    until the person answered; the tool now tells the session to decide it and writes no ask."""
    p, rpc = server(tmp_path, 5)
    left = [("kpi", {"kpis": [{"benchmark": "B001", "metric": "M001", "splits": ["test"]}]}),
            ("boundaries", {"boundaries": [{"area": "stereo", "recommend": "out", "papers": ["P012"]}]}),
            ("included_off_topic", {"papers": [{"id": "P012", "recommend": "drop"}]}),
            ("cap", {"current": 40, "proposed": 50}), ("free", {"options": ["a", "b"]})]
    for i, (kind, proposal) in enumerate(left):
        r = call(rpc, {"kind": kind, "question": "q?", "context": "c", "proposal": proposal}, i=10 + i)
        assert r.startswith("DECIDE:") and "app-mode.md" in r and "--by agent" in r, (kind, r)
    assert not (tmp_path / "cache" / "app" / "ask.json").exists()
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert "scope" in tools[0]["description"]
    p.stdin.close(); p.wait(5)
