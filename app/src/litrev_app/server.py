"""The local web server: router, request, app object, core routes (auth, health, page, static files, settings,
global events) and the server.json single-instance file. Features add routes through `register(router)`."""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, config, security
from .briefs import STEP_TITLES
from .bus import GLOBAL, EventBus, sse_stream
from .locks import OutsideCache
from .orchestrator import Engine
from .scheduler import Conflict, Scheduler

FEATURES = ["litrev_app.api_reviews", "litrev_app.api_checkpoints", "litrev_app.doctor", "litrev_app.publish", "litrev_app.chat", "litrev_app.refresh", "litrev_app.usage", "litrev_app.update"]
STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_BODY = 2 * 1024 * 1024
log = logging.getLogger("litrev_app.server")

SIGNED_OUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Litrev</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:17px/1.55 "Avenir Next",-apple-system,system-ui,sans-serif;max-width:34rem;margin:14vh auto;padding:0 16px;color:#1c2733;background:#f2f4f1}
h1{font:600 1.7rem/1.2 "Iowan Old Style",Charter,Georgia,serif;margin:0 0 .6em}
@media (prefers-color-scheme:dark){body{color:#e4e8e3;background:#171c1a}}</style></head>
<body><h1>Open Litrev from the app</h1>
<p>This page needs the link the Litrev app opens for you. Double-click Litrev in your Applications folder, or run
litrev-app open in Terminal.</p><p>Links from an earlier launch stop working when Litrev restarts.</p></body></html>"""
SIGNED_OUT_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Request:
    def __init__(self, method: str, path: str, query: dict, headers, body: bytes):
        self.method, self.path, self.query, self.headers, self.body = method, path, query, headers, body
        self.json = None
        if body:
            try:
                self.json = json.loads(body)
            except ValueError:
                self.json = None


class Router:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        self.routes.append((method.upper(), re.compile(pattern), handler))

    def match(self, method: str, path: str):
        wrong_method = False
        for m, rx, h in self.routes:
            g = rx.fullmatch(path)
            if g:
                if m == method:
                    return h, g.groupdict()
                wrong_method = True
        if wrong_method:
            raise HTTPError(405, "Method not allowed")
        raise HTTPError(404, "Not found")


class App:
    def __init__(self, settings, cfg, problems, *, appdir, keychain, overrides, notify):
        self.settings, self.cfg, self.problems = settings, cfg, problems
        self.appdir, self.keychain, self._overrides = appdir, keychain, overrides or {}
        self.secrets = security.Secrets()
        self.port: int | None = None
        self.bus = EventBus()
        self.engine = Engine(cfg)
        kw = {"notify": notify} if notify else {}
        self.scheduler = Scheduler(self.engine, self.bus, ready=lambda: not self.problems, **kw)
        self.outside = OutsideCache()
        self.router = Router()

    def review(self, slug: str) -> Path:
        p = security.resolve_review(self.cfg.root, slug)
        if p is None:
            raise HTTPError(404, f"There is no review called {slug}")
        return p

    def require_ready(self) -> None:
        if self.problems:
            raise HTTPError(503, " ".join(self.problems))

    def reload(self, settings) -> None:
        """Apply new settings in place: the engine and scheduler hold `cfg`, so its fields change, not the object.
        A session already running keeps the settings it started with."""
        ov = self._overrides
        new, problems = config.build_config(settings, appdir=self.appdir, keychain=self.keychain,
                                            claude=ov.get("claude"), overrides=ov)
        for f in fields(new):
            setattr(self.cfg, f.name, getattr(new, f.name))
        self.settings, self.problems = settings, problems
        self.scheduler.wake()


def build_app(settings, *, appdir: Path = config.APPDIR, keychain=None, overrides: dict | None = None, notify=None,
              features: list[str] | None = None) -> App:
    keychain = keychain or config.Keychain()
    ov = overrides or {}
    cfg, problems = config.build_config(settings, appdir=appdir, keychain=keychain, claude=ov.get("claude"),
                                        overrides=ov)
    app = App(settings, cfg, problems, appdir=appdir, keychain=keychain, overrides=ov, notify=notify)
    core_routes(app.router)
    for name in FEATURES if features is None else features:
        importlib.import_module(name).register(app.router)
    return app


# ---- core routes ---------------------------------------------------------------------------------------------
def auth(app, req):
    if not security.same(req.query.get("t"), app.secrets.token):
        return 401, SIGNED_OUT, SIGNED_OUT_HEADERS
    # the csrf secret only in the fragment (never sent to a server, so never in GET /): app.js moves it to
    # localStorage, whose origin includes the port, unlike the cookie's (security F4)
    return 302, "", {"Location": f"/#k={app.secrets.csrf}",
                     "Set-Cookie": security.set_cookie(security.cookie_name(app.port), app.secrets.session)}


def health(app, req):
    out = {"app": "litrev-app", "version": __version__, "pid": os.getpid()}
    n = req.query.get("n")
    if n and len(n) <= security.MAX_NONCE:              # cli.alive's proof that this server holds the token (F5)
        out["proof"] = security.health_proof(app.secrets.token, n)
    return 200, out, {}


def index(app, req):
    page = (STATIC / "index.html").read_text()
    return 200, page.replace("{{version}}", __version__), {}          # no csrf secret here (security F4)


def static(app, req, name):
    p = STATIC / name
    if not p.is_file():
        raise HTTPError(404, "Not found")
    return 200, p.read_bytes(), {"Content-Type": STATIC_TYPES[p.suffix], "Cache-Control": "no-cache"}


def app_info(app, req):
    c = app.cfg
    return 200, {"version": __version__, "root": str(c.root), "skill": str(c.skill), "claude": str(c.claude),
                 "problems": app.problems, "port": app.port, "step_titles": STEP_TITLES}, {}


def _settings_view(app) -> dict:
    keys = app.cfg.keys or {}
    return {**asdict(app.settings), "keys": {short: bool(keys.get(env)) for short, env in config.KEY_ENV.items()}}


def get_settings(app, req):
    return 200, _settings_view(app), {}


def post_settings(app, req):
    d = req.json if isinstance(req.json, dict) else {}
    try:
        new = config.update(app.settings, d)
        for short, env in config.KEY_ENV.items():
            if d.get(f"clear_{short}_key"):
                app.keychain.delete(env)
            elif d.get(f"{short}_key"):
                app.keychain.set(env, str(d[f"{short}_key"]))
    except ValueError as e:
        raise HTTPError(422, str(e))
    except RuntimeError as e:
        raise HTTPError(500, str(e))
    config.save(new, app.appdir)
    app.reload(new)
    return 200, _settings_view(app), {}


def global_events(app, req):
    return 200, sse_stream(app.bus, GLOBAL, backlog=False), {}


def core_routes(router: Router) -> None:
    router.add("GET", r"/auth", auth)
    router.add("GET", r"/health", health)
    router.add("GET", r"/", index)
    router.add("GET", r"/static/(?P<name>[a-z0-9-]+\.(?:js|css|svg))", static)
    router.add("GET", r"/api/app", app_info)
    router.add("GET", r"/api/settings", get_settings)
    router.add("POST", r"/api/settings", post_settings)
    router.add("GET", r"/api/events", global_events)


# ---- HTTP plumbing -------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Litrev"
    sys_version = ""
    protocol_version = "HTTP/1.0"     # one request per connection; an SSE stream simply runs until it closes

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        app: App = self.server.app
        url = urlsplit(self.path)
        if method == "GET" and self.headers.get("Host") == f"localhost:{app.port}" and not url.path.startswith("/api/"):
            # pages move to the launcher's origin: the CSRF secret is in 127.0.0.1's localStorage (F4)
            return self._send(302, "", {"Location": f"http://127.0.0.1:{app.port}{self.path}"})
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        bad = security.check(method, url.path, self.headers, app.port, app.secrets, query)
        if bad:
            status, why = bad
            if status == 401 and not url.path.startswith("/api/"):
                return self._send(401, SIGNED_OUT, dict(SIGNED_OUT_HEADERS))
            return self._send(status, {"error": why}, {})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "Request too large"}, {})
        body = self.rfile.read(n) if n else b""
        req = Request(method, url.path, query, self.headers, body)
        try:
            handler, groups = app.router.match(method, url.path)
            status, out, extra = handler(app, req, **groups)
        except HTTPError as e:
            status, out, extra = e.status, {"error": e.message}, {}
        except Conflict as e:
            status, out, extra = 409, {"error": str(e)}, {}
        except Exception as e:
            log.exception("handler failed: %s %s", method, url.path)
            status, out, extra = 500, {"error": f"Litrev hit an internal error: {e}"}, {}
        self._send(status, out, extra or {})

    def _send(self, status: int, body, extra: dict) -> None:
        headers = dict(security.BASE_HEADERS)
        if "Content-Security-Policy" not in extra:
            headers.update(security.APP_HEADERS)
        headers.update(extra)
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode()
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(body, str):
            data = body.encode()
            headers.setdefault("Content-Type", "text/html; charset=utf-8")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            headers.setdefault("Content-Type", "application/octet-stream")
        elif body is None:
            data = b""
        elif hasattr(body, "__next__"):
            return self._stream(status, body, headers)
        else:
            raise TypeError(f"unsupported body type {type(body).__name__}")
        try:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self, status: int, body, headers: dict) -> None:
        headers.setdefault("Content-Type", "text/event-stream; charset=utf-8")
        try:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in body:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            close = getattr(body, "close", None)
            if close:
                close()


def bind(app: App, port: int = 8765, tries: int = 20) -> ThreadingHTTPServer:
    """127.0.0.1 only; the first free port from `port` (0 = any, for tests)."""
    last = None
    for p in ([0] if port == 0 else range(port, port + tries)):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError as e:
            last = e
            continue
        httpd.daemon_threads = True
        httpd.app = app
        app.port = httpd.server_address[1]
        return httpd
    raise OSError(f"No free port between {port} and {port + tries - 1}: {last}")


def write_server_json(appdir: Path, port: int, token: str) -> None:
    appdir.mkdir(parents=True, exist_ok=True)
    p, tmp = appdir / "server.json", appdir / "server.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "pid": os.getpid(), "token": token, "started_at": time.time()}, f)
    os.replace(tmp, p)


def read_server_json(appdir: Path) -> dict | None:
    try:
        d = json.loads((appdir / "server.json").read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and {"port", "pid", "token"} <= d.keys() else None


def remove_server_json(appdir: Path) -> None:
    d = read_server_json(appdir)
    if d and d.get("pid") == os.getpid():
        (appdir / "server.json").unlink(missing_ok=True)


def start_features(app, names=None) -> None:
    """Call each feature module's optional `on_start(app)` once the scheduler runs (M1c: resume waiting publishes)."""
    for name in FEATURES if names is None else names:
        try:
            hook = getattr(importlib.import_module(name), "on_start", None)
            if hook:
                hook(app)
        except Exception:
            log.exception("start hook of %s failed", name)


def serve(app: App, httpd: ThreadingHTTPServer) -> None:
    """Run until shut down: server.json for the launcher, the scheduler thread, then the HTTP loop."""
    write_server_json(app.appdir, app.port, app.secrets.token)
    app.scheduler.launch()
    start_features(app)
    try:
        httpd.serve_forever()
    finally:
        app.scheduler.shutdown()
        httpd.server_close()
        remove_server_json(app.appdir)
