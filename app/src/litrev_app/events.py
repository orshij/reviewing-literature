"""Turn Claude Code stream-json events into activity items and session facts (shapes from spikes S2a, S2b, S3)."""
from __future__ import annotations

import itertools
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

ASK_TOOL = "mcp__litrev_app__ask_user"
LITREV_RE = re.compile(r"python3\s+(?:-I\s+)?\S*litrev\.py\s+([a-z][a-z-]*)")
LIMIT_TEXT_RE = re.compile(r"hit your [\w ]*limit", re.I)
SHELL_STOP_RE = re.compile(r"^(?:;|&&?|\|\|?|\d*[<>].*)$")   # ; & && | || and redirections (2>&1, >file, 2>/dev/null)
BOOL_FLAGS = {"--json", "--strict", "--dry-run", "--report-only", "--no-fetch", "--rescreen", "--needs-text",
              "--suggest", "--list", "--add", "--force", "--no-search", "--all-years", "--from-fulltext",
              "--keep-alias", "--variant-from-name"}
TITLES = {
    "status": "Checking where the review stands", "search": "Searching", "paper": "Looking up paper records",
    "fulltext": "Fetching full texts", "enrich": "Finding code, weights and labs", "select": "Applying the paper cap",
    "screen-input": "Preparing screening packets", "merge-screening": "Applying screening verdicts",
    "screen": "Recording a screening decision", "snowball": "Following citations (snowball)",
    "coverage": "Looking for missed papers", "references": "Walking references", "citations": "Walking citing papers",
    "scope": "Recording the scope", "decide": "Logging a decision", "set": "Updating a field",
    "set-meta": "Updating review settings", "import": "Importing records", "merge-details": "Merging method details",
    "details-status": "Checking method-detail files", "check-details": "Checking a details file",
    "kpi": "Working on KPIs", "rank-papers": "Ranking papers", "figures": "Extracting figures",
    "cluster-input": "Preparing clustering input", "merge-clusters": "Merging method families",
    "explainer-input": "Preparing an explainer", "merge-explainers": "Merging explainers",
    "synthesis-input": "Preparing the synthesis", "merge-synthesis": "Merging the synthesis",
    "emphasis-input": "Preparing highlights", "merge-emphasis": "Merging highlights",
    "adjacent-input": "Preparing adjacent topics", "merge-adjacent": "Merging adjacent topics",
    "report-input": "Preparing the review report", "merge-report": "Merging the review report",
    "validate": "Validating the review", "render": "Rendering the outputs", "list": "Listing records",
    "show": "Reading a record", "dataset-info": "Looking up a dataset", "fold": "Merging duplicate records",
    "delete": "Removing a record", "why-missing": "Checking a missing paper", "refresh": "Checking for new papers",
    "init": "Creating the review", "pwc-index": "Indexing archived leaderboards",
}


@dataclass
class Activity:
    kind: str
    title: str
    detail: str = ""
    tool_use_id: str | None = None
    subagent: bool = False
    status: str = "ok"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "detail": self.detail, "tool_use_id": self.tool_use_id,
                "subagent": self.subagent, "status": self.status}


