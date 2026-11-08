"""The publish handoff's pure parts (spec: Publish handoff): the prompt and settings for the interactive Claude
Code session, the .command lines, and reading the outcome back from the session transcript and meta.json."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import terminal

MAX_HUB_BYTES = 16 * 1024 * 1024
URL_RE = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9-]+")
PUBLISH_ENV = {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}       # spike S2b: the advisor was half of a session's cost


def set_meta_prefix(python: Path, litrev: Path) -> str:
    """The exact command start Claude must type; the settings allow exactly this (absolute venv python: a teammate's
    `python3` may be the Xcode stub)."""
    return f"{terminal.q(python)} {terminal.q(litrev)} set-meta"


def publish_prompt(*, review: Path, hub: Path, python: Path, litrev: Path, hub_url: str | None) -> str:
    if hub_url:
        target = (f"This review already has a hub link: republish in place by passing url {hub_url}. The publish may "
                  "be refused once because this session has not viewed the live version; the refusal itself counts "
                  f"as viewing it. Then publish {hub} again, unchanged: the hub is generated from the review's data, "
                  "so there is nothing on the live page to read or merge.")
    else:
        target = "This hub has not been published before: create a new artifact."
    return "\n".join([
        "Litrev App publish. The person started this from the Litrev App and approves the publish in this window.",
        "1. Load the artifact-design skill.",
        f"2. Publish {hub} unchanged, from that path, as an Artifact with capabilities {{\"downloads\": true}} (the "
        f"hub's BibTeX download needs it). {target}",
        f"3. Run exactly: {set_meta_prefix(python, litrev)} --review {terminal.q(review)} --field hub_url --json "
        "'\"<the artifact url>\"'",
        "4. Stop. Do not edit, re-render or restyle the hub, and change nothing else in the review.",
        "Run no other shell command (no wc, ls or cat): reading the hub with the Read tool is enough, and any other "
        "command needs a second approval from the person.",
    ])


def publish_settings(*, python: Path, litrev: Path, review: Path) -> dict:
    """The window may run the one set-meta command and read the review; it may not change files. Hooks off, so the
    person's SessionStart hooks don't steer the session away from the publish (spike S3)."""
    return {"disableAllHooks": True, "permissions": {
        "allow": [f"Bash({set_meta_prefix(python, litrev)} *)", f"Read(/{review}/**)"],
        "deny": [f"Edit(/{review}/**)", f"Write(/{review}/**)", "Bash(git *)", "Bash(curl *)", "Bash(rm *)",
                 "WebFetch", "WebSearch"]}}


def command_lines(*, root: Path, claude: Path, sid: str, settings: Path, review: Path, prompt: str) -> list[str]:
    """`cd <root>` (the folder-trust dialog then appears once per machine), then claude with the prompt FIRST:
    `--add-dir` takes several directories and swallows a prompt placed after it (checked on 2.1.296).
    LITREV_APP_REVIEW keeps its set-meta in this review (security F1), and `env` sets it over the login shell's."""
    return [f"cd {terminal.q(root)} || exit 1",
            terminal.claude_line(claude, [prompt, "--session-id", sid, "--settings", str(settings), "--add-dir",
                                          str(review)],
                                 set_env={**PUBLISH_ENV, "LITREV_APP_REVIEW": os.path.realpath(review)})]


def find_transcript(projects: Path, sid: str) -> Path | None:
    hits = sorted(projects.glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _same_file(file_path, hub: Path, cwd: Path) -> bool:
    if not file_path:
        return False
    p = Path(str(file_path)).expanduser()
    return os.path.normpath(p if p.is_absolute() else cwd / p) == os.path.normpath(hub)


def transcript_url(path: Path, hub: Path, cwd: Path) -> str | None:
    """URL of the last successful Artifact publish of `hub` in a session transcript. Only that tool's own result
    counts: the hub's text links other artifacts (the Pipeline Atlas), the republish prompt quotes the old URL, and a
    refused republish (is_error) carries no new URL."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    ours, url = set(), None
    for raw in lines:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Artifact":
                inp = b.get("input") or {}
                if inp.get("action", "publish") == "publish" and not inp.get("asset") and \
                        _same_file(inp.get("file_path"), hub, cwd):
                    ours.add(b.get("id"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in ours and not b.get("is_error"):
                tur = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
                m = URL_RE.search(str(tur.get("url") or "")) or URL_RE.search(_text(b.get("content")))
                if m:
                    url = m.group(0)
    return url


def iso_ts(s) -> float | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def meta_url(meta: dict, since: float) -> str | None:
    """hub_url when `set-meta --field hub_url` ran at or after `since` (litrev stamps step_log to the second)."""
    url = meta.get("hub_url")
    at = iso_ts((meta.get("step_log") or {}).get("set-meta:hub_url"))
    return url if url and at is not None and at >= int(since) else None
