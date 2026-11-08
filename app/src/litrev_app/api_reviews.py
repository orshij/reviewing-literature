"""Review routes: list, create, detail, Run/Resume/Retry/Stop, the live event stream and the sandboxed hub."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path

from . import security
from .briefs import STEP_TITLES
from .bus import RING, sse_stream
from .events import StreamState
from .server import HTTPError
from .store import Store, safe_name

EST_BASE, EST_PER_PAPER = 30.0, 0.75
NO_HUB = ("<!doctype html><meta charset=utf-8><title>No hub yet</title>"
          "<p style=\"font:16px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;max-width:34rem\">The hub is made "
          "in step 7 (render). It appears here once the review gets there.</p>")
META_KEYS = ("seed", "purpose", "caps", "created_at", "updated_at", "hub_url", "kpis", "scope", "categories")
NO_VIDEO = ("<!doctype html><meta charset=utf-8><title>No video yet</title>"
            "<p style=\"font:16px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;max-width:34rem\">The video is made "
            "in step 8. Press Make video on the review page.</p>")


def estimate_usd(cap: int) -> int:
    """Spec rule of thumb, API-equivalent: ~$30 + ~$0.75 per paper."""
    return round(EST_BASE + EST_PER_PAPER * cap)


def slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    if len(s) > 60:
        s = s[:60].rsplit("-", 1)[0] if "-" in s[:60] else s[:60]
    return s.strip("-") or "review"


def free_slug(base: Path, slug: str) -> str:
    if not (base / slug).exists():
        return slug
    for i in range(2, 1000):
        cand = f"{slug[:55].rstrip('-')}-{i}"
        if not (base / cand).exists():
            return cand
    raise HTTPError(409, "Too many reviews with this name; change the topic a little")


def _int(v, lo: int, hi: int, label: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPError(422, f"{label} must be a whole number") from None
    if not lo <= n <= hi:
        raise HTTPError(422, f"{label} must be between {lo} and {hi}")
    return n


def read_meta(r: Path) -> dict:
    try:
        d = json.loads((r / "meta.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


class FileCache:
    """A value computed from a review's files, reused while their fingerprint holds (and at most `ttl` seconds)."""

    def __init__(self, ttl: float = 30.0, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str, fingerprint, compute):
        with self._lock:
            hit = self._d.get(key)
        if hit and hit[0] == fingerprint and self.clock() - hit[1] < self.ttl:
            return hit[2]
        val = compute()
        with self._lock:
            self._d[key] = (fingerprint, self.clock(), val)
        return val


STATUS, PAPERS = FileCache(ttl=30.0), FileCache(ttl=300.0)


def fingerprint(r: Path) -> tuple:
    """What `status` reads: meta.json, the data/ tables and folders, cached full texts, the rendered hub."""
    parts = []
    for p in (r / "meta.json", r / "rendered" / "hub.html", r / "cache" / "fulltext"):
        try:
            parts.append(p.stat().st_mtime_ns)
        except OSError:
            parts.append(0)
    try:
        with os.scandir(r / "data") as it:
            parts += sorted((e.name, e.stat().st_mtime_ns) for e in it)
    except OSError:
        pass
    return tuple(parts)


def review_status(app, r: Path) -> dict:
    return STATUS.get(str(r), fingerprint(r), lambda: app.engine.litrev(r).status())


def step5_progress(r: Path) -> dict | None:
    """Method-detail files ÷ papers that get one (litrev's needs_details: included, not a survey, not a taxonomy
    source). Counting files is cheap; `details-status` checks quotes at ~0.5 s per paper."""
    pj = r / "data" / "papers.json"
    try:
        fp = pj.stat().st_mtime_ns
    except OSError:
        return None

    def need() -> frozenset:
        try:
            papers = json.loads(pj.read_text())
        except (OSError, ValueError):
            return frozenset()
        rows = papers.values() if isinstance(papers, dict) else papers if isinstance(papers, list) else []
        return frozenset(p["id"] for p in rows if isinstance(p, dict) and p.get("id")
                         and p.get("status") == "included" and not p.get("is_survey") and not p.get("taxonomy_source"))

    ids = PAPERS.get(str(pj), fp, need)
    if not ids:
        return None
    dd = r / "data" / "details"
    done = sum(1 for f in dd.glob("*.json") if f.stem in ids) if dd.is_dir() else 0
    return {"done": done, "total": len(ids)}


