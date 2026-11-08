"""In-memory event bus: a ring buffer of recent activities per review, and live subscribers (the SSE streams)."""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from collections import deque

GLOBAL = "*"
GLOBAL_TYPES = ("state", "notify")
RING = 300


class Subscription:
    def __init__(self, bus: "EventBus", slug: str):
        self.bus, self.slug = bus, slug
        self.q: queue.Queue = queue.Queue(maxsize=2000)

    def get(self, timeout: float) -> dict | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.bus._unsubscribe(self)


class EventBus:
    def __init__(self, ring: int = RING, clock=time.time):
        self.ring, self.clock = ring, clock
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._rings: dict[str, deque] = {}
        self._subs: dict[str, set] = {}

    def publish(self, slug: str, event: dict) -> dict:
        with self._lock:
            ev = {**event, "slug": slug, "seq": next(self._seq), "ts": event.get("ts") or self.clock()}
            if ev.get("type") == "activity":
                self._rings.setdefault(slug, deque(maxlen=self.ring)).append(ev)
            targets = list(self._subs.get(slug, ()))
            if ev.get("type") in GLOBAL_TYPES and slug != GLOBAL:
                targets += list(self._subs.get(GLOBAL, ()))
        for s in targets:
            try:
                s.q.put_nowait(ev)
            except queue.Full:              # a stalled browser misses events; it refetches when it reconnects
                pass
        return ev

    def seed(self, slug: str, events: list[dict]) -> None:
        """Fill an empty ring (after an app restart, from the last session's events file). Nobody is notified."""
        with self._lock:
            if self._rings.get(slug):
                return
            ring = self._rings.setdefault(slug, deque(maxlen=self.ring))
            for e in events:
                ring.append({**e, "slug": slug, "seq": next(self._seq), "ts": e.get("ts")})

    def recent(self, slug: str) -> list[dict]:
        with self._lock:
            return list(self._rings.get(slug, ()))

    def subscribe(self, slug: str) -> Subscription:
        s = Subscription(self, slug)
        with self._lock:
            self._subs.setdefault(slug, set()).add(s)
        return s

    def _unsubscribe(self, s: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(s.slug)
            if subs is not None:
                subs.discard(s)
                if not subs:
                    del self._subs[s.slug]


def sse_format(ev: dict) -> bytes:
    """One SSE message: the event id is the bus seq (the browser drops seqs it has seen), the data is one JSON line."""
    return f"id: {ev.get('seq', '')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()


def sse_stream(bus: EventBus, slug: str, *, backlog: bool = True, ping_s: float = 15.0, stop=None):
    """Body generator for an SSE response. Subscribes before replaying the ring so nothing falls in between."""
    sub = bus.subscribe(slug)
    try:
        yield b"retry: 3000\n\n"
        if backlog:
            for ev in bus.recent(slug):
                yield sse_format(ev)
        while not (stop is not None and stop.is_set()):
            ev = sub.get(ping_s)
            yield sse_format(ev) if ev is not None else b": ping\n\n"
    finally:
        sub.close()
