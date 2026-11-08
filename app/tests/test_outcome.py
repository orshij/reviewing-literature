import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from litrev_app.events import StreamState
from litrev_app.outcome import classify, parse_reset

J = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 10, 18, 0, tzinfo=J)          # a Saturday


def test_parse_reset_formats_from_real_transcripts():
    assert parse_reset("You've hit your session limit · resets 8:40pm (Asia/Jerusalem)", NOW) == datetime(2026, 10, 10, 20, 40, tzinfo=J)
    assert parse_reset("You've hit your session limit · resets 2:30pm (Asia/Jerusalem)", NOW) == datetime(2026, 10, 11, 14, 30, tzinfo=J)
    assert parse_reset("You've hit your weekly limit · resets Oct 4 at 12am (Asia/Jerusalem)", NOW) == datetime(2027, 10, 4, 0, 0, tzinfo=J)
    assert parse_reset("You've hit your weekly limit · resets Oct 12 at 12am (Asia/Jerusalem)", NOW) == datetime(2026, 10, 12, 0, 0, tzinfo=J)
    mon = parse_reset("You've hit your weekly limit · resets Mon 12:00am", NOW)
    assert mon.weekday() == 0 and (mon.hour, mon.minute) == (0, 0) and timedelta(0) < mon - NOW <= timedelta(days=7)
    assert parse_reset("You've hit your weekly limit · resets Oct 2", NOW) == datetime(2027, 10, 2, 0, 0, tzinfo=J)
    assert parse_reset("You've hit your Opus limit · resets 3:45pm", NOW) == datetime(2026, 10, 11, 15, 45, tzinfo=J)
    assert parse_reset("You've hit your usage limit", NOW) is None


def _st(*events):
    st = StreamState()
    for e in events:
        st.feed(e)
    return st


INIT = {"type": "system", "subtype": "init", "model": "m"}


def test_ok_uses_the_last_of_several_results():
    st = _st(INIT, {"type": "result", "subtype": "success", "is_error": False, "num_turns": 9, "total_cost_usd": 0.01},
             INIT, {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.02, "result": "done"})
    o = classify(st, 0, None, NOW)
    assert o.kind == "ok" and o.num_turns == 3 and o.cost_usd == 0.02 and o.result_text == "done"


def test_rejected_rate_limit_with_error_result_is_usage_limit():
    st = _st(INIT, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1790101800}},
             {"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your session limit · resets 9:30pm (Asia/Jerusalem)"})
    o = classify(st, 0, None, NOW)
    assert o.kind == "usage_limit" and o.resets_at == 1790101800


def test_limit_text_without_event_parses_the_reset():
    st = _st(INIT, {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                    "result": "You've hit your session limit · resets 8:40pm (Asia/Jerusalem)"})
    o = classify(st, 0, None, NOW)
    assert o.kind == "usage_limit" and o.resets_at == datetime(2026, 10, 10, 20, 40, tzinfo=J).timestamp()


def test_subagent_limit_with_ok_main_result_sets_limit_seen():
    st = _st(INIT, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1790101800}},
             {"type": "result", "subtype": "success", "is_error": False, "result": "partly done"})
    o = classify(st, 0, None, NOW)
    assert o.kind == "ok" and o.limit_seen and o.resets_at == 1790101800


def test_is_error_with_success_subtype_is_classified():
    assert classify(_st(INIT, {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
                               "result": "Invalid API key · Please run /login"}), 0, None, NOW).kind == "auth"
    assert classify(_st(INIT, {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 529,
                               "result": "Overloaded"}), 0, None, NOW).kind == "transient"
    assert classify(_st(INIT, {"type": "result", "subtype": "success", "is_error": True, "result": "boom"}), 1, None, NOW).kind == "error"


def test_endings_without_a_result():
    assert classify(_st(), 1, None, NOW).kind == "startup_failed"
    assert classify(_st(INIT), 143, None, NOW).kind == "interrupted"
    assert classify(_st(INIT), 130, "user", NOW).kind == "stopped"
    assert classify(_st(INIT), 130, "watchdog", NOW).kind == "watchdog"
    assert classify(_st(INIT, {"type": "result", "subtype": "error_max_turns", "is_error": False}), 0, None, NOW).kind == "max_turns"
