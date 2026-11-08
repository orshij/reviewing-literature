"""The Update button (M2): update the plugin marketplace clone, then the plugin, then reinstall the app from that
clone (`claude plugin marketplace add` clones the whole repo, app/ included). Refused while any session runs: a
plugin update replaces the skill folder that sessions read from. Never passes -y: a marketplace-declared command
must be shown to the person, so an update that needs one fails here with its message."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import locate
from .store import Store
from .writers import pid_alive

MARKETPLACE = locate.PLUGIN_ID.split("@", 1)[1]
STEP_TIMEOUT_S = 600


def find_uv(env_path: str | None = None) -> Path | None:
    dirs = (env_path if env_path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    dirs += [os.path.expanduser(d) for d in locate.EXTRA_BIN]
    hit = shutil.which("uv", path=os.pathsep.join(d for d in dirs if d))
    return Path(hit) if hit else None


def install_location(listing: str) -> Path | None:
    """The marketplace clone from `claude plugin marketplace list --json`, if it holds the app."""
    try:
        items = json.loads(listing)
    except ValueError:
        return None
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("name") == MARKETPLACE and it.get("installLocation"):
            p = Path(it["installLocation"])
            return p if (p / "app" / "pyproject.toml").exists() else None
    return None


def running_sessions(app) -> list[str]:
    busy = [f"a session on {Path(k).name}" for k in list(app.engine.live)]
    for attr, label in (("chat_service", "a chat message"), ("refresh_service", "a refresh")):
        svc = getattr(app, attr, None)
        if svc is not None:
            busy += [f"{label} on {Path(k).name}" for k in svc.running_keys()]
    reviews = Path(app.cfg.root) / "reviews"
    if reviews.is_dir():
        for state in sorted(reviews.glob("*/cache/app/state.json")):
            r = state.parent.parent.parent
            st = Store(r).load()
            if st.phase == "running" and pid_alive(st.pid) and str(r) not in app.engine.live:
                busy.append(f"a session on {r.name}")
    return busy


def run_update(app, *, run=subprocess.run, uv: Path | None = None) -> tuple[int, dict]:
    busy = running_sessions(app)
    if busy:
        return 409, {"ok": False, "error": f"A session is running ({', '.join(busy)}). Update when it finishes."}
    why = app.engine.writers.freeze()
    if why:
        return 409, {"ok": False, "error": f"Busy: {why}. Update when it finishes."}
    steps: list[dict] = []
    try:
        claude, env = str(app.cfg.claude), app.engine.env()
        uv = uv or find_uv()

        def step(title: str, argv: list[str]) -> dict:
            try:
                p = run(argv, capture_output=True, text=True, timeout=STEP_TIMEOUT_S, env=env,
                        stdin=subprocess.DEVNULL)
                rec = {"title": title, "argv": argv, "exit": p.returncode, "stdout": p.stdout or "",
                       "output": ((p.stdout or "") + (p.stderr or ""))[-4000:]}
            except (OSError, subprocess.TimeoutExpired) as e:
                rec = {"title": title, "argv": argv, "exit": -1, "stdout": "", "output": str(e)}
            steps.append(rec)
            return rec

        listing = step("Find the plugin's marketplace", [claude, "plugin", "marketplace", "list", "--json"])
        loc = install_location(listing["stdout"]) if listing["exit"] == 0 else None
        if loc is None:
            return 200, {"ok": False, "steps": steps, "error": (
                f"The {MARKETPLACE} marketplace (with the app) isn't installed on this Mac; this copy of the skill "
                "is updated the way it was installed.")}
        for title, argv in (("Update the marketplace", [claude, "plugin", "marketplace", "update", MARKETPLACE]),
                            ("Update the plugin", [claude, "plugin", "update", locate.PLUGIN_ID])):
            if step(title, argv)["exit"] != 0:
                return 200, {"ok": False, "steps": steps, "error": f"{title} failed; see its output."}
        if uv is None:
            steps.append({"title": "Reinstall the app", "argv": [], "exit": -1, "stdout": "",
                          "output": "uv not found (install.sh installs it)"})
            return 200, {"ok": False, "steps": steps, "error": "Reinstall the app failed: uv not found."}
        if step("Reinstall the app", [str(uv), "tool", "install", "--force", "--reinstall", "--python", "3.12",
                                      str(loc / "app")])["exit"] != 0:      # --reinstall: same version, new build
            return 200, {"ok": False, "steps": steps, "error": "Reinstall the app failed; see its output."}
        if "/plugins/" in str(app.cfg.skill):            # the plugin's skill moved to the new version's folder
            new = locate.find_skill(app.cfg.claude)
            if new:
                app.cfg.skill = new
                app.engine.cfg.skill = new
        return 200, {"ok": True, "steps": steps, "skill": str(app.cfg.skill),
                     "restart": "Litrev restarts with the new version and opens in a new browser tab; you can "
                                "close this one."}
    finally:
        app.engine.writers.thaw()
        for s in steps:
            s.pop("stdout", None)


def restart_soon(app, *, delay: float = 1.0) -> threading.Thread:
    """After an update: a detached shell waits for this server to exit and starts the new build with `open`; then
    this server stops itself (SIGTERM runs the normal shutdown). The launcher reuses a running server, so nothing
    else would ever load the new code (M2 review)."""
    appdir = Path(getattr(app, "appdir", None) or app.cfg.appdir)
    port = getattr(app, "port", None) or 8765
    script = (f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done; exec {shlex.quote(sys.executable)} "
              f"-m litrev_app.cli open --appdir {shlex.quote(str(appdir))} --port {int(port)}")
    subprocess.Popen(["/bin/sh", "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    t = threading.Timer(delay, os.kill, (os.getpid(), signal.SIGTERM))   # after the reply has gone out
    t.daemon = True
    t.start()
    return t


def post_update(app, req):
    code, body = run_update(app)
    if body.get("ok"):
        restart_soon(app)
    return code, body, {}


def register(router) -> None:
    router.add("POST", r"^/api/update$", post_update)
