"""litrev-app serve | open | stop: one local Litrev server per user (server.json), opened in the default browser.
The Litrev.app launcher (M1c) runs `litrev-app open`."""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config, security, server

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1 must never go to a proxy


def alive(info: dict | None) -> bool:
    """server.json names a live Litrev server of this user: its pid is ours and alive, and /health on its port answers
    with that pid and HMAC(token, a fresh nonce). Any local listener can echo a pid; only our server knows the token
    in the 0600 server.json (security F5)."""
    if not info or not info.get("token"):
        return False
    try:
        os.kill(int(info["pid"]), 0)
    except (ProcessLookupError, PermissionError):      # PermissionError: another user's process, so not our server
        return False
    except (KeyError, TypeError, ValueError):
        return False
    nonce = secrets.token_hex(16)
    try:
        with NO_PROXY.open(f"http://127.0.0.1:{int(info['port'])}/health?n={nonce}", timeout=2) as r:
            body = json.loads(r.read())
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(body, dict) or not isinstance(body.get("proof"), str):
        return False
    return (body.get("app") == "litrev-app" and body.get("pid") == info["pid"]
            and hmac.compare_digest(body["proof"].encode(), security.health_proof(str(info["token"]), nonce).encode()))


def auth_url(info: dict) -> str:
    return f"http://127.0.0.1:{info['port']}/auth?t={info['token']}"


def _appdir(args) -> Path:
    return Path(args.appdir).expanduser() if args.appdir else config.APPDIR


_SERVER_LOCK = None     # held for the server process's lifetime: one server per user (M1b review)


def _lock_held(appdir: Path) -> bool:
    """True when another process holds appdir/server.lock (a server is starting or running)."""
    import fcntl
    appdir.mkdir(parents=True, exist_ok=True)
    with open(appdir / "server.lock", "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def private_log(path: Path):
    """The server log, readable only by the user (it holds the launch link with its token)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, "ab")


def cmd_serve(args) -> int:
    global _SERVER_LOCK
    import fcntl
    appdir = _appdir(args)
    info = server.read_server_json(appdir)
    if alive(info):
        print(f"Litrev is already running: {auth_url(info)}")
        return 0
    appdir.mkdir(parents=True, exist_ok=True)
    fh = open(appdir / "server.lock", "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:          # server.json is written only after build_app, which can take a while
        fh.close()
        print("Litrev is already starting; run `litrev-app open` to get its link.")
        return 0
    _SERVER_LOCK = fh
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = server.build_app(config.load(appdir), appdir=appdir)
    httpd = server.bind(app, args.port)

    def stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    url = auth_url({"port": app.port, "token": app.secrets.token})
    print(f"Litrev is running: {url}", flush=True)
    for p in app.problems:
        print(f"Problem: {p}", flush=True)
    if args.open:
        subprocess.run(["/usr/bin/open", url])
    server.serve(app, httpd)
    print("Litrev stopped.", flush=True)
    return 0


def cmd_open(args) -> int:
    appdir = _appdir(args)
    info = server.read_server_json(appdir)
    if not alive(info):
        appdir.mkdir(parents=True, exist_ok=True)
        log = appdir / "server.log"
        starting = _lock_held(appdir)          # another `open` already started one: wait for it, don't spawn
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (pkg_parent, os.environ.get("PYTHONPATH")) if p)}
        if not starting:
            with private_log(log) as out:
                subprocess.Popen([sys.executable, "-m", "litrev_app.cli", "serve", "--appdir", str(appdir),
                                  "--port", str(args.port)], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                 env=env, start_new_session=True)
        deadline = time.time() + args.wait
        while time.time() < deadline:
            info = server.read_server_json(appdir)
            if alive(info):
                break
            time.sleep(0.2)
        else:
            print(f"Litrev did not start within {args.wait:.0f} s; see {log}", file=sys.stderr)
            return 1
    url = auth_url(info)
    if not args.no_browser:
        subprocess.run(["/usr/bin/open", url])
    print(url)
    return 0


def cmd_stop(args) -> int:
    appdir = _appdir(args)
    info = server.read_server_json(appdir)
    if not alive(info):
        (appdir / "server.json").unlink(missing_ok=True)
        print("Litrev is not running.")
        return 0
    os.kill(int(info["pid"]), signal.SIGTERM)
    deadline = time.time() + 15
    while alive(info) and time.time() < deadline:
        time.sleep(0.2)
    print("Litrev stopped. A step that was running keeps going; Litrev picks it up when it starts again.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="litrev-app", description="Run literature reviews from a local web page.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the Litrev server in this terminal")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--appdir")
    s.add_argument("--open", action="store_true", help="also open the page in the browser")
    s.set_defaults(func=cmd_serve)
    o = sub.add_parser("open", help="start Litrev if needed and open it in the browser")
    o.add_argument("--port", type=int, default=8765)
    o.add_argument("--appdir")
    o.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    o.add_argument("--wait", type=float, default=20.0)
    o.set_defaults(func=cmd_open)
    t = sub.add_parser("stop", help="stop the Litrev server")
    t.add_argument("--appdir")
    t.set_defaults(func=cmd_stop)
    return p


def main(argv: list[str] | None = None) -> int:
    from .commands import dispatch          # M1c: `litrev-app doctor` and `litrev-app sessions`
    handled = dispatch(list(sys.argv[1:] if argv is None else argv))
    if handled is not None:
        return handled
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
