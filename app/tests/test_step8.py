"""Step 8 (the narrated video) in the app: the last step, the step rail, the opt-in on the form and on a finished
review, the video and caption routes, the doctor's video check and its voice-install fix."""
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from apphelpers import make_skill
from litrev_app import api_reviews as ar
from litrev_app import claude_cmd as cc
from litrev_app import doctor_checks as dc
from litrev_app import doctor_fix as fx
from litrev_app.orchestrator import decide_next, last_step
from litrev_app.outcome import Outcome
from litrev_app.store import RunState, Store
from web_client import SKILL, make_app, start

needs_skill = pytest.mark.skipif(not (SKILL / "scripts" / "litrev.py").exists(), reason="needs the skill")
ROWS7 = [{"step": 7, "key": "render", "state": "done"}, {"step": 7, "key": "publish", "state": "todo"}]
ROWS8 = ROWS7 + [{"step": 8, "key": "plan", "state": "todo"}, {"step": 8, "key": "script", "state": "todo"}]


# ---------- engine ----------

def test_step_8_runs_only_for_a_review_that_opted_in():
    assert last_step({"rows": ROWS7}) == 7 and last_step({"rows": ROWS8}) == 8
    done7 = decide_next(RunState(step=7, phase="running"), Outcome("ok"), {"outcome": "done"}, {"rows": ROWS7}, None, None, 1.0)
    assert done7.phase == "done" and "hub" in done7.message
    to8 = decide_next(RunState(step=7, phase="running"), Outcome("ok"), {"outcome": "done"}, {"rows": ROWS8}, None, None, 1.0)
    assert (to8.step, to8.phase) == (8, "running")
    rows8_done = ROWS7 + [{"step": 8, "key": k, "state": "done"} for k in ("plan", "script", "video")] + \
        [{"step": 8, "key": "audio", "state": "warn"}]                 # audio off its planned time: a warning, not open
    done8 = decide_next(RunState(step=8, phase="running"), Outcome("ok"), {"outcome": "done"}, {"rows": rows8_done}, None, None, 1.0)
    assert done8.phase == "done" and "Video tab" in done8.message


def test_steps_rail_has_a_ninth_step_only_after_the_opt_in():
    assert len(ar.steps({"rows": ROWS7})) == 8
    st = ar.steps({"rows": ROWS8})
    assert len(st) == 9 and st[8]["state"] == "todo" and "video" in st[8]["title"]


def test_sessions_may_run_long_bash_commands_under_the_idle_watchdog(tmp_path):
    from litrev_app.runner import Limits
    env = cc.child_env({}, litrev_home=tmp_path, venv_bin=Path("/v"), claude_dir=Path("/c"), concurrency=3)
    assert int(env["BASH_MAX_TIMEOUT_MS"]) == cc.BASH_MAX_TIMEOUT_MS < Limits().idle_s * 1000


# ---------- API ----------

@pytest.fixture
def web(tmp_path):
    app = make_app(tmp_path)
    httpd, client = start(app)
    yield app, client
    httpd.shutdown()
    httpd.server_close()


@needs_skill
def test_new_review_form_records_the_video_opt_in(web):
    app, c = web
    r = c.post("/api/reviews", {"topic": "Depth", "purpose": "Pick a model", "cap": 12, "video": True, "video_minutes": 10})
    assert r.status == 201, r.text
    meta = json.loads((Path(app.cfg.root) / "reviews" / r.json()["slug"] / "meta.json").read_text())
    assert meta["presentation"]["wanted"] is True and meta["presentation"]["minutes"] == 10 and meta["presentation"]["by"] == "user"
    d = c.get(f"/api/reviews/{r.json()['slug']}").json()
    assert d["video"]["wanted"] is True and d["video"]["exists"] is False and "USD" in d["video"]["cost"]
    assert len(d["steps"]) == 9
    plain = c.post("/api/reviews", {"topic": "Other", "purpose": "p", "cap": 12}).json()["slug"]
    assert "presentation" not in json.loads((Path(app.cfg.root) / "reviews" / plain / "meta.json").read_text())
    assert c.post("/api/reviews", {"topic": "T", "purpose": "p", "video": True, "video_minutes": 90}).status == 422


@needs_skill
def test_make_video_on_a_finished_review_queues_step_8(web):
    app, c = web
    slug = c.post("/api/reviews", {"topic": "Depth", "purpose": "Pick a model", "cap": 12}).json()["slug"]
    r = Path(app.cfg.root) / "reviews" / slug
    store = Store(r)
    store.save(RunState(step=0, phase="running", sid="abc"))
    assert c.post(f"/api/reviews/{slug}/video", {"minutes": 20}).status == 409          # a run is going
    store.save(RunState(step=7, phase="done"))
    out = c.post(f"/api/reviews/{slug}/video", {"minutes": 20})
    assert out.status == 200, out.text
    assert json.loads((r / "meta.json").read_text())["presentation"]["minutes"] == 20
    st = store.load()
    assert st.phase == "running" and "video" in st.message   # a fresh review: its open steps come first, then 8


