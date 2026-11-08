"""Build one Claude Code session: argv, child environment and the per-session settings, agents and MCP files."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ASK_TOOL = "mcp__litrev_app__ask_user"
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
BG_WAIT_CEILING_MS = 2 * 60 * 60 * 1000    # -p stops background subagents 10 min after the last turn by default; a
                                           # step-5 extraction of a big paper takes longer (Depth Anything app run)
BASH_MAX_TIMEOUT_MS = 18 * 60 * 1000         # step 8: `audio` / `video` run for minutes; under Limits.idle_s (20 min)
READ_ONLY_CMDS = ("status", "show", "list", "validate", "why-missing", "details-status", "check-details")
CHAT_RO_EXTRA = ("kpi --suggest", "kpi --list", "select --preview")   # read-only forms of writing commands
PKG_PARENT = Path(__file__).resolve().parent.parent          # lets `python -m litrev_app.ask_mcp` import in dev
SUBAGENT_RULES = (
    "Litrev App session rules for shell commands: run only `python3 {litrev} <cmd> ... --review {review}` and single "
    "read-only commands such as `grep -F -- '<text>' <file>` or `head`. No `python3 -c`, no shell loops, no `;` or "
    "`&&` chains, no `cd`, `curl`, `git` or `rm`: they are denied and waste turns. To check that a quote is in a "
    "paper's text, run one `grep -F -- '<quote>' <cached text file>` per quote; to check that a JSON file you wrote "
    "parses, run `python3 {litrev} check-json <file>`. Write files only under {review}.")


@dataclass
class SessionFiles:
    settings: Path
    agents: Path
    mcp: Path


def child_env(base: dict, *, litrev_home: Path, venv_bin: Path, claude_dir: Path, concurrency: int,
              mailto: str | None = None, keys: dict | None = None, review: Path | None = None) -> dict:
    """The session's environment. No API key or gateway: in -p a key always wins over the claude.ai login.
    `review`: a session's review, as LITREV_APP_REVIEW; litrev.py then refuses `init`, any other --review and
    import/check-json paths outside it (security F1: `Bash(python3 litrev.py *)` takes any path). The app's own
    litrev calls pass none and never inherit one (the app runs `init` itself)."""
    env = {k: v for k, v in base.items() if k not in STRIP_ENV and not k.startswith("CLAUDE_CODE_USE_")
           and k != "LITREV_APP_REVIEW"}
    if review is not None:
        env["LITREV_APP_REVIEW"] = os.path.realpath(review)      # litrev resolves both sides
    env["PATH"] = os.pathsep.join([str(venv_bin), str(claude_dir), "/opt/homebrew/bin", "/usr/local/bin",
                                   "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env.update(LITREV_HOME=str(litrev_home), DISABLE_AUTOUPDATER="1",
               CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=str(concurrency),
               CLAUDE_CODE_DISABLE_ADVISOR_TOOL="1",    # S2b: the advisor was half of a step's cost
               CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=str(BG_WAIT_CEILING_MS),
               # step 8's `audio` and `video` run for minutes with no output: allow a Bash call up to 18 min, under
               # the runner's 20-min idle watchdog (Limits.idle_s)
               BASH_MAX_TIMEOUT_MS=str(BASH_MAX_TIMEOUT_MS))
    if mailto:
        env["LITREV_MAILTO"] = mailto
    env.update({k: v for k, v in (keys or {}).items() if v})
    return env


# The steps whose sessions fetch web pages (dataset homepages, eval.ai, checkpoint READMEs: 297 WebFetch calls in 50
# app sessions, all in steps 3-5). WebFetch is the channel a prompt-injected session could send data out through, so
# no other session gets it: not step 1 (hundreds of outside abstracts) or 6 (whole papers), chat or refresh.
WEB_STEPS = (3, 4, 5)


def settings(profile: str, *, litrev: Path, skill: Path, review: Path, web: bool = False) -> dict:
    """Permission rules (spike S2a): the skill's command and files under the review; reads only there and in the
    skill. `Path` objects are absolute, so `Edit(/{review}/**)` is the `//abs` form. No bare "Glob"/"Grep" (security
    F5, RESULTS.md): where Claude Code offers those tools, a bare allow let them read outside the Read paths, and the
    Read rules alone keep them working inside."""
    L = f"python3 {litrev}"
    deny = ["Artifact", "AskUserQuestion", "Bash(git *)", "Bash(curl *)", "Bash(rm *)", "WebSearch",
            "ScheduleWakeup", "CronCreate"]     # spike S2b: subagent notifications re-enter the agent; no wait loops
    reads = [f"Read(/{review}/**)", f"Read(/{skill}/**)"]
    if profile in ("step", "chat_rw"):
        allow = [f"Bash({L} *)", f"Edit(/{review}/**)", f"Write(/{review}/**)", *reads, "Agent", ASK_TOOL]
        if web and profile == "step":
            allow.append("WebFetch")
    elif profile == "chat_ro":
        allow = [f"Bash({L} {c} *)" for c in READ_ONLY_CMDS] + reads
        allow += [f"Bash({L} {c} *)" for c in CHAT_RO_EXTRA]
        deny += ["Edit", "Write", "Agent"]
    else:
        raise ValueError(f"unknown profile {profile}")
    return {"disableAllHooks": True, "permissions": {"allow": allow, "deny": deny}}


def agents(skill: Path, step5_model: str | None = None) -> dict:
    """`--agents` definitions from the skill's agents/*.md (spike S7); litrev-details gets the step-5 model."""
    out = {}
    for f in sorted((skill / "agents").glob("*.md")):
        m = re.match(r"---\n(.*?)\n---\n(.*)", f.read_text(), re.S)
        if not m:
            continue
        fm = dict(re.findall(r"^(\w+):[ \t]*(.*)$", m.group(1), re.M))
        a = {"description": fm.get("description", ""), "prompt": m.group(2).strip()}
        if fm.get("tools"):
            a["tools"] = [t.strip() for t in fm["tools"].split(",") if t.strip()]
        model = step5_model if fm.get("name") == "litrev-details" and step5_model else fm.get("model")
        if model and model != "inherit":
            a["model"] = model
        out[fm["name"]] = a
    return out


