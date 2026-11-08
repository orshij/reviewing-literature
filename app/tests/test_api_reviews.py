import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from litrev_app import api_reviews as ar
from litrev_app.security import HUB_CSP
from litrev_app.store import Store
from web_client import SKILL, make_app, start

needs_skill = pytest.mark.skipif(not (SKILL / "scripts" / "litrev.py").exists(), reason="needs the skill")
STATUS_CMD = "python3 /s/scripts/litrev.py status --json --review /r"
SCEN = {"events": [{"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
    {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": STATUS_CMD}}]}},
    {"$sleep": 0.3}, {"$report": {"step": 0, "summary": "looked around"}}]}


@pytest.fixture
def web(tmp_path):
    app = make_app(tmp_path)
    httpd, client = start(app)
    yield app, client
    httpd.shutdown()
    httpd.server_close()


def create(c, topic="Depth Anything", **kw):
    r = c.post("/api/reviews", {"topic": topic, "purpose": "Pick a depth model to evaluate", "cap": 12, **kw})
    assert r.status == 201, r.text
    return r.json()["slug"]


def test_slugify_and_free_slug(tmp_path):
    assert ar.slugify("Depth Anything v2: zero-shot depth!") == "depth-anything-v2-zero-shot-depth"
    assert ar.slugify("Défi caméra") == "defi-camera" and ar.slugify("Глубина") == "review"
    long = ar.slugify("word " * 30)
    assert len(long) <= 60 and not long.endswith("-")
    (tmp_path / "x").mkdir(); (tmp_path / "x-2").mkdir()
    assert ar.free_slug(tmp_path, "x") == "x-3" and ar.free_slug(tmp_path, "y") == "y"
    assert ar.estimate_usd(40) == 60 and ar.estimate_usd(128) == 126


def test_step5_progress_counts_only_papers_that_get_details(tmp_path):
    (tmp_path / "data" / "details").mkdir(parents=True)
    papers = {"P1": {"id": "P1", "status": "included"}, "P2": {"id": "P2", "status": "included", "is_survey": True},
              "P3": {"id": "P3", "status": "excluded"}, "P4": {"id": "P4", "status": "included", "taxonomy_source": "x"},
              "P5": {"id": "P5", "status": "included"}}
    (tmp_path / "data" / "papers.json").write_text(json.dumps(papers))
    for pid in ("P1", "P2", "P9"):
        (tmp_path / "data" / "details" / f"{pid}.json").write_text("{}")
    assert ar.step5_progress(tmp_path) == {"done": 1, "total": 2}
    assert ar.step5_progress(tmp_path / "nothing") is None


def test_steps_group_rows_and_ignore_publish():
    st = ar.steps({"rows": [{"step": 0, "key": "scope", "state": "done"}, {"step": 1, "key": "search", "state": "done"},
                            {"step": 1, "key": "screen", "state": "stale"}, {"step": 4, "key": "openalex", "state": "warn"},
                            {"step": 7, "key": "render", "state": "done"}, {"step": 7, "key": "publish", "state": "todo"}]})
    assert [s["state"] for s in st] == ["done", "stale", "todo", "todo", "warn", "todo", "todo", "done"]
    assert st[3]["title"] and len(st[7]["rows"]) == 2


@needs_skill
def test_create_list_and_detail(web):
    app, c = web
    slug = create(c, "Depth Anything v2: zero-shot depth!", years=2)
    assert slug == "depth-anything-v2-zero-shot-depth"
    meta = json.loads((Path(app.cfg.root) / "reviews" / slug / "meta.json").read_text())
    assert meta["caps"]["max_papers"] == 12 and meta["caps"]["years"] == 2
    assert create(c, "Depth Anything v2: zero-shot depth!") == slug + "-2"
    lst = c.get("/api/reviews").json()["reviews"]
    mine = next(x for x in lst if x["slug"] == slug)
    assert mine["made_in_claude_code"] is False and mine["state"]["phase"] == "running" and mine["cap"] == 12
    d = c.get(f"/api/reviews/{slug}").json()
    assert [s["step"] for s in d["steps"]] == list(range(8)) and d["steps"][0]["state"] == "todo"
    assert d["cost"] == {"spent_usd": 0.0, "estimate_usd": 39, "label": "API-equivalent estimate",
                         "formula": "~$30 + ~$0.75/paper"}
    assert d["ask"] is None and d["hub"] is False and d["outside"]["message"] is None and d["skill_version"]
    assert d["decisions"] == []
    m = Path(app.cfg.root) / "reviews" / slug / "meta.json"
    meta = json.loads(m.read_text())
    meta["decisions"] = [{"id": f"DEC{i:03d}", "kind": "kpi", "decision": f"d{i}", "reason": "r", "by": "agent"}
                         for i in range(1, 106)]
    m.write_text(json.dumps(meta))
    ds = c.get(f"/api/reviews/{slug}").json()["decisions"]
    assert len(ds) == 100 and ds[0]["id"] == "DEC105" and ds[-1]["id"] == "DEC006"   # newest first, capped
    assert c.get("/api/reviews/no-such-review").status == 404


@needs_skill
def test_create_checks_its_fields(web, tmp_path):
    app, c = web
    for body, words in (({"purpose": "p"}, "about"), ({"topic": "t"}, "for"),
                        ({"topic": "t", "purpose": "p", "cap": "lots"}, "whole number"),
                        ({"topic": "t", "purpose": "p", "cap": 1000}, "between 5 and 300"),
                        ({"topic": "t", "purpose": "p", "years": 0}, "between 1 and 30")):
        r = c.post("/api/reviews", body)
        assert r.status == 422 and words in r.json()["error"], (body, r.text)
    broken = make_app(tmp_path / "b", overrides={"claude": tmp_path / "no-claude"})
    h2, c2 = start(broken)
    try:
        r = c2.post("/api/reviews", {"topic": "t", "purpose": "p"})
        assert r.status == 503 and "Claude Code is not installed" in r.json()["error"]
    finally:
        h2.shutdown(); h2.server_close()


@needs_skill
def test_skill_mode_review_is_listed_untouched_and_runs_from_its_first_open_step(web):
    app, c = web
    r = Path(app.cfg.root) / "reviews" / "cli-made"
    lit = app.engine.litrev(r)
    p = subprocess.run([sys.executable, str(SKILL / "scripts" / "litrev.py"), "init", "--path", str(r), "--slug",
                        "cli-made", "--purpose", "p", "--seed", "s"], capture_output=True, text=True, env=app.engine.env())
    assert p.returncode == 0, p.stderr
    assert lit.run(["scope", "--in", "monocular depth", "--notes", "test", "--by", "user"]).returncode == 0
    (r / "data" / "seed_taxonomy").mkdir(parents=True, exist_ok=True)
    (r / "data" / "seed_taxonomy" / "P001.json").write_text("{}")
    row = next(x for x in c.get("/api/reviews").json()["reviews"] if x["slug"] == "cli-made")
    assert row["made_in_claude_code"] is True and row["state"]["phase"] == "idle"
    d = c.get("/api/reviews/cli-made").json()
    assert d["made_in_claude_code"] is True and d["steps"][0]["state"] == "done"
    assert not (r / "cache" / "app").exists()                  # looking writes nothing
    st = c.post("/api/reviews/cli-made/run").json()["state"]
    assert (st["step"], st["phase"]) == (1, "running") and (r / "cache" / "app" / "state.json").exists()


@needs_skill
def test_buttons_follow_the_phase(web):
    app, c = web
    slug = create(c)
    assert c.post(f"/api/reviews/{slug}/run").status == 409          # already queued
    assert c.post(f"/api/reviews/{slug}/stop").json()["state"]["phase"] == "idle"
    r = c.post(f"/api/reviews/{slug}/resume")
    assert r.status == 409 and "paused" in r.json()["error"]
    assert c.post(f"/api/reviews/{slug}/run").json()["state"]["phase"] == "running"
    assert c.post(f"/api/reviews/{slug}/retry").status == 409
    assert c.post(f"/api/reviews/{slug}/explode").status == 404


@needs_skill
def test_hub_is_sandboxed(web):
    app, c = web
    slug = create(c)
    r = c.get(f"/r/{slug}/hub")
    assert r.status == 404 and "step 7" in r.text and r.headers["content-security-policy"] == HUB_CSP
    hub = Path(app.cfg.root) / "reviews" / slug / "rendered" / "hub.html"
    hub.parent.mkdir(parents=True, exist_ok=True)
    hub.write_text("<!doctype html><title>hub</title><script>1</script>")
    r = c.get(f"/r/{slug}/hub")
    assert r.status == 200 and r.text.endswith("<script>1</script>")
    assert r.headers["content-security-policy"] == "sandbox allow-scripts allow-popups allow-downloads"
    assert "x-frame-options" not in r.headers
    assert c.get(f"/api/reviews/{slug}").json()["hub"] is True


@needs_skill
def test_sse_delivers_the_sessions_activities_and_a_restart_keeps_them(tmp_path, monkeypatch):
    sc = tmp_path / "scenario.json"
    sc.write_text(json.dumps(SCEN))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(sc))
    app = make_app(tmp_path)
    httpd, c = start(app)
    try:
        slug = create(c)
        sse = c.sse(f"/api/reviews/{slug}/events")
        t = threading.Thread(target=app.scheduler.tick)
        t.start()
        seen, deadline = [], time.time() + 30
        while time.time() < deadline and not any(e.get("title") == "Checking where the review stands" for e in seen):
            ev = sse.next_event(timeout=1)
            if ev:
                seen.append(ev)
        t.join(60)
        sse.close()
        assert seen[0]["type"] == "state" and seen[0]["message"] == "Starting step 0"
        assert any(e["type"] == "activity" and e["kind"] == "litrev" for e in seen)
        assert all(e["slug"] == slug for e in seen) and [e["seq"] for e in seen] == sorted(e["seq"] for e in seen)
    finally:
        httpd.shutdown(); httpd.server_close()
    again = make_app(tmp_path)                                   # a new launch: the bus starts empty
    h2, c2 = start(again)
    try:
        d = c2.get(f"/api/reviews/{slug}").json()
        assert any(a["title"] == "Checking where the review stands" for a in d["activities"])
        assert d["sessions"][-1]["step"] == 0 and d["elapsed_s"] >= 0
    finally:
        h2.shutdown(); h2.server_close()


HOLD = ("import fcntl, os, sys, time; f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
        "open(sys.argv[2], 'w').write(str(os.getpid())); time.sleep(60)")


@needs_skill
def test_outside_writer_banner(web, tmp_path):
    app, c = web
    slug = create(c)
    r = app.review(slug)
    (r / "cache").mkdir(exist_ok=True)
    pidf = tmp_path / "holder.pid"
    q = shlex.quote
    # started through a shell that exits at once, so the holder's parent is launchd: not the app's process tree
    subprocess.run(["/bin/sh", "-c", f"{q(sys.executable)} -c {q(HOLD)} {q(str(r / 'cache' / 'litrev.lock'))} "
                                     f"{q(str(pidf))} >/dev/null 2>&1 &"], check=True)
    deadline = time.time() + 10
    while not (pidf.exists() and pidf.read_text()) and time.time() < deadline:
        time.sleep(0.05)
    pid = int(pidf.read_text())
    try:
        out = c.get(f"/api/reviews/{slug}").json()["outside"]
        assert out["lock"] == [pid] and "working on this review" in out["message"]
    finally:
        os.kill(pid, signal.SIGKILL)


def test_steps_after_the_current_one_are_upcoming_not_done():
    # live check 2026-10-11: steps 4-5 showed green checkmarks during step 0 (status calls them done: nothing included)
    from litrev_app.api_reviews import steps
    status = {"rows": [{"step": 0, "key": "scope", "state": "done"}, {"step": 0, "key": "taxonomy", "state": "todo"},
                       {"step": 4, "key": "fulltext", "state": "done"}, {"step": 5, "key": "details", "state": "done"}]}
    out = {s["step"]: s["state"] for s in steps(status, current=0)}
    assert out[0] == "todo" and out[4] == "upcoming" and out[5] == "upcoming"
    assert {s["step"]: s["state"] for s in steps(status)}[4] == "done"      # skill-mode reviews: status as is
