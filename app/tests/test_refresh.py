import json
import os
import subprocess
import time

import pytest

from m2help import DATE, FakeRouter, HAS_SKILL, argv_log, candidate, make_app, mini_review, review_env_log, scenario
from litrev_app import refresh
from litrev_app.litrev_cli import Litrev
from litrev_app.store import RunState, Store

needs_skill = pytest.mark.skipif(not HAS_SKILL, reason="needs the skill")
REAL_RUN = Litrev.run
REPORT = f"""# Refresh {DATE} - t

3 replays run (0 failed), 1 skipped, 2 new candidates. Nothing was screened, ranked or published.

## New candidates

### P003 - New paper C (2026)

- Link: https://arxiv.org/abs/2610.00011
- Found by: term Q001, citing walk of P001

### P004 - New paper D (2026)

- Link: https://arxiv.org/abs/2610.00012
- Found by: no recorded path
"""
PLAN = {"dry_run": True, "requests": 3, "skipped": [{"what": "Q002", "reason": "term status deferred"}],
        "replays": [{"kind": "refresh-keyword", "label": "depth", "source": "arxiv", "since": "2026-09-01"},
                    {"kind": "refresh-keyword", "label": "metric depth", "source": "openalex", "since": "2026-09-01"},
                    {"kind": "refresh-citing", "label": "papers citing P001", "source": "openalex", "since": "2026-09-01"}]}
ASK = {"kind": "refresh", "question": "Which of these new papers should join the review?", "context": "2 new",
       "proposal": {"papers": [
           {"id": "P003", "title": "New paper C", "year": 2026, "paths": ["term Q001"], "recommend": "include",
            "why": "on scope"},
           {"id": "P004", "title": "New paper D", "year": 2026, "paths": [], "recommend": "exclude", "why": "stereo"}]}}
SCREEN = {"events": [{"$ask": ASK}, {"$report": {"step": "refresh", "summary": "recommended"}}]}


def _stub(monkeypatch, new=2):
    """`refresh` needs the network: the stub leaves what a real one leaves (candidates, last_refresh, REFRESH.md)."""
    def run(self, argv):
        if argv[:1] == ["refresh"] and "--dry-run" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(PLAN), "")
        if argv[:1] == ["refresh"]:
            if new:
                f = self.review / "cache" / "refresh-new.json"
                f.write_text(json.dumps([candidate("C", 1), candidate("D", 2)][:new]))
                assert REAL_RUN(self, ["import", "--table", "papers", "--file", str(f)]).returncode == 0
                (self.review / "rendered").mkdir(exist_ok=True)
                (self.review / "rendered" / "REFRESH.md").write_text(REPORT)
            REAL_RUN(self, ["set-meta", "--field", "last_refresh", "--json", json.dumps(
                {"date": DATE, "replays": 3, "failed": 0, "new": new, "report": "rendered/REFRESH.md"})])
            return subprocess.CompletedProcess(argv, 0, f"refresh: 3 replays (0 failed, 1 skipped), {new} new", "")
        return REAL_RUN(self, argv)
    monkeypatch.setattr(Litrev, "run", run)


def _router():
    rt = FakeRouter()
    refresh.register(rt)
    return rt


def _stage(app, since, *stages):
    return app.bus.wait_for(lambda e: e.get("type") == "refresh" and (e.get("state") or {}).get("stage") in stages,
                            timeout=30, since=since)["state"]


def test_parse_refresh_report():
    assert refresh.parse_refresh_report(REPORT) == {"P003": ["term Q001", "citing walk of P001"], "P004": []}


