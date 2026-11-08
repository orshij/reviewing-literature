import json
import threading

from litrev_app.bus import GLOBAL, EventBus, sse_format, sse_stream


def test_publish_numbers_events_and_keeps_only_activities():
    bus = EventBus(clock=lambda: 42.0)
    a = bus.publish("r1", {"type": "activity", "kind": "litrev", "title": "Searching"})
    b = bus.publish("r1", {"type": "state", "phase": "running"})
    assert (a["seq"], b["seq"]) == (1, 2) and a["slug"] == "r1" and a["ts"] == 42.0
    assert [e["title"] for e in bus.recent("r1")] == ["Searching"] and bus.recent("r2") == []


def test_ring_keeps_the_newest():
    bus = EventBus(ring=3)
    for i in range(5):
        bus.publish("r", {"type": "activity", "title": str(i)})
    assert [e["title"] for e in bus.recent("r")] == ["2", "3", "4"]


def test_subscribers_and_the_global_stream():
    bus = EventBus()
    one, glob = bus.subscribe("r1"), bus.subscribe(GLOBAL)
    bus.publish("r1", {"type": "activity", "title": "x"})
    bus.publish("r2", {"type": "state", "phase": "done"})
    bus.publish("r1", {"type": "notify", "title": "t", "body": "b"})
    assert one.get(0.1)["title"] == "x" and one.get(0.1)["type"] == "notify" and one.get(0.05) is None
    assert [e["type"] for e in (glob.get(0.1), glob.get(0.1))] == ["state", "notify"] and glob.get(0.05) is None
    one.close()
    bus.publish("r1", {"type": "activity", "title": "y"})
    assert one.get(0.05) is None


def test_seed_fills_only_an_empty_ring():
    bus = EventBus()
    bus.seed("r", [{"type": "activity", "title": "old"}])
    bus.seed("r", [{"type": "activity", "title": "ignored"}])
    assert [e["title"] for e in bus.recent("r")] == ["old"] and bus.recent("r")[0]["seq"] == 1


def test_sse_format_is_one_data_line():
    raw = sse_format({"type": "activity", "seq": 7, "title": "a\nb"}).decode()
    assert raw.startswith("id: 7\ndata: ") and raw.endswith("\n\n") and raw.count("\n") == 3
    assert json.loads(raw.split("data: ", 1)[1])["title"] == "a\nb"


def test_sse_stream_replays_then_streams_then_pings():
    bus, stop = EventBus(), threading.Event()
    bus.publish("r", {"type": "activity", "title": "before"})
    gen = sse_stream(bus, "r", ping_s=0.05, stop=stop)
    assert next(gen).startswith(b"retry:")
    assert b'"before"' in next(gen)
    bus.publish("r", {"type": "activity", "title": "live"})
    assert b'"live"' in next(gen)
    assert next(gen) == b": ping\n\n"
    gen.close()
    assert bus.publish("r", {"type": "activity"}) and not bus._subs.get("r")
