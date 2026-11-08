"""stdio MCP server for Litrev App step sessions: one tool, ask_user, which hands a question to the app and waits
for the app to record the answer (spike S1: a blocking call returned after 2 and 15 min, with a progressToken)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from .checkpoints import ASKED, DECIDE, KINDS, validate

TOOL = {"name": "ask_user",
        "description": "Ask the person the step-0 scope question (or the refresh brief's question) and wait for the "
                       "answer, which the app records before replying. Every other call is yours (app-mode.md).",
        "inputSchema": {"type": "object", "required": ["kind", "question", "proposal"], "properties": {
            "kind": {"type": "string", "enum": list(KINDS)}, "question": {"type": "string"},
            "context": {"type": "string"}, "proposal": {"type": "object"},
            "refs": {"type": "array", "items": {"type": "string"}}}}}


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")   # unique per thread
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Server:
    def __init__(self, review: Path, sid: str, wait_s: float, out=sys.stdout, clock=time.time):
        self.ask_path = review / "cache" / "app" / "ask.json"
        self.sid, self.wait_s, self.out = sid, wait_s, out
        self.clock = clock      # wall time: Claude Code's idle timer counts a sleeping Mac, time.monotonic doesn't
        self._lock = threading.Lock()

    def send(self, msg: dict) -> None:
        with self._lock:
            self.out.write(json.dumps(msg) + "\n")
            self.out.flush()

    def reply(self, rid, text: str) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}})

    def call(self, req: dict) -> None:
        params = req.get("params") or {}
        a = params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")
        if a.get("kind") in KINDS and a.get("kind") not in ASKED:
            self.reply(req["id"], DECIDE.format(kind=a["kind"]))
            return
        err = validate(a.get("kind", ""), a.get("proposal")) if a.get("question") else "a 'question' is required"
        if err:
            self.reply(req["id"], f"INVALID: {err}. Fix the proposal and call ask_user again.")
            return
        qid = uuid.uuid4().hex
        write_json_atomic(self.ask_path, {"id": qid, "sid": self.sid, "kind": a["kind"], "question": a["question"],
                                          "context": a.get("context", ""), "proposal": a["proposal"],
                                          "refs": a.get("refs") or [], "status": "open", "created_at": time.time()})
        t0, beats = self.clock(), 0
        while self.clock() - t0 < self.wait_s:
            d = read_json(self.ask_path) or {}
            if d.get("id") == qid and d.get("status") == "recorded":
                d["status"] = "delivered"
                write_json_atomic(self.ask_path, d)
                self.reply(req["id"], d.get("reply") or "User answered; already recorded. Continue the step.")
                return
            time.sleep(0.5)
            if token is not None and (self.clock() - t0) // 60 > beats:
                beats += 1
                self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": token, "progress": beats, "message": "waiting for the person"}})
        d = read_json(self.ask_path) or {}
        if d.get("id") == qid and d.get("status") == "recorded":      # answered at the last moment
            d["status"] = "delivered"
            write_json_atomic(self.ask_path, d)
            self.reply(req["id"], d.get("reply") or "User answered; already recorded. Continue the step.")
            return
        if d.get("id") == qid:
            d["status"] = "parked"
            write_json_atomic(self.ask_path, d)
        self.reply(req["id"], "PARKED: the person has not answered yet. Write your report with outcome parked and "
                              "end your turn; the app records the answer and starts a new session.")

    def handle(self, req: dict) -> None:
        m, rid = req.get("method"), req.get("id")
        if m == "initialize":
            self.send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": (req.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "litrev_app", "version": "0.1.0"}}})
        elif m == "tools/list":
            self.send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL]}})
        elif m == "tools/call":
            threading.Thread(target=self.call, args=(req,), daemon=True).start()
        elif m == "ping":
            self.send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {m}"}})


def main() -> None:
    s = Server(Path(os.environ["LITREV_APP_REVIEW"]), os.environ.get("LITREV_APP_SID", ""),
               float(os.environ.get("LITREV_APP_WAIT_S", "1800")))
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        s.handle(req)


if __name__ == "__main__":
    main()
