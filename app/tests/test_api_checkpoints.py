import json
import threading
import time
from pathlib import Path

import pytest

from litrev_app import api_checkpoints as ac
from litrev_app.store import Store
from web_client import SKILL, make_app, start

needs_skill = pytest.mark.skipif(not (SKILL / "scripts" / "litrev.py").exists(), reason="needs the skill")
STEP0 = {"events": [{"$ask": {"kind": "scope", "question": "Right scope?", "context": "**Found** 3 surveys",
                              "proposal": {"in": ["monocular depth"], "out": ["stereo"], "protocol": [],
                                           "rationale": "the seed is monocular"}}},
                    {"$write": {"path": "{review}/data/seed_taxonomy/P001.json", "content": "{}"}},
                    {"$report": {"step": 0, "summary": "scope and taxonomy"}}]}
# A step session no longer asks about KPIs (fewer questions, 2026-10-12); the refresh form is still a question.
REFRESH_ASK = {"events": [{"$ask": {"kind": "refresh", "question": "Which of these new papers should join the review?",
                                    "context": "c", "proposal": {"papers": [{"id": "P130", "title": "t", "recommend": "include",
                                                                             "why": "w"}]}}},
                          {"$report": {"step": 6, "summary": "refresh"}}]}


def session(tmp_path, monkeypatch, scenario):
    sc = tmp_path / "scenario.json"
    sc.write_text(json.dumps(scenario))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(sc))
    app = make_app(tmp_path)
    httpd, c = start(app)
    slug = c.post("/api/reviews", {"topic": "Depth Anything", "purpose": "p", "cap": 12}).json()["slug"]
    t = threading.Thread(target=app.scheduler.tick)
    t.start()
    return app, httpd, c, slug, t


def wait_ask(c, slug, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = c.get(f"/api/reviews/{slug}/ask").json()["ask"]
        if a and a["status"] == "open":
            return a
        time.sleep(0.1)
    raise AssertionError("no question arrived")


@needs_skill
def test_answer_records_and_releases_the_waiting_session(tmp_path, monkeypatch):
    app, httpd, c, slug, t = session(tmp_path, monkeypatch, STEP0)
    try:
        ask = wait_ask(c, slug)
        assert ask["kind"] == "scope" and ask["proposal"]["in"] == ["monocular depth"]
        d = c.get(f"/api/reviews/{slug}").json()
        assert d["ask"]["question"] == "Right scope?" and d["state"]["busy"] is True
        r = c.post(f"/api/reviews/{slug}/answer", {"in": ["monocular depth", "video depth"], "out": ["stereo"],
                                                   "protocol": [], "notes": "Edited Claude's proposal (Litrev App)"})
        assert r.status == 200 and r.json()["ok"] and r.json()["commands"] == ["scope --in"]
        t.join(60)
        rv = app.review(slug)
        assert Store(rv).ask()["status"] == "delivered"
        assert json.loads((rv / "meta.json").read_text())["scope"]["in"] == ["monocular depth", "video depth"]
        st = Store(rv).load()
        assert (st.step, st.phase) == (1, "running")
        assert c.post(f"/api/reviews/{slug}/answer", {"in": ["x"]}).status == 409       # nothing open any more
    finally:
        httpd.shutdown(); httpd.server_close()


@needs_skill
def test_rejected_answer_keeps_the_question_open(tmp_path, monkeypatch):
    app, httpd, c, slug, t = session(tmp_path, monkeypatch, REFRESH_ASK)
    try:
        wait_ask(c, slug)
        r = c.post(f"/api/reviews/{slug}/answer", {"approve": ["P130"], "reject": [], "date": "2026-10-12"})
        assert r.status == 422 and r.json()["ok"] is False and "P130" in r.json()["error"]
        rv = app.review(slug)
        assert Store(rv).ask()["status"] == "open" and str(rv) in app.engine.live     # still waiting for a fix
        assert c.post(f"/api/reviews/{slug}/answer", ["not", "an", "object"]).status == 422
        assert c.post(f"/api/reviews/{slug}/stop").status == 200
        t.join(60)
        assert Store(rv).load().phase == "idle"
    finally:
        httpd.shutdown(); httpd.server_close()


@needs_skill
def test_kpi_suggest_for_the_kpi_form(tmp_path):
    app = make_app(tmp_path)
    httpd, c = start(app)
    try:
        slug = c.post("/api/reviews", {"topic": "Depth Anything", "purpose": "p", "cap": 12}).json()["slug"]
        r = c.get(f"/api/reviews/{slug}/kpi-suggest")
        assert r.status == 200 and r.json() == {"boards": [], "kpis": [], "names": {}}
        assert c.get("/api/reviews/no-such/kpi-suggest").status == 404
        assert c.get(f"/api/reviews/{slug}/ask").json() == {"ask": None}
    finally:
        httpd.shutdown(); httpd.server_close()


def test_names_come_from_the_review_tables(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "metrics.json").write_text(json.dumps({"M001": {"id": "M001", "name": "AbsRel"}}))
    (tmp_path / "data" / "benchmarks.json").write_text(json.dumps({"B002": {"id": "B002", "name": "KITTI Eigen"}}))
    (tmp_path / "data" / "datasets.json").write_text("not json")
    assert ac.names(tmp_path) == {"M001": "AbsRel", "B002": "KITTI Eigen"}


def test_answer_needs_a_ready_app(tmp_path):
    # M1b review: /answer skipped require_ready(), so a missing skill gave a cryptic 422 instead of the 503 sentence
    app = make_app(tmp_path, skill=tmp_path / "no-skill", features=None)
    httpd, c = start(app)
    try:
        r = c.post("/api/reviews/anything/answer", {"answer": {}})
        assert r.status == 503
    finally:
        httpd.shutdown(); httpd.server_close()