def litrev_title(command: str) -> tuple[str, str] | None:
    """('Searching', 'term depth anything') for a litrev command line, else None."""
    m = LITREV_RE.search(command or "")
    if not m:
        return None
    cmd = m.group(1)
    tail = command[m.end():].replace("\\\n", " ").split("\n", 1)[0]   # only this command: not a chained `ls`
    try:
        lex = shlex.shlex(tail, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        words = tail.split()
    words = list(itertools.takewhile(lambda w: not SHELL_STOP_RE.match(w), words))
    detail = ""
    for flag in ("--term", "--id", "--table", "--field", "--name"):
        if flag in words and words.index(flag) + 1 < len(words):
            detail = f"{flag[2:]} {words[words.index(flag) + 1]}"
            break
    else:
        pos, skip = [], False
        for w in words:
            if skip:
                skip = False
            elif w.startswith("--"):
                skip = w not in BOOL_FLAGS
            elif not w.startswith("/"):
                pos.append(w)
        detail = " ".join(pos[:4])
    return TITLES.get(cmd, cmd), detail[:80]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


@dataclass
class StreamState:
    inits: list = field(default_factory=list)
    results: list = field(default_factory=list)
    denials: list = field(default_factory=list)
    tools: dict = field(default_factory=dict)            # tool_use_id -> (name, input, parent_tool_use_id)
    running_agents: dict = field(default_factory=dict)   # task_id -> description (subagents only)
    bg_commands: dict = field(default_factory=dict)      # task_id -> description (backgrounded shell commands)
    rate_limit: dict | None = None                       # last rate_limit_info
    limit_rejected: dict | None = None                   # rejected rate_limit_info, or {"text": limit message}
    last_text: str = ""

    @property
    def cost_usd(self) -> float | None:
        return self.results[-1].get("total_cost_usd") if self.results else None

    def feed(self, e: dict) -> list[Activity]:
        t, st = e.get("type"), e.get("subtype")
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        parent = e.get("parent_tool_use_id")
        out: list[Activity] = []
        if t == "system" and st == "init":
            if not self.inits:
                out.append(Activity("start", f"Claude Code started ({e.get('model', '?')})"))
            self.inits.append(e)
        elif t == "assistant":
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    if not parent:
                        self.last_text = c["text"]
                    synthetic = e.get("error") == "rate_limit" or msg.get("model") == "<synthetic>"
                    if synthetic and LIMIT_TEXT_RE.search(c["text"]):
                        self.limit_rejected = self.limit_rejected or {"text": c["text"]}
                        out.append(Activity("limit", "Claude usage limit reached", c["text"], status="failed"))
                elif c.get("type") == "tool_use":
                    self.tools[c["id"]] = (c.get("name"), c.get("input") or {}, parent)
                    out += self._started(c, parent)
        elif t == "user":
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    out += self._finished(c)
        elif t == "system" and st == "task_started" and e.get("task_type") == "local_bash":
            self.bg_commands[e.get("task_id")] = e.get("description", "")      # a backgrounded shell command
            out.append(Activity("bg_command", "Background command", e.get("description", ""),
                                tool_use_id=e.get("tool_use_id"), status="running"))
        elif t == "system" and st == "task_started":
            self.running_agents[e.get("task_id")] = e.get("description", "")
            out.append(Activity("agent_start", f"{e.get('subagent_type') or 'subagent'}: {e.get('description', '')}",
                                tool_use_id=e.get("tool_use_id"), subagent=True, status="running"))
        elif t == "system" and st == "task_progress":
            if e.get("task_id") in self.bg_commands:
                return out
            name = f"{e.get('subagent_type') or 'subagent'}: {self.running_agents.get(e.get('task_id'), '')}".rstrip(": ")
            out.append(Activity("agent_progress", name, e.get("description", ""), tool_use_id=e.get("tool_use_id"),
                                subagent=True, status="running"))     # title = who, detail = what it is doing now
        elif t == "system" and st == "task_notification" and e.get("task_id") in self.bg_commands:
            desc = self.bg_commands.pop(e.get("task_id"))
            out.append(Activity("bg_command", "Background command", desc, tool_use_id=e.get("tool_use_id"),
                                status="ok" if e.get("status") == "completed" else "failed"))
        elif t == "system" and st == "task_notification":
            self.running_agents.pop(e.get("task_id"), None)
            ok = e.get("status") == "completed"
            out.append(Activity("agent_end", (e.get("summary") or "").split("\n")[0][:200],
                                tool_use_id=e.get("tool_use_id"), subagent=True, status="ok" if ok else "failed"))
        elif t == "system" and st == "permission_denied":
            self.denials.append(e)
            _, inp, par = self.tools.get(e.get("tool_use_id"), (None, {}, None))
            out.append(Activity("denied", "Skipped a command the app doesn't allow (Claude carries on)",
                                str(inp.get("command") or inp.get("file_path") or "")[:200],
                                e.get("tool_use_id"), bool(par), "ok"))
        elif t == "system" and st == "api_retry":
            out.append(Activity("retry", "Claude API retry", json.dumps(
                {k: e.get(k) for k in ("attempt", "error", "retry_delay_ms", "error_status") if k in e})))
        elif t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            self.rate_limit = info
            if info.get("status") == "rejected":
                self.limit_rejected = info
                out.append(Activity("limit", "Claude usage limit reached",
                                    f"{info.get('rateLimitType', '')} resets at {info.get('resetsAt')}", status="failed"))
        elif t == "result":
            self.results.append(e)
            out.append(Activity("result", "Session turn finished", f"{e.get('subtype')}, {e.get('num_turns')} turns",
                                status="failed" if e.get("is_error") else "ok"))
        return out

    def _started(self, c: dict, parent) -> list[Activity]:
        name, inp, tid = c.get("name"), c.get("input") or {}, c.get("id")
        if name == "Bash":
            lt = litrev_title(inp.get("command", ""))
            if lt:
                return [Activity("litrev", lt[0], lt[1], tid, bool(parent), "running")]
            return [Activity("tool", "Shell command", str(inp.get("command", ""))[:120], tid, bool(parent))]
        if name == ASK_TOOL:
            return [Activity("ask", "Question for you", str(inp.get("question", "")), tid, False, "running")]
        if name in ("Write", "Edit"):
            return [Activity("tool", f"Writing {Path(str(inp.get('file_path', ''))).name}", "", tid, bool(parent))]
        return []

    def _finished(self, c: dict) -> list[Activity]:
        tid = c.get("tool_use_id")
        name, inp, parent = self.tools.get(tid, (None, {}, None))
        failed = bool(c.get("is_error"))
        if name == "Bash":
            lt = litrev_title(inp.get("command", ""))
            if lt:
                detail = _text(c.get("content")).strip().splitlines()[-1][:200] if failed and _text(c.get("content")).strip() else lt[1]
                return [Activity("litrev_done", lt[0], detail, tid, bool(parent), "failed" if failed else "ok")]
        if name == ASK_TOOL:
            txt = _text(c.get("content"))
            return [Activity("ask_done", "Question parked" if txt.startswith("PARKED") else "Answer received",
                             txt[:200], tid, False, "ok")]
        return []