def mcp_config(python: Path, review: Path, sid: str, wait_s: int) -> dict:
    """`timeout` (ms): Claude Code aborts an MCP call that is silent for its idle timeout (1800 s in 2.1.296, which
    sends no progress token, so the server can't send heartbeats); ask_user must outlast its own wait (M2 live run)."""
    return {"mcpServers": {"litrev_app": {
        "type": "stdio", "command": str(python), "args": ["-m", "litrev_app.ask_mcp"],
        "timeout": int((float(wait_s) + 900) * 1000),
        "env": {"LITREV_APP_REVIEW": str(review), "LITREV_APP_SID": sid, "LITREV_APP_WAIT_S": str(wait_s),
                "PYTHONPATH": str(PKG_PARENT)}}}}


def write_session_files(dirpath: Path, *, profile: str, litrev: Path, skill: Path, review: Path, python: Path,
                        sid: str, wait_s: int, step5_model: str | None, web: bool = False) -> SessionFiles:
    dirpath.mkdir(parents=True, exist_ok=True)
    f = SessionFiles(dirpath / "settings.json", dirpath / "agents.json", dirpath / "mcp.json")
    f.settings.write_text(json.dumps(settings(profile, litrev=litrev, skill=skill, review=review, web=web), indent=1))
    f.agents.write_text(json.dumps(agents(skill, step5_model), indent=1))
    f.mcp.write_text(json.dumps(mcp_config(python, review, sid, wait_s), indent=1))
    return f


def step_argv(claude: Path, brief: str, sid: str, files: SessionFiles, *, skill: Path, review: Path, litrev: Path,
              model: str | None = None, effort: str | None = None, max_turns: int = 400) -> list[str]:
    """One step or refresh session. `--effort` also sets its subagents' effort (probe 2026-10-13: hook payloads)."""
    argv = [str(claude), "-p", brief, "--output-format", "stream-json", "--verbose", "--session-id", sid,
            "--no-session-persistence", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--settings", str(files.settings), "--agents", str(files.agents), "--add-dir", str(skill),
            "--mcp-config", str(files.mcp), "--strict-mcp-config", "--max-turns", str(max_turns),
            "--append-subagent-system-prompt", SUBAGENT_RULES.format(litrev=litrev, review=review)]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv


def write_chat_files(dirpath: Path, *, profile: str, litrev: Path, skill: Path, review: Path,
                     step5_model: str | None) -> SessionFiles:
    """Settings (chat_rw or chat_ro), the skill's agents and an empty MCP config: chat has no ask_user tool, it asks
    in its reply."""
    dirpath.mkdir(parents=True, exist_ok=True)
    f = SessionFiles(dirpath / "settings.json", dirpath / "agents.json", dirpath / "mcp.json")
    f.settings.write_text(json.dumps(settings(profile, litrev=litrev, skill=skill, review=review), indent=1))
    f.agents.write_text(json.dumps(agents(skill, step5_model), indent=1))
    f.mcp.write_text(json.dumps({"mcpServers": {}}, indent=1))
    return f


def chat_argv(claude: Path, message: str, sid: str, files: SessionFiles, *, resume: bool, skill: Path, review: Path,
              litrev: Path, system_prompt: str, model: str | None = None, effort: str | None = None,
              max_turns: int = 60) -> list[str]:
    """One chat message. Persisted (no --no-session-persistence): the first message creates the session with
    --session-id, later ones --resume it. Text deltas are streamed (--include-partial-messages, spike S10)."""
    text = " " + message if message.startswith("-") else message      # never parsed as an option
    argv = [str(claude), "-p", text, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            *(["--resume", sid] if resume else ["--session-id", sid]),
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--settings", str(files.settings),
            "--agents", str(files.agents), "--add-dir", str(skill), "--mcp-config", str(files.mcp),
            "--strict-mcp-config", "--max-turns", str(max_turns), "--append-system-prompt", system_prompt,
            "--append-subagent-system-prompt", SUBAGENT_RULES.format(litrev=litrev, review=review)]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv
