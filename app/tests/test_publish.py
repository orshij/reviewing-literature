import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from apphelpers import write_publish_transcript
from litrev_app import publish as pub
from litrev_app.handoff import MAX_HUB_BYTES
from litrev_app.store import RunState, Store
from litrev_app.writers import WriterLocks

SKILL = Path(os.environ.get("LITREV_SKILL", Path.home() / ".claude/skills/reviewing-literature"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeLitrev:
    """Stands in for M1a's Litrev: records argv, applies set-meta to meta.json the way litrev does."""

    def __init__(self, review, render_state="done", render_rc=0, makes_hub=True):
        self.review, self.calls = review, []
        self.render_state, self.render_rc, self.makes_hub = render_state, render_rc, makes_hub

    def meta(self):
        return json.loads((self.review / "meta.json").read_text())

    def status(self):
        return {"rows": [{"step": 7, "key": "render", "state": self.render_state}]}

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "render":
            if self.render_rc == 0 and self.makes_hub:
                (self.review / "rendered").mkdir(exist_ok=True)
                (self.review / "rendered" / "hub.html").write_text("<html>hub</html>")
            return subprocess.CompletedProcess(argv, self.render_rc, "", "render boom" if self.render_rc else "")
        if argv[0] == "set-meta":
            m = self.meta()
            m[argv[argv.index("--field") + 1]] = json.loads(argv[argv.index("--json") + 1])
            m.setdefault("step_log", {})["set-meta:hub_url"] = now_iso()
            (self.review / "meta.json").write_text(json.dumps(m))
        return subprocess.CompletedProcess(argv, 0, "ok", "")


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def setup(tmp_path, hub_url=None, **lit_kw):
    root = tmp_path / "root"
    review = root / "reviews" / "t"
    (review / "rendered").mkdir(parents=True)
    (review / "rendered" / "hub.html").write_text("<html>hub</html>")
    meta = {"slug": "t"}
    if hub_url:
        meta.update(hub_url=hub_url, step_log={"set-meta:hub_url": "2026-01-01T00:00:00Z"})
    (review / "meta.json").write_text(json.dumps(meta))
    lit = FakeLitrev(review, **lit_kw)
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("")
    cfg = SimpleNamespace(root=root, skill=Path("/sk"), claude=claude, python=Path("/venv/bin/python"),
                          appdir=tmp_path / "appdir")
    engine = SimpleNamespace(cfg=cfg, live={}, writers=WriterLocks(), litrev=lambda r: lit,
                             review_path=lambda slug: root / "reviews" / slug)
    procs, holders, opened, clock = [], [], [], Clock()
    p = pub.Publisher(engine, opener=opened.append, procs=lambda: list(procs), lock_holders=lambda path: list(holders),
                      projects=tmp_path / "projects", clock=clock, autowatch=False)
    return SimpleNamespace(review=review, lit=lit, engine=engine, procs=procs, holders=holders, opened=opened,
                           clock=clock, p=p, projects=tmp_path / "projects")


def transcript(e, url):
    sid = e.p.load(e.review)["sid"]
    write_publish_transcript(e.projects, sid, e.review / "rendered" / "hub.html", url)
    return sid


def test_start_renders_first_when_needed_and_opens_the_command(tmp_path):
    e = setup(tmp_path, render_state="stale")
    out = e.p.start(e.review)
    assert e.lit.calls[0] == ["render"] and out["state"] == "waiting_approval"
    st = e.p.load(e.review)
    # security F4: in the app's own folder, beside the session's settings (out of the review, which sessions'
    # Write/Edit rules allow), new (O_EXCL) and owner-only
    cmd = tmp_path / "appdir" / "sessions" / st["sid"] / "publish.command"
    assert e.opened == [cmd] and st["command"] == str(cmd) and cmd.stat().st_mode & 0o777 == 0o700
    assert not (e.review / "cache" / "app" / "publish.command").exists()
    settings = json.loads((tmp_path / "appdir" / "sessions" / st["sid"] / "settings.json").read_text())
    assert settings["disableAllHooks"] is True and st["prev_url"] is None and st["hub_bytes"] == len("<html>hub</html>")
    assert f"--session-id {st['sid']}" in cmd.read_text()
    assert f"LITREV_APP_REVIEW={os.path.realpath(e.review)} " in cmd.read_text()          # security F1
    with pytest.raises(pub.PublishRefused) as again:
        e.p.start(e.review)
    assert again.value.status == 409


def test_start_skips_render_when_it_is_current(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    assert e.lit.calls == []


@pytest.mark.parametrize("case,status,words", [
    ("live", 409, "step session"), ("running", 409, "running"), ("lock", 409, "4242"), ("big", 413, "16 MB"),
    ("render_fails", 500, "render boom"), ("no_hub", 409, "no rendered hub"), ("no_claude", 409, "not found"),
    ("scheduled", 409, "step session"), ("chat", 409, "chat message"), ("refresh", 409, "refresh")])
def test_refusals(tmp_path, case, status, words):
    e = setup(tmp_path, render_state="todo" if case in ("render_fails", "no_hub") else "done",
              render_rc=1 if case == "render_fails" else 0, makes_hub=case not in ("no_hub", "big"))
    if case == "live":
        e.engine.live[str(e.review)] = object()
    if case in ("chat", "refresh"):     # security F4: a chat_rw message, or a refresh still fetching (no session yet)
        assert e.engine.writers.acquire(str(e.review), case)
    if case == "running":
        Store(e.review).save(RunState(phase="running"))
    if case == "lock":
        e.holders.append(4242)
    if case == "big":
        with open(e.review / "rendered" / "hub.html", "wb") as f:
            f.truncate(MAX_HUB_BYTES + 1)
    if case == "no_hub":
        (e.review / "rendered" / "hub.html").unlink()
    if case == "no_claude":
        e.engine.cfg.claude = Path("/missing/claude")             # M1b's placeholder when Claude Code is absent
    if case == "scheduled":
        e.p.scheduler = SimpleNamespace(busy=lambda r: True)       # an adopted session from an earlier launch
    with pytest.raises(pub.PublishRefused) as r:
        e.p.start(e.review)
    assert r.value.status == status and words in r.value.message
    assert e.opened == [] and e.p.load(e.review)["state"] == "idle"
    if case == "big":                  # rendered again first; an older skill leaves the page over the limit
        assert e.lit.calls == [["render"]] and "Update" in r.value.message and "paper cap" not in r.value.message


def test_an_over_limit_hub_is_rendered_again_before_publish(tmp_path):
    # skill 1.34.0: render trims the page to fit (or exits 1 naming the parts), so a hub left over the limit by an
    # older render is rendered again instead of refused with advice that does not fit the cause
    e = setup(tmp_path)
    with open(e.review / "rendered" / "hub.html", "wb") as f:
        f.truncate(MAX_HUB_BYTES + 1)
    out = e.p.start(e.review)
    assert e.lit.calls[0] == ["render"] and out["state"] == "waiting_approval"


def test_claude_stores_the_link_then_the_app_rerenders(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    sid = e.p.load(e.review)["sid"]
    e.procs.append((9, f"claude ... --session-id {sid} --settings x"))
    assert e.p.poll_once(e.review)["state"] == "waiting_approval"
    e.lit.run(["set-meta", "--field", "hub_url", "--json", json.dumps("https://claude.ai/artifact/NEW1")])  # step 3
    st = e.p.poll_once(e.review)
    assert st["state"] == "published" and st["url"] == "https://claude.ai/artifact/NEW1" and st["message"].startswith("Published.")
    assert e.lit.calls[-1] == ["render"]


def test_link_from_the_transcript_is_backfilled_when_claude_skipped_set_meta(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    transcript(e, "https://claude.ai/artifact/NEW1")                  # and the window is already closed
    st = e.p.poll_once(e.review)
    assert st["state"] == "published" and "the app did" in st["message"]
    assert ["set-meta", "--field", "hub_url", "--json", '"https://claude.ai/artifact/NEW1"'] in e.lit.calls
    assert e.lit.meta()["hub_url"] == "https://claude.ai/artifact/NEW1" and e.lit.calls[-1] == ["render"]


def test_backfill_waits_while_claude_may_still_store_it(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    sid = transcript(e, "https://claude.ai/artifact/NEW1")
    e.procs.append((9, f"claude --session-id {sid}"))
    assert e.p.poll_once(e.review)["state"] == "waiting_approval"
    e.clock.t += e.p.grace_s
    assert e.p.poll_once(e.review)["state"] == "published"


def test_new_url_on_republish_replaces_the_stored_one(tmp_path):
    e = setup(tmp_path, hub_url="https://claude.ai/artifact/OLD1")
    e.p.start(e.review)
    assert "url https://claude.ai/artifact/OLD1" in Path(e.p.load(e.review)["command"]).read_text()
    transcript(e, "https://claude.ai/artifact/NEW2")
    st = e.p.poll_once(e.review)
    assert st["url"] == "https://claude.ai/artifact/NEW2" and "OLD1" in st["message"]
    assert e.lit.meta()["hub_url"] == "https://claude.ai/artifact/NEW2"


def test_the_old_stored_link_is_not_taken_for_a_new_publish(tmp_path):
    e = setup(tmp_path, hub_url="https://claude.ai/artifact/OLD1")
    e.p.start(e.review)
    e.procs.append((9, f"claude --session-id {e.p.load(e.review)['sid']}"))
    assert e.p.poll_once(e.review)["state"] == "waiting_approval"


def test_window_closed_without_publishing_fails_and_publish_works_again(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    e.procs.append((9, f"claude --session-id {e.p.load(e.review)['sid']}"))
    e.p.poll_once(e.review)
    e.procs.clear()
    st = e.p.poll_once(e.review)
    assert st["state"] == "failed" and "closed before" in st["message"]
    assert e.p.public(e.review)["busy"] is None
    assert e.p.start(e.review)["state"] == "waiting_approval"


def test_window_that_never_starts_times_out(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    assert e.p.poll_once(e.review)["state"] == "waiting_approval"
    e.clock.t += e.p.start_timeout_s + 1
    st = e.p.poll_once(e.review)
    assert st["state"] == "failed" and "did not start" in st["message"]


def test_cancel(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    assert e.p.cancel(e.review)["state"] == "idle"
    assert e.p.poll_once(e.review)["state"] == "idle"


def test_cancel_during_the_rerender_is_not_blocked_and_wins(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)
    gate, entered, plain_run = threading.Event(), threading.Event(), e.lit.run

    def slow_run(argv):
        if argv[0] == "render":
            entered.set()
            gate.wait(10)
        return plain_run(argv)
    e.lit.run = slow_run
    plain_run(["set-meta", "--field", "hub_url", "--json", json.dumps("https://claude.ai/artifact/NEW1")])
    t = threading.Thread(target=e.p.poll_once, args=(e.review,))
    t.start()
    assert entered.wait(10)
    t0 = time.monotonic()
    assert e.p.cancel(e.review)["state"] == "idle" and time.monotonic() - t0 < 2
    gate.set()
    t.join(10)
    assert e.p.load(e.review)["state"] == "idle"


def test_watcher_resumes_after_an_app_restart(tmp_path):
    e = setup(tmp_path)
    e.p.start(e.review)                                    # the first app process stopped before it saw the link
    e.lit.run(["set-meta", "--field", "hub_url", "--json", json.dumps("https://claude.ai/artifact/NEW1")])
    again = pub.Publisher(e.engine, opener=e.opened.append, procs=lambda: [], lock_holders=lambda p: [],
                          projects=e.projects, poll_s=0.01)
    again.ensure_watcher(e.review)
    again.join(e.review, 10)
    assert again.load(e.review)["state"] == "published"


def test_state_changes_go_to_the_bus(tmp_path):
    e = setup(tmp_path)
    events = []
    e.p.bus = SimpleNamespace(publish=lambda slug, ev: events.append((slug, ev)))
    e.p.start(e.review)
    assert events[-1][0] == "t" and events[-1][1]["type"] == "publish"
    assert events[-1][1]["state"] == "waiting_approval"


def test_routes(tmp_path):
    e = setup(tmp_path)
    app = SimpleNamespace(engine=e.engine, bus=None)
    setattr(app, pub.APP_ATTR, e.p)
    routes = []
    pub.register(SimpleNamespace(add=lambda m, pat, h: routes.append((m, pat, h))))
    assert {(m, h) for m, _, h in routes} == {("GET", pub.get_publish), ("POST", pub.post_publish)}
    assert all(re.match(pat, "/api/reviews/t/publish") for _, pat, _ in routes)
    assert not re.match(routes[0][1], "/api/reviews/../publish")
    assert pub.get_publish(app, SimpleNamespace(json=None), slug="nope")[0] == 404
    status, body, _ = pub.post_publish(app, SimpleNamespace(json={}), slug="t")
    assert status == 202 and body["state"] == "waiting_approval"
    status, body, _ = pub.post_publish(app, SimpleNamespace(json={}), slug="t")
    assert status == 409 and "already waiting" in body["error"]
    assert pub.post_publish(app, SimpleNamespace(json={"action": "cancel"}), slug="t")[1]["state"] == "idle"
    assert pub.get_publish(app, SimpleNamespace(json=None), slug="t")[1]["state"] == "idle"


def test_server_registers_publish():
    from litrev_app import server
    assert "litrev_app.publish" in server.FEATURES


@pytest.mark.skipif(not (SKILL / "scripts" / "litrev.py").exists(), reason="needs the skill")
def test_real_litrev_render_set_meta_and_status(tmp_path):
    from litrev_app.orchestrator import AppConfig, Engine
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("")
    eng = Engine(AppConfig(root=tmp_path / "root", skill=SKILL, claude=claude, python=Path(sys.executable),
                           appdir=tmp_path / "appdir", caffeinate=False))
    review = eng.create_review("t", seed="Depth Anything", purpose="test", cap=12, years=3)
    Store(review).save(RunState(phase="idle"))            # create_review leaves it 'running' for the scheduler
    p = pub.Publisher(eng, opener=lambda c: None, procs=lambda: [], lock_holders=lambda path: [],
                      projects=tmp_path / "projects", autowatch=False)
    p.start(review)                                       # status says render todo, so it renders first
    hub = review / "rendered" / "hub.html"
    assert hub.exists()
    write_publish_transcript(tmp_path / "projects", p.load(review)["sid"], hub, "https://claude.ai/artifact/NEW1")
    assert p.poll_once(review)["state"] == "published"
    meta = json.loads((review / "meta.json").read_text())
    assert meta["hub_url"] == "https://claude.ai/artifact/NEW1" and "set-meta:hub_url" in meta["step_log"]
    rows = {r["key"]: r["state"] for r in eng.litrev(review).status()["rows"]}
    assert rows["publish"] == "done" and rows["render"] == "done"
    assert "https://claude.ai/artifact/NEW1" in (review / "rendered" / "REVIEW.md").read_text()


def test_a_link_stored_by_another_session_is_not_taken(tmp_path):
    # M1c review: meta_url took any set-meta stamped after start; a refused republish that still ran set-meta with the
    # old URL (or another open window) showed "Published." for a publish that never happened
    e = setup(tmp_path)
    e.p.start(e.review)
    sid = e.p.load(e.review)["sid"]
    d = e.projects / "proj"; d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "publish"}}) + "\n")
    e.procs.append((9, f"claude ... --session-id {sid}"))
    e.lit.run(["set-meta", "--field", "hub_url", "--json", json.dumps("https://claude.ai/artifact/OTHER")])
    assert e.p.poll_once(e.review)["state"] == "waiting_approval"     # this session's transcript shows no publish
    transcript(e, "https://claude.ai/artifact/MINE1")
    e.lit.run(["set-meta", "--field", "hub_url", "--json", json.dumps("https://claude.ai/artifact/MINE1")])
    st = e.p.poll_once(e.review)
    assert st["state"] == "published" and st["url"] == "https://claude.ai/artifact/MINE1"


def test_resume_all_restarts_waiting_watchers(tmp_path):
    # M1c review: after an app restart the watch resumed only when someone opened the Publish panel
    e = setup(tmp_path)
    e.p.start(e.review)
    e.lit.run(["set-meta", "--field", "hub_url", "--json", json.dumps("https://claude.ai/artifact/NEW1")])
    again = pub.Publisher(e.engine, opener=e.opened.append, procs=lambda: [], lock_holders=lambda p: [],
                          projects=e.projects, poll_s=0.01)
    assert again.resume_all() == ["t"]
    again.join(e.review, 10)
    assert again.load(e.review)["state"] == "published"


def test_server_runs_feature_start_hooks(monkeypatch):
    import types
    from litrev_app import server
    seen = []
    mod = types.SimpleNamespace(on_start=lambda app: seen.append(app))
    monkeypatch.setattr(server.importlib, "import_module", lambda name: mod)
    server.start_features("APP", ["x.y"])
    assert seen == ["APP"]


def test_a_publish_sid_that_is_not_a_uuid_fails_the_publish(tmp_path, monkeypatch):
    """publish.json is in the review folder, which sessions can write: its sid must not reach find_transcript."""
    e = setup(tmp_path)
    e.p.start(e.review)
    st = e.p.load(e.review)
    st["sid"] = "../../outside/t"
    e.p.save(e.review, st)
    monkeypatch.setattr(pub, "find_transcript", lambda *a: (_ for _ in ()).throw(AssertionError("searched " + a[1])))
    st = e.p.poll_once(e.review)
    assert st["state"] == "failed" and "Press Publish" in st["message"]
