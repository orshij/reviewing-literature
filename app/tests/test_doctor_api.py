import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apphelpers import make_skill
from litrev_app import commands, doctor, proc
from litrev_app.doctor_checks import Check
from litrev_app.store import RunState, Store


class FakeRouter:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler):
        self.routes.append((method, re.compile(pattern), handler))

    def find(self, method, path):
        for m, rx, h in self.routes:
            g = rx.match(path) if m == method else None
            if g:
                return h, g.groupdict()
        return None, None


def app_for(tmp_path):
    cfg = SimpleNamespace(claude=None, skill=make_skill(tmp_path / "skill"), python=Path(sys.executable),
                          root=tmp_path / "root", appdir=tmp_path / "appdir")
    return SimpleNamespace(cfg=cfg)


CHECKS = [Check("claude", "Claude Code", "ok", "Claude Code 2.1.296"),
          Check("auth", "Claude login", "fail", "Not logged in", action="Log in"),
          Check("pwc", "Archived leaderboards (optional)", "warn", "cached", action="Build it")]


@pytest.fixture(autouse=True)
def fresh_cache():
    doctor.invalidate()
    yield
    doctor.invalidate()


def test_register_routes():
    r = FakeRouter()
    doctor.register(r)
    assert r.find("GET", "/api/doctor")[0] is doctor.get_doctor
    h, g = r.find("POST", "/api/doctor/auth/fix")
    assert h is doctor.post_fix and g == {"check_id": "auth"}
    assert r.find("POST", "/api/doctor/../fix")[0] is None


def test_get_doctor_caches_the_slow_checks(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(doctor, "run_checks", lambda inp, **kw: calls.append(1) or list(CHECKS))
    app = app_for(tmp_path)
    status, body, _ = doctor.get_doctor(app, SimpleNamespace(query={}))
    assert status == 200 and not body["ready"] and [c["id"] for c in body["checks"]] == ["claude", "auth", "pwc"]
    pwc = body["checks"][2]
    assert pwc["message"].startswith("Not built")                  # recomputed on every request, never cached
    doctor.get_doctor(app, SimpleNamespace(query={}))
    assert len(calls) == 1
    doctor.get_doctor(app, SimpleNamespace(query={"fresh": ["1"]}))
    assert len(calls) == 2


def test_post_fix(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    monkeypatch.setattr(doctor.doctor_fix, "fix", lambda cid, inp: {"started": True, "message": "opened"})
    assert doctor.post_fix(app, SimpleNamespace(json={}), check_id="auth")[0] == 202

    def nofix(cid, inp):
        raise KeyError(cid)
    monkeypatch.setattr(doctor.doctor_fix, "fix", nofix)
    status, body, _ = doctor.post_fix(app, SimpleNamespace(json={}), check_id="root")
    assert status == 400 and "no automatic fix" in body["error"]


def test_cli_doctor_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "default_inputs", lambda: None)
    monkeypatch.setattr(doctor, "run_checks", lambda inp, **kw: list(CHECKS))
    assert doctor.cli_main([]) == 1
    assert "FAIL" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "run_checks", lambda inp, **kw: [CHECKS[0], CHECKS[2]])
    assert doctor.cli_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)[1]["level"] == "warn"


def test_proc_lists_full_command_lines(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--settings",
                              str(tmp_path / "Application Support" / "LitrevApp" / "sessions" / "S1" / "settings.json")])
    try:
        procs = proc.list_processes()
        assert any(pid == os.getpid() for pid, _ in procs)
        mine = proc.app_sessions(tmp_path / "Application Support" / "LitrevApp", procs)
        assert child.pid in {pid for pid, _ in mine}                    # the space in the path survives
    finally:
        child.kill()
        child.wait()


def test_sessions(tmp_path):
    appdir = tmp_path / "Library" / "Application Support" / "LitrevApp"
    root = tmp_path / "root"
    step = (101, f"/x/claude -p brief --settings {appdir}/sessions/S1/settings.json --add-dir /sk")
    assert commands.sessions_main([], procs=lambda: [step, (102, "vim notes.txt")], appdir=appdir, root=root) == 3
    assert commands.sessions_main([], procs=lambda: [], appdir=appdir, root=root) == 0
    Store(root / "reviews" / "r1").save(RunState(phase="running"))
    assert commands.sessions_main([], procs=lambda: [], appdir=appdir, root=root) == 0     # app off: resumes later
    appdir.mkdir(parents=True)
    (appdir / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": 8765}))
    assert commands.sessions_main(["--json"], procs=lambda: [], appdir=appdir, root=root) == 3


def test_dispatch_leaves_other_commands_to_cli():
    assert commands.dispatch([]) is None and commands.dispatch(["serve"]) is None


def test_cli_hands_doctor_and_sessions_over(monkeypatch):
    from litrev_app import cli
    monkeypatch.setattr(commands, "sessions_main", lambda argv: 7)
    monkeypatch.setattr(sys, "argv", ["litrev-app", "sessions"])
    assert cli.main() == 7


def test_server_registers_the_doctor():
    from litrev_app import server
    assert "litrev_app.doctor" in server.FEATURES