@needs_skill
def test_refresh_with_the_answer_given_in_session(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, SCREEN)
    _stub(monkeypatch)
    app = make_app(tmp_path)
    r = mini_review(app)                                   # cap 2, 2 included: at the cap
    rt = _router()
    code, s, _ = rt.call(app, "POST", "/api/reviews/t/refresh/plan")
    assert code == 200 and s["stage"] == "planned" and s["plan"]["requests"] == 3
    assert (s["plan"]["keyword"], s["plan"]["citing"], s["plan"]["skipped"]) == (2, 1, 1)
    n = len(app.bus.events)
    assert rt.call(app, "POST", "/api/reviews/t/refresh/start")[0] == 200
    store, end = Store(r), time.time() + 30
    while (store.ask() or {}).get("status") != "open":
        assert time.time() < end
        time.sleep(0.05)
    s = rt.call(app, "GET", "/api/reviews/t/refresh")[1]
    assert [c["id"] for c in s["candidates"]] == ["P003", "P004"] and s["candidates"][0]["recommend"] == "include"
    assert s["candidates"][0]["paths"] == ["term Q001", "citing walk of P001"] and s["cap"] == {"used": 2, "max": 2}
    code, err, _ = rt.call(app, "POST", "/api/reviews/t/refresh/answer", {"approve": ["P003"]})
    assert code == 409 and "cap" in err["error"]
    assert rt.call(app, "POST", "/api/reviews/t/refresh/answer", {"approve": ["P999"], "raise_cap": True})[0] == 400
    code, s, _ = rt.call(app, "POST", "/api/reviews/t/refresh/answer", {"approve": ["P003"], "raise_cap": True})
    assert code == 200, s
    final = _stage(app, n, "queued", "done", "failed")
    assert final["stage"] == "queued" and final["approved"] == ["P003"] and final["rejected"] == ["P004"]
    P = json.loads((r / "data" / "papers.json").read_text())
    assert (P["P003"]["status"], P["P004"]["status"]) == ("included", "excluded") and P["P003"]["manual"]
    meta = json.loads((r / "meta.json").read_text())
    assert meta["caps"]["max_papers"] == 3
    assert {(d["kind"], d.get("by")) for d in meta["decisions"]} >= {("cap", "user"), ("screen", "user")}
    st = store.load()
    assert (st.step, st.phase) == (4, "running") and "do not run select" in st.notes.lower()
    assert any(a.startswith("refresh: approved P003") for a in st.answers)
    app.refresh_service.threads[str(r)].join(10)           # the writer lock is released when the thread ends
    assert app.scheduler.woken == 1 and app.engine.writers.holder(str(r)) is None
    argv = argv_log(tmp_path)[-1]
    brief = argv[argv.index("-p") + 1]
    assert "P003, P004" in brief and 'kind "refresh"' in brief and "no merge-screening" in brief
    assert "--no-session-persistence" in argv and store.sessions()[-1]["step"] == "refresh"
    assert review_env_log(tmp_path)[-1] == os.path.realpath(r)          # security F1: the screening session too


@needs_skill
def test_parked_question_is_answered_later_and_finishes_once(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, SCREEN)
    _stub(monkeypatch)
    app = make_app(tmp_path, wait_s=0.5)
    r = mini_review(app, cap=5)                            # room for both
    rt = _router()
    rt.call(app, "POST", "/api/reviews/t/refresh/plan")
    n = len(app.bus.events)
    rt.call(app, "POST", "/api/reviews/t/refresh/start")
    s = _stage(app, n, "waiting_user", "failed", "done")
    assert s["stage"] == "waiting_user" and Store(r).ask()["status"] == "parked"
    assert Store(r).load().phase == "done"                 # nothing queued before the answer
    code, s, _ = rt.call(app, "POST", "/api/reviews/t/refresh/answer", {"approve": ["P003", "P004"]})
    assert code == 200 and s["stage"] == "queued" and s["question"] is None
    st = Store(r).load()
    assert (st.step, st.phase) == (4, "running") and app.scheduler.woken == 1
    assert rt.call(app, "POST", "/api/reviews/t/refresh/answer", {"approve": []})[0] == 409
    assert app.scheduler.woken == 1


@needs_skill
def test_refusals_and_nothing_new(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, SCREEN)
    _stub(monkeypatch, new=0)
    app = make_app(tmp_path)
    r = mini_review(app)
    rt = _router()
    assert rt.call(app, "GET", "/api/reviews/nope/refresh")[0] == 404
    assert rt.call(app, "POST", "/api/reviews/t/refresh/start")[0] == 409          # no plan yet
    Store(r).save(RunState(step=3, phase="running"))
    assert rt.call(app, "POST", "/api/reviews/t/refresh/plan")[0] == 409
    Store(r).save(RunState(step=7, phase="done"))
    assert app.engine.writers.acquire(str(r), "chat")
    code, err, _ = rt.call(app, "POST", "/api/reviews/t/refresh/plan")
    assert code == 409 and "chat" in err["error"]
    app.engine.writers.release(str(r), "chat")
    rt.call(app, "POST", "/api/reviews/t/refresh/plan")
    n = len(app.bus.events)
    rt.call(app, "POST", "/api/reviews/t/refresh/start")
    s = _stage(app, n, "done", "failed", "waiting_user", "queued")
    assert s["stage"] == "done" and "Nothing new" in s["message"] and argv_log(tmp_path) == []


