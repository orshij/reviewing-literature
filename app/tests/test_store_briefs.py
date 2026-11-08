import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from litrev_app.briefs import STEP_TITLES, step_brief
from litrev_app.litrev_cli import Litrev, init_review
from litrev_app.store import RunState, Store

SKILL = Path(os.environ.get("LITREV_SKILL", Path.home() / ".claude/skills/reviewing-literature"))


def test_store_roundtrip_and_blocked_is_taken_once(tmp_path):
    s = Store(tmp_path)
    assert s.load() == RunState()
    st = RunState(step=2, phase="paused", paused_until=5.0, answers=["scope: x"])
    s.save(st)
    assert Store(tmp_path).load() == st
    s.log_session({"sid": "a"}); s.log_session({"sid": "b"})
    assert [r["sid"] for r in s.sessions()] == ["a", "b"]
    (s.dir / "blocked.json").write_text(json.dumps({"reason": "OpenAlex credits"}))
    assert s.take_blocked("S1")["reason"] == "OpenAlex credits" and s.take_blocked("S2") is None
    (s.dir / "report-S1.json").write_text(json.dumps({"outcome": "done"}))
    assert s.report("S1") == {"outcome": "done"} and s.report("S9") is None


def test_step_brief_carries_app_mode_and_context():
    b = step_brief(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), step=1, sid="S1", parallel=3,
                   new={"seed": "Depth Anything", "purpose": "p"}, cont="handoff after hop 2", answers=["scope: in a"])
    assert "/sk/app-mode.md" in b and "/sk/SKILL.md" in b and "step 1 (" + STEP_TITLES[1] + ")" in b
    assert "python3 /sk/scripts/litrev.py <cmd> --review /r/x" in b and "at most 3" in b
    assert 'seed "Depth Anything"' in b and "handoff after hop 2" in b and "scope: in a" in b
    assert "/r/x/cache/app/report-S1.json" in b
    plain = step_brief(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), step=4, sid="S2", parallel=3)
    assert "seed" not in plain and "continues" not in plain and "Answered" not in plain


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_litrev_init_status_and_lock_retry(tmp_path):
    env = {**os.environ, "LITREV_HOME": str(tmp_path)}
    r = tmp_path / "reviews" / "t"
    assert init_review(Path(sys.executable), SKILL, r, slug="t", purpose="p", seed="s", cap=12, years=3, env=env).returncode == 0
    lit = Litrev(Path(sys.executable), SKILL, r, env, sleep=lambda s: None, lock_retries=2)
    st = lit.status()
    assert st["rows"][0]["key"] == "scope" and st["skill_version"]
    assert lit.meta()["caps"]["max_papers"] == 12
    import fcntl
    with open(r / "cache" / "litrev.lock", "a+") as fh:          # hold the review's write lock
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write("test pid 1"); fh.flush()
        p = lit.run(["set-meta", "--field", "x", "--json", "1"])
    assert p.returncode != 0 and "locked by" in p.stderr
    assert lit.run(["set-meta", "--field", "x", "--json", "1"]).returncode == 0


def test_atomic_writes_from_many_threads_never_collide(tmp_path):
    # M1b writes state.json from HTTP threads and the engine thread at once; a pid-only temp name collides
    import threading
    from litrev_app.ask_mcp import read_json, write_json_atomic
    errors = []
    def w(i):
        try:
            for _ in range(50):
                write_json_atomic(tmp_path / "state.json", {"i": i})
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors and read_json(tmp_path / "state.json")["i"] in range(8)


def test_step_brief_parks_when_the_question_tool_fails(tmp_path):
    """Live M2 run: ask_user failed with Claude Code's MCP timeout and the session decided the question itself.
    Since v1.22.1 only the scope is asked (KPIs etc. are Claude's own calls by design); a failed scope ask parks."""
    from litrev_app.briefs import step_brief
    b = step_brief(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), step=6, sid="S1", parallel=3)
    assert "ask_user fails" in b and "never decide it yourself" in b


def test_step_brief_asks_only_the_scope():
    """The user (2026-10-12): fewer questions in the app; every call but the scope is Claude's, recorded by agent."""
    b = step_brief(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), step=6, sid="S1", parallel=3)
    assert "only the scope" in b and "--by agent" in b and "notes_for_user" in b
