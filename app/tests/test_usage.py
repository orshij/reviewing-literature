import json
import sys
from pathlib import Path
from types import SimpleNamespace

from m2help import FAKE, FAST, FakeRouter
from litrev_app import runner, usage
from litrev_app.runner import Session, SessionSpec, run_session

FX = Path(__file__).parent / "fixtures" / "streams"
NOW = 1_790_000_000.0


def _info(five, week, status="allowed", reset_in=3600):
    return {"status": status, "resetsAt": NOW + reset_in, "rateLimitType": "five_hour", "unifiedWindows": {
        "five_hour": {"utilization": five, "resetsAt": NOW + reset_in},
        "seven_day": {"utilization": week, "resetsAt": NOW + 5 * 86400}}}


def test_meter_reads_the_s2a_fixture():
    m = usage.Meter()
    want = None
    for line in (FX / "s2a-matrix.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            m.observe(e)
            if e.get("type") == "rate_limit_event":
                want = e["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"]
    s = m.snapshot()
    assert s["known"] and want is not None
    assert s["five_hour"]["utilization"] == want or s["five_hour"]["reset"]


def test_warning_over_80_percent_and_reset_windows():
    m = usage.Meter(clock=lambda: NOW)
    m.observe({"type": "rate_limit_event", "rate_limit_info": _info(0.1, 0.83)})
    s = m.snapshot()
    assert s["seven_day"]["utilization"] == 0.83 and "83%" in s["warn_new_review"] and s["limited"] is None
    m.observe({"type": "rate_limit_event", "rate_limit_info": _info(0.1, 0.5)})
    assert m.warning() is None
    m.observe({"type": "rate_limit_event", "rate_limit_info": _info(1.0, 0.5, status="rejected")})
    s = m.snapshot()
    assert s["limited"]["type"] == "five_hour" and "until" in s["warn_new_review"]
    later = usage.Meter(clock=lambda: NOW + 7 * 86400)
    later.observe({"type": "rate_limit_event", "rate_limit_info": _info(0.9, 0.95)})
    s = later.snapshot()
    assert s["seven_day"] == {"utilization": None, "resets_at": NOW + 5 * 86400, "reset": True}
    assert s["warn_new_review"] is None
    m.observe({"type": "assistant"})                      # other events are ignored
    assert m.snapshot()["known"]


def test_persisted_and_bound(tmp_path):
    m = usage.Meter(tmp_path / "usage.json", clock=lambda: NOW)
    m.observe({"type": "rate_limit_event", "rate_limit_info": _info(0.2, 0.3)})
    again = usage.Meter(tmp_path / "usage.json", clock=lambda: NOW)
    assert again.snapshot()["five_hour"]["utilization"] == 0.2
    free = usage.Meter(clock=lambda: NOW)
    free.observe({"type": "rate_limit_event", "rate_limit_info": _info(0.4, 0.3)})
    free.bind(tmp_path / "other.json")
    assert json.loads((tmp_path / "other.json").read_text())["info"]["unifiedWindows"]["five_hour"]["utilization"] == 0.4


def test_register_hooks_every_session_once_and_serves_the_route(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "METER", usage.Meter())
    monkeypatch.setattr(runner, "EVENT_HOOKS", [])
    rt = FakeRouter()
    usage.register(rt)
    usage.register(FakeRouter())
    assert runner.EVENT_HOOKS == [usage.METER.observe]
    app = SimpleNamespace(cfg=SimpleNamespace(appdir=tmp_path / "appdir"))
    code, body, _ = rt.call(app, "GET", "/api/usage")
    assert code == 200 and body["known"] is False
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps({"events": [{"type": "rate_limit_event",
                                          "rate_limit_info": _info(0.42, 0.1, reset_in=10**9)}]}))   # real clock
    spec = SessionSpec("S1", [sys.executable, str(FAKE), "-p", "x", "--session-id", "S1"],
                       {"FAKE_CLAUDE_SCENARIO": str(sc), "PATH": "/usr/bin:/bin"}, tmp_path,
                       tmp_path / "ev" / "S1.jsonl", tmp_path / "ev" / "S1.stderr")
    run_session(Session(spec, caffeinate=False), None, FAST)
    body = rt.call(app, "GET", "/api/usage")[1]
    assert body["known"] and body["five_hour"]["utilization"] == 0.42
    assert (tmp_path / "appdir" / "usage.json").exists()


def test_usage_is_a_registered_feature():
    from litrev_app import server
    assert "litrev_app.usage" in server.FEATURES
