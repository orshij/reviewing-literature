import hashlib
import hmac
import json
from pathlib import Path

import pytest

from litrev_app import security, server
from web_client import Client, make_app, start, stub_skill


@pytest.fixture
def web(tmp_path):
    app = make_app(tmp_path, skill=stub_skill(tmp_path), features=[])
    httpd, client = start(app)
    yield app, client
    httpd.shutdown()
    httpd.server_close()


def test_wrong_host_is_403(web):
    app, c = web
    for host in ("evil.example:%d" % app.port, "127.0.0.1:1", "localhost", "[::1]:%d" % app.port):
        assert c.get("/api/app", host=host).status == 403
    assert c.get("/health", host="evil.example", auth=False).status == 403
    assert c.get("/api/app", host=f"localhost:{app.port}").status == 200


def test_no_cookie_is_401(web):
    app, c = web
    r = c.get("/api/app", auth=False)
    assert r.status == 401 and r.json()["error"]
    page = c.get("/", auth=False)
    assert page.status == 401 and "litrev-app open" in page.text and "script" not in page.text
    assert c.get("/api/app", auth=False, headers={"Cookie": f"{security.cookie_name(app.port)}=guess"}).status == 401


def test_post_needs_matching_origin_and_csrf(web):
    app, c = web
    assert c.post("/api/settings", {"parallel": 2}, origin=False).status == 403
    assert c.post("/api/settings", {"parallel": 2}, headers={"Origin": "http://evil.example"}).status == 403
    assert c.post("/api/settings", {"parallel": 2}, csrf=False).status == 403
    assert c.post("/api/settings", {"parallel": 2}, headers={"X-CSRF": "nope"}).status == 403
    assert c.post("/api/settings", {"parallel": 2}).status == 200


def test_auth_sets_a_strict_http_only_cookie_and_redirects(web):
    app, c = web
    assert c.get("/auth?t=wrong", auth=False).status == 401
    r = c.get(f"/auth?t={app.secrets.token}", auth=False)
    assert r.status == 302 and r.headers["location"] == f"/#k={app.secrets.csrf}"     # the fragment never reaches a server
    ck = r.headers["set-cookie"]
    assert ck.startswith(f"{security.cookie_name(app.port)}={app.secrets.session};")
    assert "HttpOnly" in ck and "SameSite=Strict" in ck and "Path=/" in ck
    assert app.secrets.token != app.secrets.session != app.secrets.csrf


def test_api_reads_need_the_csrf_secret_too(web):
    # security F4: cookies ignore ports, so another loopback listener can get the cookie; alone it reads nothing
    app, c = web
    for path in ("/api/app", "/api/settings", "/api/events"):
        for kw in ({"csrf": False}, {"headers": {"X-CSRF": "nope"}}):
            r = c.get(path, **kw)
            assert (r.status, r.json()) == (401, {"error": "Not signed in: open Litrev from the app"}), (path, kw)
    assert c.get("/api/app").status == 200
    page = c.get("/", csrf=False)
    assert page.status == 200 and app.secrets.csrf not in page.text
    assert c.get("/static/app.js", csrf=False).status == 200 and c.get("/health", auth=False, csrf=False).status == 200


def test_only_the_event_streams_take_the_secret_in_the_query(web):
    # EventSource can't send headers, so the two stream paths take ?k=; nothing else does
    app, c = web
    k = app.secrets.csrf
    c.sse(f"/api/events?k={k}", csrf=False).close()
    assert c.get(f"/api/reviews/some-review/events?k={k}", csrf=False).status == 404         # past the check
    assert c.get("/api/reviews/some-review/events?k=nope", csrf=False).status == 401
    assert c.get("/api/reviews/some-review/events", csrf=False).status == 401
    for path in (f"/api/app?k={k}", f"/api/settings?k={k}", f"/api/reviews/x/events/more?k={k}",
                 f"/api/reviews/x/y/events?k={k}"):
        assert c.get(path, csrf=False).status == 401, path
    assert c.post(f"/api/settings?k={k}", {"parallel": 2}, csrf=False).status == 403       # writes: the header only
    assert c.get(f"/api/events?k={k}", auth=False, csrf=False).status == 401              # and the cookie still


def test_check_takes_the_query_for_streams_only():
    s = security.Secrets()
    h = {"Host": "127.0.0.1:9", "Cookie": f"{security.cookie_name(9)}={s.session}"}
    assert security.check("GET", "/api/events", h, 9, s, {"k": s.csrf}) is None
    assert security.check("GET", "/api/reviews/a-b-1/events", h, 9, s, {"k": s.csrf}) is None
    assert security.check("GET", "/api/app", h, 9, s, {"k": s.csrf})[0] == 401
    assert security.check("GET", "/api/app", {**h, "X-CSRF": s.csrf}, 9, s) is None
    assert security.check("GET", "/", h, 9, s) is None and security.check("GET", "/r/a/hub", h, 9, s) is None


def test_health_needs_no_cookie(web):
    app, c = web
    h = c.get("/health", auth=False).json()
    assert h["app"] == "litrev-app" and h["pid"] > 0 and h["version"] and "proof" not in h


def test_health_proves_it_knows_the_token(web):
    # security F5: the launcher trusts a listener only when it answers HMAC(token, nonce) for its fresh nonce
    app, c = web
    h = c.get("/health?n=abc", auth=False).json()
    assert h["proof"] == hmac.new(app.secrets.token.encode(), b"abc", hashlib.sha256).hexdigest()
    assert h["app"] == "litrev-app" and h["pid"] > 0 and h["version"]
    assert "proof" not in c.get("/health?n=" + "a" * 200, auth=False).json()          # overlong: ignored
    assert "proof" not in c.get("/health?n=", auth=False).json()


