import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from m2help import FakeRouter, make_app
from litrev_app import locate, update
from litrev_app.store import RunState, Store


def _clone(tmp_path) -> Path:
    loc = tmp_path / "marketplaces" / "litrev"
    (loc / "app").mkdir(parents=True)
    (loc / "app" / "pyproject.toml").write_text("[project]\nname = 'litrev-app'\n")
    return loc


def _runner(listing, fail=None):
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[1:4] == ["plugin", "marketplace", "list"]:
            return subprocess.CompletedProcess(argv, 0, listing, "")
        code = 1 if fail and fail in " ".join(argv) else 0
        return subprocess.CompletedProcess(argv, code, f"ran {' '.join(argv[1:3])}", "boom" if code else "")
    return run, calls


def test_install_location(tmp_path):
    loc = _clone(tmp_path)
    good = json.dumps([{"name": "claude-plugins-official", "installLocation": "/x"},
                       {"name": "litrev", "installLocation": str(loc)}])
    assert update.install_location(good) == loc
    assert update.install_location("[]") is None and update.install_location("not json") is None


def test_update_runs_the_three_steps_in_order_and_thaws(tmp_path, monkeypatch):
    loc = _clone(tmp_path)
    app = make_app(tmp_path)
    old = tmp_path / "plugins" / "cache" / "v1" / "skills" / "reviewing-literature"
    new = tmp_path / "plugins" / "cache" / "v2" / "skills" / "reviewing-literature"
    app.cfg.skill = old
    monkeypatch.setattr(locate, "find_skill", lambda claude, override=None, home=None: new)
    run, calls = _runner(json.dumps([{"name": "litrev", "installLocation": str(loc)}]))
    code, body = update.run_update(app, run=run, uv=Path("/u/uv"))
    assert code == 200 and body["ok"] and "restart" in body["restart"].lower()
    assert [c[1:4] for c in calls] == [["plugin", "marketplace", "list"], ["plugin", "marketplace", "update"],
                                       ["plugin", "update", locate.PLUGIN_ID], ["tool", "install", "--force"]]
    assert calls[1][4] == "litrev" and calls[3][-1] == str(loc / "app") and "-y" not in calls[2]
    assert app.cfg.skill == new and app.engine.cfg.skill == new and not app.engine.writers.frozen


def test_a_failing_step_stops_the_rest(tmp_path):
    loc = _clone(tmp_path)
    app = make_app(tmp_path)
    run, calls = _runner(json.dumps([{"name": "litrev", "installLocation": str(loc)}]),
                         fail="plugin update")
    code, body = update.run_update(app, run=run, uv=Path("/u/uv"))
    assert code == 200 and not body["ok"] and "Update the plugin" in body["error"]
    assert len(calls) == 3 and body["steps"][-1]["exit"] == 1 and not app.engine.writers.frozen


def test_no_marketplace_means_nothing_to_update(tmp_path):
    app = make_app(tmp_path)
    run, calls = _runner("[]")
    code, body = update.run_update(app, run=run, uv=Path("/u/uv"))
    assert code == 200 and not body["ok"] and "marketplace" in body["error"] and len(calls) == 1


def test_refused_while_any_session_runs(tmp_path):
    app = make_app(tmp_path)
    run, calls = _runner("[]")
    app.engine.live[str(tmp_path / "reviews" / "a")] = object()
    assert update.run_update(app, run=run, uv=Path("/u/uv"))[0] == 409
    app.engine.live.clear()
    app.chat_service = SimpleNamespace(running_keys=lambda: [str(tmp_path / "reviews" / "b")])
    code, body = update.run_update(app, run=run, uv=Path("/u/uv"))
    assert code == 409 and "chat message on b" in body["error"]
    del app.chat_service
    app.engine.writers.start_reading()
    assert update.run_update(app, run=run, uv=Path("/u/uv"))[0] == 409
    app.engine.writers.stop_reading()
    r = tmp_path / "reviews" / "c"
    Store(r).save(RunState(step=3, phase="running", pid=os.getpid()))
    code, body = update.run_update(app, run=run, uv=Path("/u/uv"))
    assert code == 409 and "session on c" in body["error"] and calls == []


def test_route_and_feature(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(update, "run_update", lambda app: (200, {"ok": True, "steps": []}))
    monkeypatch.setattr(update, "restart_soon", lambda app, **kw: None)     # a real one would stop pytest
    rt = FakeRouter()
    update.register(rt)
    assert rt.call(app, "POST", "/api/update", {}) == (200, {"ok": True, "steps": []}, {})
    from litrev_app import server
    assert "litrev_app.update" in server.FEATURES


def test_update_reinstalls_and_restarts_litrev(tmp_path, monkeypatch):
    """M2 review: `uv tool install --force` keeps the old build when the version is unchanged, and the Update
    button said "quit it and open it again" with no way to quit (the launcher reuses the running server)."""
    loc = _clone(tmp_path)
    app = make_app(tmp_path)
    run, calls = _runner(json.dumps([{"name": "litrev", "installLocation": str(loc)}]))
    code, body = update.run_update(app, run=run, uv=Path("/u/uv"))
    assert code == 200 and body["ok"] and "--reinstall" in calls[3] and "new" in body["restart"].lower()
    spawned, killed = [], []
    monkeypatch.setattr(update.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)))
    monkeypatch.setattr(update.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    update.restart_soon(app, delay=0).join(5)
    (argv, kw), = spawned
    script = argv[-1]
    assert argv[:2] == ["/bin/sh", "-c"] and f"kill -0 {os.getpid()}" in script
    assert "litrev_app.cli open" in script and str(app.cfg.appdir) in script and kw.get("start_new_session")
    assert killed == [(os.getpid(), update.signal.SIGTERM)]


def test_post_update_restarts_only_after_a_successful_update(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = []
    monkeypatch.setattr(update, "restart_soon", lambda a, **kw: calls.append(a))
    monkeypatch.setattr(update, "run_update", lambda a: (200, {"ok": False}))
    update.post_update(app, None)
    monkeypatch.setattr(update, "run_update", lambda a: (200, {"ok": True}))
    update.post_update(app, None)
    assert calls == [app]
