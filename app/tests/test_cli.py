import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from litrev_app import cli, config, server
from web_client import make_app, start, stub_skill

SRC = Path(__file__).resolve().parents[1] / "src"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_cli(*args, timeout=40):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run([sys.executable, "-m", "litrev_app.cli", *args], capture_output=True, text=True,
                          env=env, timeout=timeout)


def test_alive_rejects_stale_server_json(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert cli.alive({"pid": dead.pid, "port": free_port(), "token": "t"}) is False
    assert cli.alive({"pid": os.getpid(), "port": free_port(), "token": "t"}) is False      # nothing listening
    assert cli.alive(None) is False


def fake_health(answer):
    """A 127.0.0.1 listener that answers /health like Litrev (another local user's impostor, security F5)."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(answer(self.path)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_alive_needs_the_token_proof_not_an_echoed_pid():
    # security F5: any local listener could echo {"app": "litrev-app", "pid": <pid>} and get the browser + token
    seen = []
    echo = fake_health(lambda path: seen.append(path) or {"app": "litrev-app", "version": "9", "pid": os.getpid()})
    wrong = fake_health(lambda path: {"app": "litrev-app", "pid": os.getpid(), "proof": "0" * 64})
    odd = fake_health(lambda path: ["litrev-app", os.getpid()])
    try:
        for httpd in (echo, wrong, odd):
            assert cli.alive({"pid": os.getpid(), "port": httpd.server_address[1], "token": "t"}) is False
        nonces = [path.partition("?n=")[2] for path in seen]
        assert len(nonces) == 1 and len(nonces[0]) == 32                  # a fresh secrets.token_hex(16)
    finally:
        for httpd in (echo, wrong, odd):
            httpd.shutdown(); httpd.server_close()


def test_alive_rejects_another_users_pid(monkeypatch):
    # security F5: PermissionError from kill(pid, 0) means another user's process, so never this user's server
    def proof(path):
        n = path.partition("?n=")[2]
        return {"app": "litrev-app", "pid": 1, "proof": hmac.new(b"t", n.encode(), hashlib.sha256).hexdigest()}

    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    httpd = fake_health(proof)
    try:
        info = {"pid": 1, "port": httpd.server_address[1], "token": "t"}
        monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
        assert cli.alive(info) is True                                     # the fake does answer the proof
        monkeypatch.setattr(cli.os, "kill", kill)
        assert cli.alive(info) is False
    finally:
        httpd.shutdown(); httpd.server_close()


def test_alive_accepts_the_real_server_only_with_its_token(tmp_path):
    app = make_app(tmp_path, skill=stub_skill(tmp_path), features=[])
    httpd, _ = start(app)
    try:
        info = {"pid": os.getpid(), "port": app.port, "token": app.secrets.token}
        assert cli.alive(info) is True
        assert cli.alive({**info, "token": "not-the-token"}) is False
        assert cli.alive({**info, "token": ""}) is False
        assert cli.alive({k: v for k, v in info.items() if k != "token"}) is False      # a server.json without one
    finally:
        httpd.shutdown(); httpd.server_close()


def test_open_starts_one_server_and_stop_ends_it(tmp_path):
    appdir = tmp_path / "appdir"
    config.save(config.Settings(root=str(tmp_path / "root"), skill_override=str(stub_skill(tmp_path))), appdir)
    port = free_port()
    first = run_cli("open", "--appdir", str(appdir), "--port", str(port), "--no-browser")
    assert first.returncode == 0, first.stderr
    info = server.read_server_json(appdir)
    try:
        assert (appdir / "server.json").stat().st_mode & 0o777 == 0o600
        assert info["port"] == port and cli.alive(info)
        assert first.stdout.strip().endswith(cli.auth_url(info))
        again = run_cli("open", "--appdir", str(appdir), "--port", str(port), "--no-browser")
        assert again.returncode == 0 and server.read_server_json(appdir)["pid"] == info["pid"]
        serve = run_cli("serve", "--appdir", str(appdir), "--port", str(port), timeout=20)
        assert serve.returncode == 0 and "already running" in serve.stdout
    finally:
        stop = run_cli("stop", "--appdir", str(appdir))
    assert stop.returncode == 0, stop.stderr
    deadline = time.time() + 10
    while cli.alive(info) and time.time() < deadline:
        time.sleep(0.2)
    assert not cli.alive(info) and not (appdir / "server.json").exists()
    assert "Litrev is running" in (appdir / "server.log").read_text()


def test_stop_when_nothing_runs(tmp_path):
    r = run_cli("stop", "--appdir", str(tmp_path))
    assert r.returncode == 0 and "not running" in r.stdout


def test_pyproject_installs_the_command_and_the_page():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((SRC.parent / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["litrev-app"] == "litrev_app.cli:main"
    assert data["project"]["scripts"]["litrev-app-mcp"] == "litrev_app.ask_mcp:main"
    assert "static/*" in data["tool"]["setuptools"]["package-data"]["litrev_app"]


def test_a_second_server_never_starts_while_one_is_starting(tmp_path):
    # M1b review: server.json is written only after build_app (which can take 30 s); two servers could both run
    import fcntl
    from litrev_app import cli
    appdir = tmp_path / "appdir"; appdir.mkdir()
    with open(appdir / "server.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)        # "another server is starting"
        args = cli.build_parser().parse_args(["serve", "--appdir", str(appdir), "--port", "0"])
        assert cli.cmd_serve(args) == 0 and not (appdir / "server.json").exists()
        args = cli.build_parser().parse_args(["open", "--appdir", str(appdir), "--no-browser", "--wait", "0.5"])
        assert cli.cmd_open(args) == 1                       # waited for the starting one; spawned nothing
    assert not (appdir / "server.log").exists()


def test_server_log_is_private(tmp_path):
    # M1b review: server.log held the launch link (with its token) and was created 0644
    from litrev_app import cli
    appdir = tmp_path / "appdir"; appdir.mkdir()
    (appdir / "server.log").write_text("old")
    (appdir / "server.log").chmod(0o644)
    cli.private_log(appdir / "server.log").close()
    assert (appdir / "server.log").stat().st_mode & 0o077 == 0