def test_static_and_slug_traversal_is_blocked(web):
    app, c = web
    for path in ("/static/../server.py", "/static/..%2Fserver.py", "/static/server.py", "/static/x.js.map",
                 "/r/../hub", "/r/..%2F..%2Fetc/hub", "/api/reviews/..%2Fx"):
        r = c.get(path)
        assert r.status == 404, path
        assert b"import" not in r.raw
    (Path(app.cfg.root) / "outside").mkdir(parents=True)
    (Path(app.cfg.root) / "outside" / "meta.json").write_text("{}")
    (Path(app.cfg.root) / "reviews").mkdir(exist_ok=True)
    (Path(app.cfg.root) / "reviews" / "link").symlink_to(Path(app.cfg.root) / "outside")
    assert security.resolve_review(Path(app.cfg.root), "link") is None
    assert security.resolve_review(Path(app.cfg.root), "Bad_Slug") is None
    with pytest.raises(server.HTTPError) as e:
        app.review("link")
    assert e.value.status == 404


def test_app_pages_carry_a_strict_csp(web):
    app, c = web
    r = c.get("/api/app")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_router_bodies_errors_and_streams(tmp_path):
    app = make_app(tmp_path, skill=stub_skill(tmp_path), features=[])

    def boom(app, req):
        raise server.HTTPError(418, "short and stout")

    def stream(app, req, n):
        return 200, (f"data: {i}\n\n".encode() for i in range(int(n))), {}

    def echo(app, req):
        return 201, {"got": req.json, "q": req.query}, {}

    app.router.add("GET", r"/t/boom", boom)
    app.router.add("GET", r"/t/stream/(?P<n>\d+)", stream)
    app.router.add("POST", r"/t/echo", echo)
    httpd, c = start(app)
    try:
        r = c.get("/t/boom")
        assert (r.status, r.json()) == (418, {"error": "short and stout"})
        assert c.get("/t/stream/3").text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
        assert c.get("/t/echo").status == 405 and c.get("/t/none").status == 404
        r = c.post("/t/echo?x=1", {"a": [1]})
        assert (r.status, r.json()) == (201, {"got": {"a": [1]}, "q": {"x": "1"}})
    finally:
        httpd.shutdown(); httpd.server_close()


def test_problems_block_running_but_not_settings(tmp_path):
    app = make_app(tmp_path, skill=tmp_path / "no-skill", overrides={"claude": tmp_path / "no-claude"}, features=[])
    assert len(app.problems) == 2
    with pytest.raises(server.HTTPError) as e:
        app.require_ready()
    assert e.value.status == 503 and "Claude Code is not installed" in e.value.message
    httpd, c = start(app)
    try:
        info = c.get("/api/app").json()
        assert len(info["problems"]) == 2
        assert c.get("/api/settings").status == 200
        r = c.post("/api/settings", {"skill_override": str(stub_skill(tmp_path))})
        assert r.status == 200 and len(c.get("/api/app").json()["problems"]) == 1     # the skill is found now
    finally:
        httpd.shutdown(); httpd.server_close()


def test_settings_refuse_a_skill_folder_others_can_write(tmp_path):
    # security F4: POST /api/settings {"skill_override": "/Users/Shared/x"} ran someone else's scripts/litrev.py
    app = make_app(tmp_path, skill=stub_skill(tmp_path), features=[])
    (tmp_path / "shared").mkdir()
    shared = stub_skill(tmp_path / "shared")
    shared.chmod(0o777)
    httpd, c = start(app)
    try:
        r = c.post("/api/settings", {"skill_override": str(shared)})
        assert r.status == 422 and "Other users can change" in r.json()["error"]
        assert app.settings.skill_override != str(shared.resolve())
    finally:
        httpd.shutdown(); httpd.server_close()


def test_settings_save_keys_and_apply_in_place(tmp_path):
    app = make_app(tmp_path, skill=stub_skill(tmp_path), features=[])
    cfg = app.cfg
    httpd, c = start(app)
    try:
        r = c.post("/api/settings", {"parallel": 2, "step5_model": "sonnet", "openalex_key": "oa-123"})
        body = r.json()
        assert r.status == 200 and body["parallel"] == 2 and body["keys"] == {"openalex": True, "s2": False}
        assert "oa-123" not in r.text
        assert app.cfg is cfg and app.engine.cfg is cfg and cfg.parallel == 2 and cfg.keys == {"OPENALEX_API_KEY": "oa-123"}
        assert json.loads((tmp_path / "appdir" / "settings.json").read_text())["step5_model"] == "sonnet"
        bad = c.post("/api/settings", {"parallel": 99})
        assert bad.status == 422 and "between 1 and 6" in bad.json()["error"] and cfg.parallel == 2
        assert c.post("/api/settings", {"clear_openalex_key": True}).json()["keys"]["openalex"] is False
    finally:
        httpd.shutdown(); httpd.server_close()


def test_localhost_pages_move_to_127_0_0_1(web):
    # the CSRF secret lives in 127.0.0.1's localStorage (F4); localhost is another origin, so a hand-typed
    # localhost URL would only show "Open Litrev again". Pages (not the API) move to the launcher's origin.
    app, c = web
    for path in ("/", f"/auth?t={app.secrets.token}"):
        r = c.get(path, host=f"localhost:{app.port}", auth=False)
        assert r.status == 302 and r.headers["location"] == f"http://127.0.0.1:{app.port}{path}", path
        assert "set-cookie" not in r.headers
    assert c.get("/api/app", host=f"localhost:{app.port}").status == 200
