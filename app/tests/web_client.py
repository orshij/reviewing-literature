"""Test helper: build an App against a temp root, run its server on a free port, talk to it like the browser."""
import http.client
import json
import os
import select
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from litrev_app import security, server
from litrev_app.config import Settings
from litrev_app.runner import Limits

SKILL = Path(os.environ.get("LITREV_SKILL", Path.home() / ".claude/skills/reviewing-literature"))
FAKE = Path(__file__).parent / "fake_claude.py"
FAST = Limits(init_s=10, idle_s=30, hard_s=120, poll_s=0.05, grace_int=2, grace_term=2)


class MemKeychain:
    def __init__(self):
        self.d = {}

    def get(self, env):
        return self.d.get(env)

    def set(self, env, value):
        self.d[env] = value

    def delete(self, env):
        self.d.pop(env, None)

    def keys(self):
        return dict(self.d)


def stub_skill(tmp_path: Path) -> Path:
    d = tmp_path / "stub-skill"
    d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text("stub")
    return d


def make_app(tmp_path: Path, *, skill: Path = SKILL, overrides=None, settings=None, features=None):
    s = settings or Settings(root=str(tmp_path / "root"), skill_override=str(skill))
    notes = []
    app = server.build_app(s, appdir=tmp_path / "appdir", keychain=MemKeychain(),
                           overrides={"claude": FAKE, "caffeinate": False, "limits": FAST, **(overrides or {})},
                           notify=lambda t, b: notes.append((t, b)), features=features)
    app.notes = notes
    return app


class Resp:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, {k.lower(): v for k, v in headers}, raw

    @property
    def text(self):
        return self.raw.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.raw)


class SSEReader:
    """Reads SSE events with a timeout. Holds the socket itself: for an HTTP/1.0 response http.client drops
    `conn.sock` as soon as the response starts."""

    def __init__(self, sock, resp):
        self.sock, self.resp, self.buf = sock, resp, b""

    def next_event(self, timeout=5.0):
        """The next `data:` event as a dict (comments and retry lines skipped), or None after `timeout`."""
        sock = self.sock
        while True:
            while b"\n\n" in self.buf:
                block, self.buf = self.buf.split(b"\n\n", 1)
                for line in block.decode().splitlines():
                    if line.startswith("data: "):
                        return json.loads(line[6:])
            if not select.select([sock], [], [], timeout)[0]:
                return None
            chunk = self.resp.read1(65536)
            if not chunk:
                return None
            self.buf += chunk

    def close(self):
        self.resp.close()
        self.sock.close()


class Client:
    def __init__(self, app):
        self.app, self.port = app, app.port
        self.host = f"127.0.0.1:{self.port}"
        self.cookie, self.csrf = self.sign_in()

    def sign_in(self):
        """Like the browser on the launcher's link: the cookie from /auth's Set-Cookie, the CSRF secret from its
        redirect's fragment only (security F4: GET / no longer carries it)."""
        r = self.get(f"/auth?t={self.app.secrets.token}", auth=False, csrf=False)
        assert r.status == 302, r.status
        cookie = r.headers["set-cookie"].split(";", 1)[0]
        assert cookie.startswith(f"{security.cookie_name(self.port)}=")
        return cookie, parse_qs(urlsplit(r.headers["location"]).fragment)["k"][0]

    def _open(self, method, path, body=None, *, auth=True, csrf=True, origin=True, host=None, headers=None):
        h = {"Host": host or self.host}
        if auth:
            h["Cookie"] = self.cookie
        if csrf:
            h["X-CSRF"] = self.csrf                       # every /api request, GET too (security F4)
        if method != "GET" and origin:
            h["Origin"] = f"http://{self.host}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            h["Content-Length"] = str(len(data))
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            if v is not None:
                c.putheader(k, v)
        c.endheaders(data)
        sock = c.sock
        return c, c.getresponse(), sock

    def request(self, method, path, body=None, **kw):
        c, r, _ = self._open(method, path, body, **kw)
        raw = r.read()
        c.close()
        return Resp(r.status, r.getheaders(), raw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def sse(self, path, **kw):
        c, r, sock = self._open("GET", path, **kw)
        assert r.status == 200, r.status
        return SSEReader(sock, r)


def start(app):
    httpd = server.bind(app, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Client(app)
