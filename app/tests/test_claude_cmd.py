import json
import os
from pathlib import Path

from litrev_app import claude_cmd as cc


def test_child_env_drops_api_keys_and_sets_litrev(tmp_path):
    base = {"ANTHROPIC_API_KEY": "sk", "ANTHROPIC_BASE_URL": "x", "CLAUDE_CODE_USE_BEDROCK": "1", "HOME": "/h",
            "CLAUDE_CODE_OAUTH_TOKEN": "keep"}
    env = cc.child_env(base, litrev_home=tmp_path, venv_bin=Path("/v/bin"), claude_dir=Path("/c"), concurrency=3,
                       mailto="a@b.c", keys={"OPENALEX_API_KEY": "k", "S2_API_KEY": ""})
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep" and env["HOME"] == "/h"
    assert env["PATH"].split(":")[:2] == ["/v/bin", "/c"]
    assert env["LITREV_HOME"] == str(tmp_path) and env["LITREV_MAILTO"] == "a@b.c" and env["OPENALEX_API_KEY"] == "k"
    assert "S2_API_KEY" not in env
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "3"


def test_child_env_names_the_review_only_for_a_session(tmp_path):
    # security F1: litrev.py refuses --review, init and import/check-json paths outside $LITREV_APP_REVIEW. Every
    # session gets it; the app's own litrev calls (it runs init itself) never do, not even when it is inherited
    kw = dict(litrev_home=tmp_path, venv_bin=Path("/v"), claude_dir=Path("/c"), concurrency=3)
    assert "LITREV_APP_REVIEW" not in cc.child_env({"LITREV_APP_REVIEW": "/elsewhere"}, **kw)
    env = cc.child_env({"LITREV_APP_REVIEW": "/elsewhere"}, review=tmp_path / "rv", **kw)
    assert env["LITREV_APP_REVIEW"] == os.path.realpath(tmp_path / "rv")


def test_step_settings_allow_only_the_skill_and_the_review(tmp_path):
    s = cc.settings("step", litrev=Path("/sk/scripts/litrev.py"), skill=Path("/sk"), review=Path("/r/x"))
    assert s["disableAllHooks"] is True
    allow, deny = s["permissions"]["allow"], s["permissions"]["deny"]
    assert "Bash(python3 /sk/scripts/litrev.py *)" in allow
    assert "Edit(//r/x/**)" in allow and "Write(//r/x/**)" in allow and "Read(//r/x/**)" in allow
    assert cc.ASK_TOOL in allow and "Agent" in allow
    assert "Read" not in allow                                  # reads stay inside the review and the skill
    assert {"Artifact", "AskUserQuestion", "Bash(curl *)", "Bash(rm *)", "Bash(git *)"} <= set(deny)


def test_no_bare_glob_or_grep_rule():
    # security F5, live check on 2.1.299 (spikes/RESULTS.md): where Claude Code offers Grep and Glob (sessions without
    # Bash), a bare "Grep"/"Glob" allow let a dontAsk session grep and list files outside the Read(...) paths; without
    # them those calls were denied and Grep/Glob inside the Read paths still worked
    for profile in ("step", "chat_rw", "chat_ro"):
        allow = cc.settings(profile, litrev=Path("/sk/scripts/litrev.py"), skill=Path("/sk"),
                            review=Path("/r/x"))["permissions"]["allow"]
        assert not {"Glob", "Grep"} & set(allow), profile
        assert {"Read(//r/x/**)", "Read(//sk/**)"} <= set(allow)


def test_webfetch_only_in_the_steps_that_fetch_pages():
    # WebFetch is the channel a prompt-injected session could send data out through. In 50 app sessions
    # (~/litrev-spike, 2026-10-21) only steps 3-5 used it: dataset homepages, eval.ai, checkpoint READMEs. Steps 1
    # (hundreds of outside abstracts) and 6 (whole papers), chat and refresh never did.
    kw = dict(litrev=Path("/sk/scripts/litrev.py"), skill=Path("/sk"), review=Path("/r/x"))
    assert "WebFetch" in cc.settings("step", web=True, **kw)["permissions"]["allow"]
    for profile in ("step", "chat_rw", "chat_ro"):
        assert "WebFetch" not in cc.settings(profile, **kw)["permissions"]["allow"], profile
    assert cc.WEB_STEPS == (3, 4, 5)


def test_chat_ro_settings_allow_read_only_litrev_commands_only():
    s = cc.settings("chat_ro", litrev=Path("/sk/scripts/litrev.py"), skill=Path("/sk"), review=Path("/r/x"))
    allow = s["permissions"]["allow"]
    assert "Bash(python3 /sk/scripts/litrev.py status *)" in allow
    assert not any(a.startswith("Edit") or a.startswith("Write") or a == "Agent" for a in allow)


