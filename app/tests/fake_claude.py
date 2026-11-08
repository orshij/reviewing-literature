#!/usr/bin/env python3
"""A stand-in for `claude -p` in tests. Replays the scenario in $FAKE_CLAUDE_SCENARIO: stream events, sleeps, file
writes, a session report, an ask_user call through the MCP server named in --mcp-config (as a real MCP client), and
assistant text, streamed as partial-message deltas when --include-partial-messages is passed (shapes from spike
S10, fixtures/streams/chat-1.jsonl).

Scenario: {"events": [<stream event> | {"$sleep": s} | {"$ask": {...}} | {"$write": {"path", "content"}} |
{"$report": {...}} | {"$text": "..."}], "result": <result event or null>, "exit": 0, "hang": false,
"no_init": false, "resume_missing": false}. {"runs": [scenario, ...]} gives each invocation its own scenario (a
counter file next to the scenario file counts invocations; the last one repeats). Paths and text may use {review}
and {sid}. $FAKE_CLAUDE_LOG, when set, gets one JSON line per invocation: {"argv": [...], "cwd": "...",
"env": {"LITREV_APP_REVIEW": ...}}."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
argv = sys.argv[1:]
sid = (argv[argv.index("--session-id") + 1] if "--session-id" in argv
       else argv[argv.index("--resume") + 1] if "--resume" in argv else "nosid")
partial = "--include-partial-messages" in argv
review = os.getcwd()
sc_path = Path(os.environ["FAKE_CLAUDE_SCENARIO"])
sc = json.loads(sc_path.read_text())
if "runs" in sc:
    counter = sc_path.with_name(sc_path.name + ".count")
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    sc = sc["runs"][min(n, len(sc["runs"]) - 1)]
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "cwd": review,
                              "env": {"LITREV_APP_REVIEW": os.environ.get("LITREV_APP_REVIEW")}}) + "\n")


def emit(e):
    sys.stdout.write(json.dumps(e) + "\n")
    sys.stdout.flush()


def sub(s):
    return s.replace("{review}", review).replace("{sid}", sid) if isinstance(s, str) else s


def say(text):
    """Assistant text: partial-message deltas (8 characters each) when asked for, then the complete event."""
    if partial:
        base = {"type": "stream_event", "session_id": sid, "parent_tool_use_id": None}
        emit({**base, "event": {"type": "message_start", "message": {"role": "assistant", "content": []}}})
        emit({**base, "event": {"type": "content_block_start", "index": 0,
                                "content_block": {"type": "text", "text": ""}}})
        for i in range(0, len(text), 8):
            emit({**base, "event": {"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "text_delta", "text": text[i:i + 8]}}})
        emit({**base, "event": {"type": "content_block_stop", "index": 0}})
        emit({**base, "event": {"type": "message_stop"}})
    emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
          "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def ask(args):
    cfg = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]["litrev_app"]
    p = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env={**os.environ, **cfg.get("env", {})})
    def rpc(msg):
        p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()
        while True:
            r = json.loads(p.stdout.readline())
            if r.get("id") == msg.get("id"):
                return r
    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); p.stdin.flush()
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "ask1", "name": "mcp__litrev_app__ask_user", "input": args}]}})
    r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "ask_user", "arguments": args, "_meta": {"progressToken": 7}}})
    text = r["result"]["content"][0]["text"]
    emit({"type": "user", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "ask1", "content": [{"type": "text", "text": text}]}]}})
    p.stdin.close(); p.wait(timeout=10)
    return text


if sc.get("resume_missing") and "--resume" in argv:
    sys.stderr.write(f"No conversation found with session ID: {sid}\n")
    sys.exit(1)
if not sc.get("no_init"):
    emit({"type": "system", "subtype": "init", "model": "fake", "session_id": sid})
parked = False
for ev in sc.get("events", []):
    if "$sleep" in ev:
        time.sleep(ev["$sleep"])
    elif "$write" in ev:
        p = Path(sub(ev["$write"]["path"])); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sub(ev["$write"]["content"]))
    elif "$report" in ev:
        rep = dict(ev["$report"]); rep["outcome"] = "parked" if parked else rep.get("outcome", "done")
        p = Path(review) / "cache" / "app" / f"report-{sid}.json"; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep))
    elif "$ask" in ev:
        parked = ask(ev["$ask"]).startswith("PARKED")
    elif "$text" in ev:
        say(sub(ev["$text"]))
    else:
        emit(ev)
while sc.get("hang"):
    time.sleep(1)
if sc.get("result", "default") is not None:
    emit(sc.get("result") if isinstance(sc.get("result"), dict) else
         {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.01,
          "result": "ok", "session_id": sid})
sys.exit(sc.get("exit", 0))