@needs_skill
def test_video_and_captions_are_served_with_byte_ranges(web):
    app, c = web
    slug = c.post("/api/reviews", {"topic": "Depth", "purpose": "Pick a model", "cap": 12}).json()["slug"]
    assert c.get(f"/r/{slug}/video").status == 404 and c.get(f"/r/{slug}/video.vtt").status == 404
    d = Path(app.cfg.root) / "reviews" / slug / "rendered" / "presentation"
    d.mkdir(parents=True)
    (d / f"{slug}.mp4").write_bytes(bytes(range(256)) * 4)                              # 1024 bytes
    (d / f"{slug}.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n")
    full = c.get(f"/r/{slug}/video")
    assert full.status == 200 and len(full.raw) == 1024 and full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "video/mp4"
    part = c.get(f"/r/{slug}/video", headers={"Range": "bytes=10-19"})
    assert part.status == 206 and part.raw == bytes(range(10, 20)) and part.headers["content-range"] == "bytes 10-19/1024"
    tail = c.get(f"/r/{slug}/video", headers={"Range": "bytes=-4"})
    assert tail.status == 206 and tail.raw == bytes(range(252, 256))
    assert c.get(f"/r/{slug}/video", headers={"Range": "bytes=5000-"}).status == 416
    cap = c.get(f"/r/{slug}/video.vtt")
    assert cap.status == 200 and cap.headers["content-type"].startswith("text/vtt") and "Hello" in cap.raw.decode()
    assert c.get(f"/r/{slug}/video", auth=False).status == 401                          # the cookie is required
    v = c.get(f"/api/reviews/{slug}").json()["video"]
    assert v["exists"] is True and v["captions"] is True and v["slides"] is False


@needs_skill
def test_the_slides_deck_is_a_download(web):
    app, c = web
    slug = c.post("/api/reviews", {"topic": "Depth", "purpose": "Pick a model", "cap": 12}).json()["slug"]
    assert c.get(f"/r/{slug}/video.pptx").status == 404
    d = Path(app.cfg.root) / "reviews" / slug / "rendered" / "presentation"
    d.mkdir(parents=True)
    (d / f"{slug}.mp4").write_bytes(b"\0" * 16)
    (d / f"{slug}.pptx").write_bytes(b"PK\x03\x04deck")
    got = c.get(f"/r/{slug}/video.pptx")
    assert got.status == 200 and got.raw == b"PK\x03\x04deck"
    assert got.headers["content-type"].endswith("presentationml.presentation")
    assert got.headers["content-disposition"] == f'attachment; filename="{slug}.pptx"'
    assert c.get(f"/r/{slug}/video.pptx", auth=False).status == 401
    assert c.get(f"/api/reviews/{slug}").json()["video"]["slides"] is True


# ---------- doctor ----------

def _inputs(tmp_path, **env):
    return dc.Inputs(claude=Path("/opt/claude"), skill=make_skill(tmp_path / "skill"), python=Path("/venv/bin/python"),
                     root=tmp_path / "root", appdir=tmp_path / "appdir", home=tmp_path / "home",
                     env={"PATH": "/usr/bin:/bin", **env})


def test_video_check_names_what_is_missing_and_offers_the_voice_install(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    inp = _inputs(tmp_path, LITREV_CHROME=str(chrome), LITREV_VOICE_HOME=str(tmp_path / "tts"))
    none = dc.check_video(inp, which=lambda n, path=None: None)
    assert none.level == "warn" and "ffmpeg" in none.message and "voice tools" in none.message
    assert none.action == "Install voice tools" and "brew install ffmpeg" in none.fix and none.ok
    (tmp_path / "tts" / "bin").mkdir(parents=True)
    (tmp_path / "tts" / "bin" / "python").write_text("")
    half = dc.check_video(inp, which=lambda n, path=None: "/opt/homebrew/bin/" + n)
    assert "voice tools" in half.message                                                # installed, models not yet
    (tmp_path / "tts" / ".litrev-warm").write_text("2026-10-21")
    ok = dc.check_video(inp, which=lambda n, path=None: "/opt/homebrew/bin/" + n, run=_probe_run(0, ""),
                        probes=dc.VoiceProbes(spawn=lambda f: f()))
    assert ok.level == "ok" and ok.action is None
    running = dc.check_video(inp, which=lambda n, path=None: None, job={"state": "running", "progress": "downloading"})
    assert running.detail.get("installing") and "downloading" in running.message


def _probe_run(code: int, stderr: str, calls: list | None = None):
    def run(argv, env=None, timeout=60):
        if calls is not None:
            calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, code, "", stderr)
    return run


