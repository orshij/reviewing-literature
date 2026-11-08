import json
import os
import subprocess
import sys
from pathlib import Path

FAKE = Path(__file__).parent / "fake_claude.py"
FX = Path(__file__).parent / "fixtures" / "streams"


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run(tmp_path, env, *args):
    p = subprocess.run([sys.executable, str(FAKE), "-p", "hi", *args], cwd=tmp_path, env=env, capture_output=True,
                       text=True)
    return p, [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def test_spike_fixtures_show_the_shapes_chat_relies_on():
    one, two = _events(FX / "chat-1.jsonl"), _events(FX / "chat-2.jsonl")
    deltas = [e for e in one if e.get("type") == "stream_event"
              and e["event"].get("type") == "content_block_delta" and e["event"]["delta"].get("type") == "text_delta"]
    assert deltas and all("parent_tool_use_id" in e for e in deltas)
    final = "".join(c["text"] for e in one if e.get("type") == "assistant" and not e.get("parent_tool_use_id")
                    for c in e["message"]["content"] if c.get("type") == "text")
    assert "".join(e["event"]["delta"]["text"] for e in deltas) == final
    assert "PELICAN" in [e for e in two if e.get("type") == "result"][-1]["result"].upper()


def test_runs_resume_partial_deltas_and_the_argv_log(tmp_path):
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps({"runs": [{"events": [{"$text": "first answer"}]}, {"events": [{"$text": "second"}]}]}))
    log = tmp_path / "argv.jsonl"
    env = {**os.environ, "FAKE_CLAUDE_SCENARIO": str(sc), "FAKE_CLAUDE_LOG": str(log)}
    _, a = _run(tmp_path, env, "--session-id", "S1", "--include-partial-messages")
    deltas = "".join(e["event"]["delta"]["text"] for e in a
                     if e["type"] == "stream_event" and e["event"]["type"] == "content_block_delta")
    assert deltas == "first answer" and a[0]["session_id"] == "S1" and a[-1]["session_id"] == "S1"
    _, b = _run(tmp_path, env, "--resume", "S1")
    assert not any(e["type"] == "stream_event" for e in b) and b[0]["session_id"] == "S1"
    assert [c["text"] for e in b if e["type"] == "assistant" for c in e["message"]["content"]] == ["second"]
    logged = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert logged[1][logged[1].index("--resume") + 1] == "S1" and "--session-id" in logged[0]


def test_resume_missing(tmp_path):
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps({"resume_missing": True, "events": [{"$text": "fresh"}]}))
    env = {**os.environ, "FAKE_CLAUDE_SCENARIO": str(sc)}
    p, out = _run(tmp_path, env, "--resume", "GONE")
    assert p.returncode == 1 and "No conversation found" in p.stderr and out == []
    p, out = _run(tmp_path, env, "--session-id", "NEW")
    assert p.returncode == 0 and out[0]["session_id"] == "NEW"
