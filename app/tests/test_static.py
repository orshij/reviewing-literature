import json
import re
import shutil
import subprocess
from urllib.parse import quote

import pytest

from litrev_app import server
from web_client import make_app, start, stub_skill

STATIC = server.STATIC
JS = sorted(p.name for p in STATIC.glob("*.js"))


def test_index_is_served_without_the_csrf_secret_and_with_the_script_order(tmp_path):
    app = make_app(tmp_path, skill=stub_skill(tmp_path), features=[])
    httpd, c = start(app)
    try:
        r = c.get("/")
        assert r.status == 200 and app.secrets.csrf not in r.text and "litrev-csrf" not in r.text    # security F4
        assert "{{" not in r.text
        srcs = re.findall(r'<script src="([^"]+)"></script>', r.text)
        assert srcs[:3] == ["/static/app.js", "/static/review.js", "/static/asks.js"]
        head, rest = r.text.split("<!-- feature scripts -->")
        block = rest.split("<!-- /feature scripts -->")[0]
        assert "/static/asks.js" in head
        assert all(re.fullmatch(r'<script src="/static/[a-z0-9-]+\.js"></script>', ln.strip())
                   for ln in block.splitlines() if ln.strip())
        for name, ctype in (("app.js", "text/javascript"), ("app.css", "text/css"), ("icon.svg", "image/svg+xml")):
            s = c.get(f"/static/{name}")
            assert s.status == 200 and s.headers["content-type"].startswith(ctype), name
    finally:
        httpd.shutdown(); httpd.server_close()


def test_no_inline_code_and_no_html_injection_points():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html and "<style" not in html
    for name in JS:
        src = (STATIC / name).read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert bad not in src, (name, bad)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("name", JS)
def test_scripts_parse(name):
    p = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_css_has_light_and_dark_tokens():
    css = (STATIC / "app.css").read_text()
    assert "@media (prefers-color-scheme: dark)" in css and ':root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css and "@media (prefers-reduced-motion: reduce)" in css
    for token in ("--paper", "--sheet", "--ink", "--ink-2", "--rule", "--pen", "--marker", "--ok", "--warn", "--bad"):
        assert css.count(f"{token}:") == 3, token            # light, dark by system, dark by choice


# app.js with stub browser globals: where the CSRF secret comes from and where it goes (security F4)
CSRF_HARNESS = r"""
const sc = JSON.parse(process.argv[3]);
const out = { fetch: [], es: [], replaced: [], store: Object.assign({}, sc.store || {}) };
global.window = global;
global.document = { hidden: false, addEventListener() {}, querySelector: () => null, getElementById: () => null };
global.location = { hash: sc.hash, pathname: '/', search: '' };
global.history = { replaceState(st, t, url) { out.replaced.push(url); location.hash = ''; } };
const storage = { getItem: (k) => (k in out.store ? out.store[k] : null), setItem: (k, v) => { out.store[k] = String(v); } };
Object.defineProperty(global, 'localStorage', { configurable: true,
  get() { if (sc.blocked) throw new Error('SecurityError'); return storage; } });
global.fetch = async (path, opt) => { out.fetch.push([path, opt.method, opt.headers['X-CSRF'] || null]);
  return { status: 200, ok: true, json: async () => ({}) }; };
global.EventSource = function (url) { out.es.push(url); this.close = () => {}; };
require(process.argv[2]);
(async () => {
  await LR.api('GET', '/api/app');
  await LR.api('POST', '/api/settings', {});
  LR.sse(null, () => {}, { keep: true });
  LR.sse('a-review', () => {}, { keep: true });
  out.hash = location.hash;
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("sc, k, replaced", [
    ({"hash": "#k=from-auth_1"}, "from-auth_1", ["/"]),                             # fresh from /auth: kept, hash gone
    ({"hash": "", "store": {"litrev-csrf": "kept-2"}}, "kept-2", []),                # a reload: from localStorage
    ({"hash": "#/r/a-review", "store": {"litrev-csrf": "kept-3"}}, "kept-3", []),    # a page route stays
    ({"hash": "#k=new-4", "store": {"litrev-csrf": "old"}}, "new-4", ["/"]),         # a new launch wins
    ({"hash": "#k=only-5", "blocked": True}, "only-5", ["/"]),                       # storage blocked: still works
    ({"hash": "#k=bad%E0", "store": {}}, "bad%E0", ["/"]),                           # a bad escape doesn't throw
])
def test_csrf_comes_from_the_auth_fragment_and_goes_on_every_api_call(tmp_path, sc, k, replaced):
    harness = tmp_path / "harness.js"
    harness.write_text(CSRF_HARNESS)
    p = subprocess.run(["node", str(harness), str(STATIC / "app.js"), json.dumps(sc)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["fetch"] == [["/api/app", "GET", k], ["/api/settings", "POST", k]]
    q = quote(k, safe="-_.!~*'()")                                              # = encodeURIComponent(k)
    assert out["es"] == [f"/api/events?k={q}", f"/api/reviews/a-review/events?k={q}"]
    assert out["replaced"] == replaced and not out["hash"].startswith("#k=")
    if not sc.get("blocked"):
        assert out["store"]["litrev-csrf"] == k


def test_only_lr_api_and_lr_sse_talk_to_the_server():
    # security F4: a direct fetch or EventSource would skip the X-CSRF header or ?k=
    for name in JS:
        src = (STATIC / name).read_text()
        n_fetch, n_es = len(re.findall(r"\bfetch\(", src)), len(re.findall(r"\bEventSource\(", src))
        assert (n_fetch, n_es) == ((1, 1) if name == "app.js" else (0, 0)), name