def steps(status: dict, current: int | None = None) -> list[dict]:
    """Step rail. `current` is the app run's step: a later step that status calls done only because nothing has
    reached it yet (e.g. "no papers need details" during step 0) shows as `upcoming`."""
    rows = status.get("rows") or []
    out = []
    for n in range(9 if any(r.get("step") == 8 for r in rows) else 8):     # step 8 only for a video opt-in
        rs = [r for r in rows if r.get("step") == n]
        counted = {r.get("state") for r in rs if r.get("key") != "publish"}
        if not counted:
            agg = "todo"
        elif counted == {"done"}:
            agg = "done"
        else:
            agg = next((s for s in ("todo", "stale", "warn") if s in counted), "unknown")
        if current is not None and n > current and agg == "done":
            agg = "upcoming"
        out.append({"step": n, "title": STEP_TITLES[n], "state": agg, "rows": rs})
    return out


def state_view(app, r: Path, st) -> dict:
    return {**asdict(st), "busy": app.scheduler.busy(r), "started_at": app.scheduler.started_at(r)}


def seed_backlog(app, r: Path, st, sessions: list[dict]) -> None:
    """After an app restart the bus is empty: rebuild the feed from the last session's events file."""
    if app.bus.recent(r.name):
        return
    sid = st.sid or (sessions[-1].get("sid") if sessions else None)
    path = Store(r).events_path(sid) if safe_name(sid) else None   # sessions can write state/sessions (F1)
    if not path or not path.is_file():
        return
    ss, acts = StreamState(), []
    with open(path, "rb") as f:                 # bytes: the file may end inside a character while a session writes
        for line in f:
            try:
                e = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(e, dict):
                acts += [{"type": "activity", **a.to_dict()} for a in ss.feed(e)]
    app.bus.seed(r.name, acts[-RING:])


def elapsed_s(app, r: Path, sessions: list[dict]) -> int:
    total = sum(max(0.0, (s.get("end") or 0) - s["start"]) for s in sessions if s.get("start"))
    started = app.scheduler.started_at(r)
    return round(total + (max(0.0, time.time() - started) if started else 0.0))


# ---- routes ------------------------------------------------------------------------------------------------------
def list_reviews(app, req):
    out = []
    for r in app.scheduler.reviews():
        if security.resolve_review(app.cfg.root, r.name) is None:
            continue
        meta, store = read_meta(r), Store(r)
        made_here = (store.dir / "state.json").exists()
        st = store.load()
        ask = store.ask() if made_here else None
        out.append({"slug": r.name, "seed": meta.get("seed") or r.name, "purpose": meta.get("purpose") or "",
                    "created_at": meta.get("created_at"), "updated_at": meta.get("updated_at"),
                    "cap": (meta.get("caps") or {}).get("max_papers"), "made_in_claude_code": not made_here,
                    "state": state_view(app, r, st), "asking": bool(ask and ask.get("status") in ("open", "parked")),
                    "hub": (r / "rendered" / "hub.html").is_file(), "hub_url": meta.get("hub_url")})
    out.sort(key=lambda d: str(d["updated_at"] or d["created_at"] or ""), reverse=True)
    return 200, {"reviews": out, "root": str(app.cfg.root), "problems": app.problems}, {}


def create(app, req):
    app.require_ready()
    d = req.json if isinstance(req.json, dict) else {}
    topic, purpose = str(d.get("topic") or "").strip(), str(d.get("purpose") or "").strip()
    if not topic:
        raise HTTPError(422, "Say what the review is about: a topic, a method or a paper title")
    if not purpose:
        raise HTTPError(422, "Say what the review is for, so Claude can judge which papers matter")
    cap = _int(d.get("cap", 40), 5, 300, "The paper cap")
    years = _int(d.get("years", 3), 1, 30, "The year window")
    video = bool(d.get("video"))
    minutes = _int(d.get("video_minutes", VIDEO_DEFAULT_MIN), 3, 60, "The video length") if video else None
    base = Path(app.cfg.root) / "reviews"
    base.mkdir(parents=True, exist_ok=True)
    slug = free_slug(base, slugify(topic))
    try:
        r = app.engine.create_review(slug, seed=topic, purpose=purpose, cap=cap, years=years)
    except RuntimeError as e:
        raise HTTPError(422, f"litrev couldn't create the review: {e}")
    if video:        # the person's answer to the step-0 video question, given on the form: no question mid-run
        opt_in(app, r, minutes)
    app.bus.publish(slug, {"type": "state", "phase": "running", "step": 0, "message": "Created"})
    app.scheduler.wake()
    return 201, {"slug": slug, "estimate_usd": estimate_usd(cap)}, {}