def _warm_venv(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    (tmp_path / "tts" / "bin").mkdir(parents=True)
    (tmp_path / "tts" / "bin" / "python").write_text("")
    (tmp_path / "tts" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (tmp_path / "tts" / ".litrev-warm").write_text("2026-10-25")
    return _inputs(tmp_path, LITREV_CHROME=str(chrome), LITREV_VOICE_HOME=str(tmp_path / "tts"))


def test_video_check_runs_the_voice_once_per_venv_state_and_offers_the_repair(tmp_path):
    # a teammate's venv got preshed 3.0.14 (2026-10-25): its files were all there, but spaCy could not load
    inp, calls = _warm_venv(tmp_path), []
    probes, which = dc.VoiceProbes(spawn=lambda f: f()), (lambda n, path=None: "/opt/homebrew/bin/" + n)
    err = "Traceback ...\n  File \"spacy/tokenizer.pyx\", line 239\nOverflowError: Python int too large to convert to C ssize_t\n"
    bad = dc.check_video(inp, which=which, run=_probe_run(1, err, calls), probes=probes)
    assert bad.level == "warn" and bad.action == "Install voice tools" and bad.ok
    assert "do not run" in bad.message and "OverflowError: Python int too large" in bad.message
    assert str(tmp_path / "tts") in bad.fix and bad.detail["voice"] is False
    argv = calls[0]
    assert argv[0] == str(tmp_path / "tts" / "bin" / "python") and argv[1:3] == ["-I", "-c"]
    assert "KPipeline" in argv[3] and "model=False" in argv[3] and "mlx_whisper" in argv[3]
    dc.check_video(inp, which=which, run=_probe_run(1, err, calls), probes=probes)
    assert len(calls) == 1                                                  # the same venv is not probed again
    sp = tmp_path / "tts" / "lib" / "python3.12" / "site-packages"
    (sp / "preshed-3.0.15.dist-info").mkdir()                               # `pip install 'preshed>=3.0.15'`
    os.utime(sp, ns=(sp.stat().st_mtime_ns + 10**9,) * 2)
    good = dc.check_video(inp, which=which, run=_probe_run(0, "", calls), probes=probes)
    assert len(calls) == 2 and good.level == "ok" and good.action is None
    probes.forget_done()                                                    # Check again probes once more
    dc.check_video(inp, which=which, run=_probe_run(0, "", calls), probes=probes)
    assert len(calls) == 3


def test_voice_probe_runs_in_the_background_while_the_row_asks_to_be_polled(tmp_path):
    inp, pending, calls = _warm_venv(tmp_path), [], []
    probes, which = dc.VoiceProbes(spawn=pending.append), (lambda n, path=None: "/opt/homebrew/bin/" + n)
    first = dc.check_video(inp, which=which, run=_probe_run(0, "", calls), probes=probes)
    assert first.level == "warn" and first.detail.get("building") and "Checking" in first.message and not calls
    dc.check_video(inp, which=which, run=_probe_run(0, "", calls), probes=probes)
    assert len(pending) == 1                                                # one probe at a time
    probes.forget_done()
    dc.check_video(inp, which=which, run=_probe_run(0, "", calls), probes=probes)
    assert len(pending) == 1                                                # a running probe is kept
    pending[0]()
    assert dc.check_video(inp, which=which, run=_probe_run(0, "", calls), probes=probes).level == "ok"
    installing = dc.check_video(inp, which=which, job={"state": "running", "progress": "x"},
                                run=_probe_run(0, "", calls), probes=dc.VoiceProbes(spawn=pending.append))
    assert installing.detail.get("installing") and len(pending) == 1        # no probe while voice-setup runs


def test_voice_install_is_a_background_job_that_runs_voice_setup(tmp_path):
    inp = _inputs(tmp_path)
    jobs, gate, calls = fx.Jobs(), threading.Event(), []

    def run(argv, env=None, timeout=60):
        calls.append(([str(a) for a in argv], env, timeout))
        gate.wait(10)
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    assert fx.fix("video", inp, jobs=jobs, run=run)["started"]
    assert not fx.fix("video", inp, jobs=jobs, run=run)["started"]                     # one install at a time
    gate.set()
    jobs.join("video", 10)
    argv, env, timeout = calls[0]
    assert argv[-1] == "voice-setup" and argv[-2].endswith("scripts/litrev.py") and timeout >= 1800
    assert ".local/bin" in env["PATH"] and "LITREV_APP_REVIEW" not in env               # the app's own call, not a session's
    assert jobs.snapshot()["video"]["state"] == "done"