def test_agents_come_from_the_skill_with_the_step5_model(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "litrev-details.md").write_text(
        "---\nname: litrev-details\ndescription: Step-5 extractor.\ntools: Read, Write, Bash\nmodel: opus\n---\n\nYou extract.\n")
    (tmp_path / "agents" / "litrev-reader.md").write_text(
        "---\nname: litrev-reader\ndescription: Reader.\ntools: Read, Write, Bash\nmodel: inherit\n---\n\nYou read.\n")
    a = cc.agents(tmp_path, step5_model="sonnet")
    assert a["litrev-details"] == {"description": "Step-5 extractor.", "prompt": "You extract.",
                                   "tools": ["Read", "Write", "Bash"], "model": "sonnet"}
    assert "model" not in a["litrev-reader"]
    assert cc.agents(tmp_path)["litrev-details"]["model"] == "opus"


def test_write_session_files_and_argv(tmp_path):
    skill = tmp_path / "sk"; (skill / "agents").mkdir(parents=True)
    f = cc.write_session_files(tmp_path / "s1", profile="step", litrev=skill / "scripts/litrev.py", skill=skill,
                               review=tmp_path / "rv", python=Path("/py"), sid="S1", wait_s=60, step5_model=None)
    mcp = json.loads(f.mcp.read_text())["mcpServers"]["litrev_app"]
    assert mcp["args"] == ["-m", "litrev_app.ask_mcp"] and mcp["env"]["LITREV_APP_SID"] == "S1"
    assert mcp["env"]["LITREV_APP_REVIEW"] == str(tmp_path / "rv") and mcp["env"]["LITREV_APP_WAIT_S"] == "60"
    argv = cc.step_argv(Path("/bin/claude"), "BRIEF", "S1", f, skill=skill, review=tmp_path / "rv",
                        litrev=skill / "scripts/litrev.py", model="opus", effort="medium")
    for flag in ("--output-format", "--verbose", "--no-session-persistence", "--strict-mcp-config", "--settings",
                 "--agents", "--mcp-config", "--append-subagent-system-prompt"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--model") + 1] == "opus" and argv[1:3] == ["-p", "BRIEF"]
    assert argv[argv.index("--effort") + 1] == "medium"
    assert "--effort" not in cc.step_argv(Path("/bin/claude"), "BRIEF", "S1", f, skill=skill, review=tmp_path / "rv",
                                          litrev=skill / "scripts/litrev.py")
    assert "--bare" not in argv and "--json-schema" not in argv
    assert str(tmp_path / "rv") in argv[argv.index("--append-subagent-system-prompt") + 1]


def test_step_settings_deny_loop_tools():
    # spike S2b: the main agent called ScheduleWakeup to wait for background subagents; their notifications
    # re-enter the agent on their own, and a wakeup loop only burns turns
    deny = cc.settings("step", litrev=Path("/sk/scripts/litrev.py"), skill=Path("/sk"), review=Path("/r/x"))["permissions"]["deny"]
    assert {"ScheduleWakeup", "CronCreate"} <= set(deny)


def test_child_env_disables_the_advisor_tool(tmp_path):
    # spike S2b: the main agent called the advisor server tool 11 times (full transcript, uncached, on another
    # model): $17 of a $33 step-0 session
    env = cc.child_env({}, litrev_home=tmp_path, venv_bin=Path("/v"), claude_dir=Path("/c"), concurrency=3)
    assert env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"


def test_subagent_rules_name_the_allowed_json_check():
    # live runs: subagents tried grep/plutil to validate their JSON; skill v1.19.2 adds `check-json` for it
    assert "check-json" in cc.SUBAGENT_RULES


def test_the_ask_server_outlasts_the_question_wait():
    """Live M2 run: Claude Code aborts an MCP call silent for its idle timeout (1800 s, the app's default wait too) and
    sent no progress token, so the app's heartbeats never went out; the server gets its own longer timeout."""
    srv = cc.mcp_config(Path("/py"), Path("/r"), "S1", 1800)["mcpServers"]["litrev_app"]
    assert srv["timeout"] >= (1800 + 600) * 1000


def test_background_subagents_get_more_than_ten_minutes():
    """Depth Anything (app run, 2026-10-12): in -p mode Claude Code stops background subagents 10 min after the main
    agent's last turn ("Background tasks still running 10m after the last turn ... stopping them"); three sessions lost
    step-5 extractions and step-1 rescreens and ended without a report."""
    env = cc.child_env({}, litrev_home=Path("/r"), venv_bin=Path("/v"), claude_dir=Path("/c"), concurrency=3)
    assert int(env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"]) >= 60 * 60 * 1000
