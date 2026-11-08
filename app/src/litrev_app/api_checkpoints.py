"""Checkpoint routes: the open question, recording the person's answer (through litrev, attributed to them), and
the KPI boards litrev suggests for the KPI form."""
from __future__ import annotations

import json
from pathlib import Path

from .api_reviews import FileCache, fingerprint, read_meta, state_view
from .server import HTTPError
from .store import Store

SUGGEST = FileCache(ttl=120.0)
NAME_TABLES = ("benchmarks", "datasets", "metrics")


def _open(ask: dict | None) -> dict | None:
    return ask if ask and ask.get("status") in ("open", "parked") else None


def get_ask(app, req, slug):
    return 200, {"ask": _open(Store(app.review(slug)).ask())}, {}


def answer(app, req, slug):
    app.require_ready()          # recording runs litrev: without Claude Code / the skill, say so (503), not a 422
    r = app.review(slug)
    if not isinstance(req.json, dict):
        raise HTTPError(422, "The answer must be a JSON object")
    store = Store(r)
    ask = _open(store.ask())
    if not ask:
        raise HTTPError(409, "There is no open question for this review; it may have been answered already")
    res = app.engine.answer(r, req.json)
    if not res.ok:
        return 422, {"ok": False, "error": res.error, "outputs": res.outputs}, {}
    st = store.load()
    app.bus.publish(slug, {"type": "state", "phase": st.phase, "step": st.step, "message": st.message,
                           "answered": ask.get("kind")})
    app.scheduler.wake()
    return 200, {"ok": True, "summary": res.summary, "commands": [" ".join(c[:2]) for c in res.commands],
                 "state": state_view(app, r, st)}, {}


def names(r: Path) -> dict:
    out = {}
    for t in NAME_TABLES:
        try:
            d = json.loads((r / "data" / f"{t}.json").read_text())
        except (OSError, ValueError):
            continue
        rows = d.values() if isinstance(d, dict) else d if isinstance(d, list) else []
        for x in rows:
            if isinstance(x, dict) and x.get("id"):
                out[x["id"]] = x.get("name") or x["id"]
    return out


def kpi_suggest(app, req, slug):
    r = app.review(slug)

    def compute() -> dict:
        p = app.engine.litrev(r).run(["kpi", "--suggest"])
        if p.returncode != 0:
            raise HTTPError(502, f"litrev kpi --suggest failed: {(p.stderr or p.stdout or '').strip()[-300:]}")
        try:
            boards = json.loads(p.stdout)
        except ValueError:
            raise HTTPError(502, "litrev kpi --suggest did not print JSON") from None
        return {"boards": boards if isinstance(boards, list) else []}

    out = SUGGEST.get(str(r), fingerprint(r), compute)          # an error raises and is not cached
    return 200, {**out, "kpis": read_meta(r).get("kpis") or [], "names": names(r)}, {}


def register(router) -> None:
    s = r"(?P<slug>[a-z0-9-]+)"
    router.add("GET", rf"/api/reviews/{s}/ask", get_ask)
    router.add("POST", rf"/api/reviews/{s}/answer", answer)
    router.add("GET", rf"/api/reviews/{s}/kpi-suggest", kpi_suggest)