BLOCKED = {"events": [{"$write": {"path": "{review}/cache/app/blocked.json", "content": json.dumps(
    {"reason": "review locked by enrich pid 9", "retry_after": None, "command": "screen-input"})}},
    {"$report": {"step": "refresh", "outcome": "blocked", "summary": "locked"}}]}


@needs_skill
def test_a_blocked_screening_says_why_and_can_be_retried(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, BLOCKED)
    _stub(monkeypatch)
    app = make_app(tmp_path)
    r = mini_review(app, cap=5)
    rt = _router()
    rt.call(app, "POST", "/api/reviews/t/refresh/plan")
    n = len(app.bus.events)
    rt.call(app, "POST", "/api/reviews/t/refresh/start")
    s = _stage(app, n, "failed", "waiting_user", "done", "queued")
    assert s["stage"] == "failed" and "review locked by enrich pid 9" in s["message"]
    assert not (r / "cache" / "app" / "blocked.json").exists()          # taken once, not re-read later
    app.refresh_service.threads[str(r)].join(10)
    n = len(app.bus.events)
    code, s, _ = rt.call(app, "POST", "/api/reviews/t/refresh/screen")
    assert code == 200 and s["new"] == ["P003", "P004"]
    assert _stage(app, n, "failed", "waiting_user", "done", "queued")["stage"] == "failed"


def test_refresh_is_a_registered_feature():
    from litrev_app import server
    assert "litrev_app.refresh" in server.FEATURES


@needs_skill
def test_a_refresh_cut_off_by_an_app_restart_can_be_retried(tmp_path):
    """M2 review: the stage lived on in refresh.json while its thread died with the app, so the tab showed
    "Claude is reading…" forever with no button."""
    app = make_app(tmp_path)
    r = mini_review(app)
    svc = refresh.service(app)
    svc._save(r, stage="screening", date=DATE, new=["P003"], message="Claude is reading the 1 new paper(s)")
    refresh.on_start(app)
    d = svc.load(r)
    assert d["stage"] == "failed" and "stopped" in d["message"]
    svc._save(r, stage="fetching")
    Store(r).put_ask({"id": "A1", "kind": "refresh", "status": "parked", "question": "q", "proposal": {"papers": []}})
    refresh.on_start(app)
    assert svc.load(r)["stage"] == "waiting_user"


@needs_skill
def test_refresh_offers_only_the_new_papers(tmp_path):
    """M2 review: an id from Claude's proposal that was already in the review became a candidate, and leaving it
    unticked recorded it as excluded."""
    from m2help import import_papers
    app = make_app(tmp_path)
    r = mini_review(app)
    import_papers(app, r, [candidate("C", 1), candidate("D", 2)])
    svc = refresh.service(app)
    svc._save(r, stage="waiting_user", date=DATE, new=["P003", "P004"])
    ask = {"kind": "refresh", "status": "open", "proposal": {"papers": [
        {"id": "P001", "recommend": "exclude", "why": "already in"}, {"id": "P003", "recommend": "include"}]}}
    assert [c["id"] for c in svc.candidates(r, ask, DATE)] == ["P003", "P004"]


def test_refresh_brief_screens_with_the_screener_agent():
    """Cost pass (2026-10-12): screening runs on a Sonnet agent type, litrev-screener (skill v1.21.0); the refresh
    session dispatches it, and Doctor requires the skill to define it."""
    from pathlib import Path
    from litrev_app import briefs, doctor_checks
    b = briefs.refresh_brief(skill=Path("/sk"), review=Path("/r/x"), root=Path("/r"), sid="S", parallel=3,
                             date="2026-10-12", ids=["P1"])
    assert "litrev-screener" in b and "litrev-reader" not in b
    assert "litrev-screener" in doctor_checks.AGENTS


@needs_skill
def test_a_refresh_waits_for_a_publish_awaiting_approval(tmp_path, monkeypatch):
    scenario(monkeypatch, tmp_path, SCREEN)
    _stub(monkeypatch, new=0)
    app = make_app(tmp_path)
    r = mini_review(app)
    rt = _router()
    Store(r).save(RunState(step=7, phase="done"))
    (Store(r).dir / "publish.json").write_text(json.dumps({"state": "waiting_approval"}))
    code, err, _ = rt.call(app, "POST", "/api/reviews/t/refresh/plan")
    assert code == 409 and "publish" in err["error"]
