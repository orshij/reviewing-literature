"""Checks for every request to the local server (spec: Server, pages, security).

Per launch there are three secrets: `token` (in server.json and the /auth link the launcher opens), `session` (the
HttpOnly SameSite=Strict cookie /auth sets) and `csrf` (only in the fragment of /auth's redirect, `/#k=`; the page keeps
it in localStorage and sends it as X-CSRF on every /api request, or as ?k= on the two event streams). Cookies ignore
ports, so another loopback listener can get the cookie; the csrf secret's origin includes the port (security F4)."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9-]{1,80}$")
HUB_CSP = "sandbox allow-scripts allow-popups allow-downloads"
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
BASE_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
APP_HEADERS = {"Content-Security-Policy": APP_CSP, "X-Frame-Options": "DENY"}
OPEN_PATHS = ("/auth", "/health")
STREAM_RE = re.compile(r"/api/(?:reviews/[a-z0-9-]{1,80}/)?events")      # EventSource can't send headers: ?k=
MAX_NONCE = 64                       # /health?n=: the launcher sends 32 hex digits


def _secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Secrets:
    token: str = field(default_factory=_secret)
    session: str = field(default_factory=_secret)
    csrf: str = field(default_factory=_secret)


def health_proof(token: str, nonce: str) -> str:
    """What /health answers for the launcher's nonce: only the server that wrote the 0600 server.json knows `token`
    (security F5: any local listener can echo a pid)."""
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def cookie_name(port: int) -> str:
    return f"litrev_{port}"            # cookies ignore ports; the name keeps two launches apart


def set_cookie(name: str, value: str) -> str:
    return f"{name}={value}; HttpOnly; SameSite=Strict; Path=/"


def host_ok(host: str | None, port: int) -> bool:
    """DNS rebinding guard: a page from evil.example resolved to 127.0.0.1 still sends Host: evil.example."""
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def origin_ok(origin: str | None, port: int) -> bool:
    return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def cookie_value(header: str | None, name: str) -> str | None:
    if not header:
        return None
    c = SimpleCookie()
    try:
        c.load(header)
    except CookieError:
        return None
    m = c.get(name)
    return m.value if m else None


def check(method: str, path: str, headers, port: int, s: Secrets,
          query: dict | None = None) -> tuple[int, str] | None:
    """None when the request may go on, else (status, reason). Order: Host, cookie, then Origin and CSRF on writes,
    and the CSRF secret on /api reads (security F4: the cookie alone reads nothing; 401 shows "Open Litrev again")."""
    if not host_ok(headers.get("Host"), port):
        return 403, "This address is only for the Litrev app on this Mac (wrong Host)"
    if method == "GET" and path in OPEN_PATHS:
        return None
    if not same(cookie_value(headers.get("Cookie"), cookie_name(port)), s.session):
        return 401, "Not signed in: open Litrev from the app"
    if method not in ("GET", "HEAD"):
        if not origin_ok(headers.get("Origin"), port):
            return 403, "Request from another site (wrong Origin)"
        if not same(headers.get("X-CSRF"), s.csrf):
            return 403, "Missing or wrong CSRF token"
    elif path.startswith("/api/") and not same(headers.get("X-CSRF"), s.csrf) and not (
            STREAM_RE.fullmatch(path) and same((query or {}).get("k"), s.csrf)):
        return 401, "Not signed in: open Litrev from the app"
    return None


def resolve_review(root: Path, slug: str) -> Path | None:
    """`root/reviews/<slug>` when the slug is valid and the folder, after symlinks, is inside `root/reviews` and has
    a meta.json. Returns the unresolved path, which is the key Engine.live and Store use."""
    if not SLUG_RE.match(slug or ""):
        return None
    base = Path(root) / "reviews"
    p = base / slug
    try:
        real = p.resolve(strict=True)
        real.relative_to(base.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return p if (real / "meta.json").is_file() else None
