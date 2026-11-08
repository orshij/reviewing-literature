import pytest
import json
from pathlib import Path

from litrev_app import briefs, claude_cmd as cc

L = Path("/sk/scripts/litrev.py")


def test_chat_ro_allows_the_read_only_forms_only():
    allow = cc.settings("chat_ro", litrev=L, skill=Path("/sk"), review=Path("/r/x"))["permissions"]["allow"]
    for c in ("status", "show", "why-missing", "kpi --suggest", "kpi --list", "select --preview"):
        assert f"Bash(python3 {L} {c} *)" in allow
    assert not any(a.startswith(("Edit", "Write")) or a == "Agent" or a == f"Bash(python3 {L} *)" for a in allow)
    rw = cc.settings("chat_rw", litrev=L, skill=Path("/sk"), review=Path("/r/x"))["permissions"]["allow"]
    assert f"Bash(python3 {L} *)" in rw and "Edit(//r/x/**)" in rw


def test_chat_files_have_no_mcp_server(tmp_path):
    (tmp_path / "sk" / "agents").mkdir(parents=True)
    f = cc.write_chat_files(tmp_path / "c", profile="chat_ro", litrev=tmp_path / "sk/scripts/litrev.py",
                            skill=tmp_path / "sk", review=tmp_path / "rv", step5_model=None)
    assert json.loads(f.mcp.read_text()) == {"mcpServers": {}}
    assert json.loads(f.settings.read_text())["disableAllHooks"] is True
    assert json.loads(f.agents.read_text()) == {}


def test_chat_argv_first_message_then_resume(tmp_path):
    f = cc.SessionFiles(tmp_path / "s.json", tmp_path / "a.json", tmp_path / "m.json")
    kw = dict(skill=Path("/sk"), review=Path("/r/x"), litrev=L, system_prompt="SYS")
    first = cc.chat_argv(Path("/bin/claude"), "Why isn't P7 here?", "S1", f, resume=False, **kw)
    again = cc.chat_argv(Path("/bin/claude"), "Add it", "S1", f, resume=True, model="opus", effort="low", **kw)
    assert first[1:3] == ["-p", "Why isn't P7 here?"] and first[first.index("--session-id") + 1] == "S1"
    assert again[again.index("--resume") + 1] == "S1" and "--session-id" not in again
    for a in (first, again):
        assert "--no-session-persistence" not in a and "--include-partial-messages" in a
        assert "--strict-mcp-config" in a and a[a.index("--permission-mode") + 1] == "dontAsk"
        assert a[a.index("--permission-prompts") + 1] == "none" and a[a.index("--append-system-prompt") + 1] == "SYS"
        assert "--bare" not in a and "--json-schema" not in a
    assert again[again.index("--model") + 1] == "opus" and again[again.index("--effort") + 1] == "low"
    assert "--effort" not in first
    dash = cc.chat_argv(Path("/bin/claude"), "--help me", "S1", f, resume=True, **kw)
    assert dash[2] == " --help me"          # a message starting with '-' is never read as a flag


def test_chat_prompt_modes():
    rw = briefs.chat_prompt(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), read_only=False)
    ro = briefs.chat_prompt(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), read_only=True)
    assert "/sk/SKILL.md" in rw and "python3 /sk/scripts/litrev.py <cmd> --review /r/x" in rw
    assert "RUN FROM STEP <n>" in rw and "Never publish" in rw and "Do not follow /sk/app-mode.md" in rw
    assert "read-write" in rw and "Mode: read-only" in ro and "read-write" not in ro
    # fewer questions (2026-10-12): the chat acts and says what it chose; at the cap it raises it for the request
    assert "ask before raising" not in rw and "say in one line what you chose" in rw and "--by agent" in rw


def test_app_status_tells_chat_what_runs_now():
    """Live M2 check: asked "what is the review doing right now?" during step 4, chat answered "nothing is
    running". Each message now carries the app's own run state."""
    from datetime import datetime
    from litrev_app.store import RunState
    s = briefs.app_status(RunState(step=4, phase="running", sid="S", message="refresh 2026-10-11: steps 4-7 for P818"),
                          busy="a session is running on this review")
    assert "step 4 is running" in s and "refresh 2026-10-11: steps 4-7 for P818" in s and "trust it" in s
    assert "step 5 starts next" in briefs.app_status(RunState(step=5, phase="running"), busy="the review is running its steps")
    w = briefs.app_status(RunState(step=1, phase="waiting_user", message="Which areas are in scope?"), busy=None)
    assert "waiting for the teammate" in w and "Which areas are in scope?" in w
    until = datetime(2026, 10, 11, 20, 40).timestamp()
    p = briefs.app_status(RunState(step=5, phase="paused", paused_until=until, message="Paused: Claude usage limit"), busy=None)
    assert "paused until 20:40" in p and "Claude usage limit" in p
    assert "failed" in briefs.app_status(RunState(step=3, phase="failed", message="No progress"), busy=None)
    assert "every step is done" in briefs.app_status(RunState(step=7, phase="done"), busy=None)
    assert "Nothing is running" in briefs.app_status(RunState(step=2, phase="idle"), busy=None)
    assert "refresh" in briefs.app_status(RunState(step=7, phase="done"), busy="a refresh is writing this review")
    prompt = briefs.chat_prompt(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), read_only=True, status=s)
    assert prompt.rstrip().endswith(s)


def test_chat_cost_is_the_difference_of_the_running_totals():
    """M2 review: S10 found total_cost_usd cumulative across --resume runs (live chat: 0.64, 1.13, 1.84, 2.51 in one
    conversation); the app showed each running total as that message's cost."""
    from litrev_app import chat
    assert chat.message_cost(1.134754, 0.642538) == pytest.approx(0.492216)
    assert chat.message_cost(0.3, 1.2) == pytest.approx(0.3)          # smaller total: a fresh session


def test_no_step1_detour_note_for_a_run_that_starts_at_step_1():
    """M2 review: RUN FROM STEP 1 got "do not run select, snowball or coverage", step 1's own work."""
    assert briefs.rerun_note(1, "a change made in the Ask Claude chat") == ""
    assert "select" in briefs.rerun_note(2, "x")


def test_chat_status_names_a_running_refresh(tmp_path):
    """M2 review: during a refresh's screening session the chat was told "Nothing is running" (the refresh
    session is in engine.live, so review_busy never said "refresh")."""
    from types import SimpleNamespace
    from litrev_app import chat
    from litrev_app.store import RunState, Store
    from litrev_app.writers import WriterLocks
    r = tmp_path / "reviews" / "t"
    Store(r).save(RunState(step=7, phase="done"))
    eng = SimpleNamespace(writers=WriterLocks(), live={str(r): object()})
    assert eng.writers.acquire(str(r), "refresh")
    assert "a refresh is checking for new papers" in chat.status_line(eng, r)
    eng.writers.release(str(r), "refresh")
    eng.live.clear()
    assert "every step is done" in chat.status_line(eng, r)