VIDEO_DEFAULT_MIN = 20
VIDEO_COST = "about USD 18-34 of usage for 20 minutes; the voice and the video render on this Mac"


def opt_in(app, r: Path, minutes: int) -> None:
    p = app.engine.litrev(r).run(["presentation", "--minutes", str(minutes), "--by", "user"])
    if p.returncode != 0:
        raise HTTPError(422, f"litrev couldn't record the video request: {(p.stderr or p.stdout)[-300:]}")


def video_mp4(r: Path, meta: dict) -> Path:
    return r / "rendered" / "presentation" / f"{meta.get('slug') or r.name}.mp4"


def video_view(r: Path, meta: dict) -> dict:
    """What the review page shows about step 8: asked for or not, the file, and the checks the video step wrote."""
    pres = meta.get("presentation") or {}
    try:
        v = json.loads((r / "data" / "presentation" / "video.json").read_text())
    except (OSError, ValueError):
        v = {}
    f = video_mp4(r, meta)
    return {"wanted": bool(pres.get("wanted")), "minutes": pres.get("minutes"), "exists": f.is_file(),
            "duration_s": (v.get("probe") or {}).get("video_s"), "size_mb": (v.get("probe") or {}).get("size_mb"),
            "checks": v.get("checks") or {}, "built_at": v.get("built_at"),
            "captions": f.with_suffix(".vtt").is_file(), "slides": f.with_suffix(".pptx").is_file(), "cost": VIDEO_COST}


def make_video(app, req, slug):
    """Make video on a review: record the opt-in, then queue step 8 (steps 0-7 first when they are not done)."""
    r = app.review(slug)
    app.require_ready()
    d = req.json if isinstance(req.json, dict) else {}
    minutes = _int(d.get("minutes", VIDEO_DEFAULT_MIN), 3, 60, "The video length")
    st = Store(r).load()
    if st.phase in ("running", "paused", "waiting_user") or st.sid:
        raise HTTPError(409, "A run is going on this review: press Make video when it has ended")
    opt_in(app, r, minutes)
    status = review_status(app, r)
    first = min((row["step"] for row in status.get("rows") or [] if row.get("step", 9) < 8
                 and row.get("state") in ("todo", "stale") and row.get("key") != "publish"), default=8)
    nxt = app.engine.queue_from(r, first, message=f"Making a {minutes}-minute video")
    app.bus.publish(slug, {"type": "state", "phase": nxt.phase, "step": nxt.step, "message": nxt.message})
    app.scheduler.wake()
    return 200, {"state": state_view(app, r, nxt)}, {}


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _file_range(req, f: Path, ctype: str):
    """The file, or the byte range the browser asked for: Safari plays a <video> only from a server that answers
    Range requests, and every browser needs them to seek."""
    size = f.stat().st_size
    m = RANGE_RE.match((req.headers.get("Range") or "").strip())
    base = {"Content-Type": ctype, "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if not m or (not m.group(1) and not m.group(2)):
        return 200, f.read_bytes(), base
    if m.group(1):
        a, b = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
    else:                                            # "bytes=-500": the last 500 bytes
        a, b = max(0, size - int(m.group(2))), size - 1
    if a >= size or a > b:
        return 416, b"", {**base, "Content-Range": f"bytes */{size}"}
    b = min(b, size - 1, a + (8 << 20) - 1)          # at most 8 MB per answer: the browser asks again
    with f.open("rb") as fh:
        fh.seek(a)
        data = fh.read(b - a + 1)
    return 206, data, {**base, "Content-Range": f"bytes {a}-{b}/{size}"}


def video(app, req, slug):
    r = app.review(slug)
    f = video_mp4(r, read_meta(r))
    if not f.is_file():
        return 404, NO_VIDEO, {}
    return _file_range(req, f, "video/mp4")


def video_captions(app, req, slug):
    r = app.review(slug)
    f = video_mp4(r, read_meta(r)).with_suffix(".vtt")
    if not f.is_file():
        return 404, b"", {}
    return 200, f.read_bytes(), {"Content-Type": "text/vtt; charset=utf-8", "Cache-Control": "no-store"}


def video_slides(app, req, slug):
    """The video's slides as a PowerPoint deck (`video` writes it beside the MP4), as a download."""
    r = app.review(slug)
    f = video_mp4(r, read_meta(r)).with_suffix(".pptx")
    if not f.is_file():
        return 404, b"", {}
    return 200, f.read_bytes(), {"Content-Type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                                 "Content-Disposition": f'attachment; filename="{f.name}"', "Cache-Control": "no-store"}


def detail(app, req, slug):
    r = app.review(slug)
    meta, store = read_meta(r), Store(r)
    made_here = (store.dir / "state.json").exists()
    st = store.load()
    status = review_status(app, r)
    ask = store.ask()
    sessions = store.sessions()
    seed_backlog(app, r, st, sessions)
    cap = (meta.get("caps") or {}).get("max_papers") or 40
    own = app.scheduler.own_pids() | ({st.pid} if st.pid else set())
    return 200, {
        "slug": slug, "seed": meta.get("seed") or slug, "purpose": meta.get("purpose") or "",
        "made_in_claude_code": not made_here, "meta": {k: meta.get(k) for k in META_KEYS},
        "state": state_view(app, r, st), "steps": steps(status, st.step if made_here and st.phase != "done" else None),
        "next": status.get("next"),
        "skill_version": status.get("skill_version"), "status_error": status.get("error"),
        "ask": ask if ask and ask.get("status") in ("open", "parked") else None,
        "step5": step5_progress(r), "activities": app.bus.recent(slug), "sessions": sessions[-50:],
        "cost": {"spent_usd": round(st.cost_usd or 0.0, 2), "estimate_usd": estimate_usd(cap),
                 "label": "API-equivalent estimate", "formula": "~$30 + ~$0.75/paper"},
        "decisions": list(reversed((meta.get("decisions") or [])[-100:])),     # newest first: the Decisions tab
        "elapsed_s": elapsed_s(app, r, sessions), "outside": app.outside.get(r, own),
        "hub": (r / "rendered" / "hub.html").is_file(), "video": video_view(r, meta),
    }, {}


def action(app, req, slug, action):
    r = app.review(slug)
    if action != "stop":
        app.require_ready()
    st = getattr(app.scheduler, action)(r)          # run | resume | retry | stop; Conflict -> 409
    return 200, {"state": state_view(app, r, st)}, {}


def events(app, req, slug):
    app.review(slug)
    return 200, sse_stream(app.bus, slug), {}


def hub(app, req, slug):
    r = app.review(slug)
    f = r / "rendered" / "hub.html"
    if not f.is_file():
        return 404, NO_HUB, {"Content-Security-Policy": security.HUB_CSP}
    return 200, f.read_bytes(), {"Content-Type": "text/html; charset=utf-8",
                                 "Content-Security-Policy": security.HUB_CSP}


def register(router) -> None:
    s = r"(?P<slug>[a-z0-9-]+)"
    router.add("GET", r"/api/reviews", list_reviews)
    router.add("POST", r"/api/reviews", create)
    router.add("GET", rf"/api/reviews/{s}", detail)
    router.add("POST", rf"/api/reviews/{s}/(?P<action>run|resume|retry|stop)", action)
    router.add("GET", rf"/api/reviews/{s}/events", events)
    router.add("GET", rf"/r/{s}/hub", hub)
    router.add("POST", rf"/api/reviews/{s}/video", make_video)
    router.add("GET", rf"/r/{s}/video", video)
    router.add("GET", rf"/r/{s}/video\.vtt", video_captions)
    router.add("GET", rf"/r/{s}/video\.pptx", video_slides)
