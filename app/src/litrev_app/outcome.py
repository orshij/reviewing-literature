"""Classify how a session ended, and when a Claude usage limit resets (formats from spike S4)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .events import LIMIT_TEXT_RE, StreamState

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), 1)}
DOWS = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
RESET_RE = re.compile(
    r"resets\s+(?:(?P<dow>mon|tue|wed|thu|fri|sat|sun)\w*\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(?P<day>\d{1,2})(?:\s+at)?\s*)?"
    r"(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm))?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)+)\))?", re.I)


@dataclass
class Outcome:
    kind: str
    resets_at: float | None = None
    result_text: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    denials: int = 0
    exit_code: int | None = None
    subtype: str | None = None
    api_error_status: int | None = None
    limit_seen: bool = False


def parse_reset(text: str, now: datetime) -> datetime | None:
    """The next moment matching 'resets 8:40pm (Asia/Jerusalem)', 'resets Oct 4 at 12am', 'resets Mon 12:00am',
    'resets Oct 2'. `now` must be timezone-aware; without a zone in the text, now's zone is used."""
    m = RESET_RE.search(text or "")
    if not m or not (m.group("h") or m.group("day")):
        return None
    tz = now.tzinfo
    if m.group("tz"):
        try:
            tz = ZoneInfo(m.group("tz"))
        except Exception:
            pass
    local = now.astimezone(tz)
    h = int(m.group("h") or 0) % 12 + (12 if (m.group("ap") or "").lower() == "pm" else 0)
    mi = int(m.group("m") or 0)
    if m.group("day"):
        try:
            cand = local.replace(month=MONTHS[m.group("mon").lower()[:3]], day=int(m.group("day")), hour=h, minute=mi,
                                 second=0, microsecond=0)
        except ValueError:
            return None
        return cand if cand > local else cand.replace(year=cand.year + 1)
    cand = local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if m.group("dow"):
        cand += timedelta(days=(DOWS[m.group("dow").lower()[:3]] - cand.weekday()) % 7)
        return cand if cand > local else cand + timedelta(days=7)
    return cand if cand > local else cand + timedelta(days=1)


def _resets(st: StreamState, text: str, now: datetime) -> float | None:
    r = (st.limit_rejected or {}).get("resetsAt")
    if r:
        return float(r)
    for t in ((st.limit_rejected or {}).get("text"), text):
        d = parse_reset(t or "", now)
        if d:
            return d.timestamp()
    return None


def classify(st: StreamState, exit_code: int | None, stop_reason: str | None, now: datetime) -> Outcome:
    """Classify on the LAST result (a session with backgrounded subagents emits several, spike S3)."""
    res = st.results[-1] if st.results else {}
    o = Outcome(kind="ok", result_text=res.get("result") or st.last_text or "", cost_usd=st.cost_usd,
                num_turns=res.get("num_turns"), denials=len(st.denials), exit_code=exit_code,
                subtype=res.get("subtype"), api_error_status=res.get("api_error_status"))
    if stop_reason == "user":
        o.kind = "stopped"
        return o
    if stop_reason == "watchdog":
        o.kind = "watchdog"
        return o
    if not st.inits:
        o.kind = "startup_failed"
        return o
    text_hit = bool(LIMIT_TEXT_RE.search(o.result_text)) and (res.get("is_error") or not res)
    if st.limit_rejected or text_hit:
        o.limit_seen = True
        o.resets_at = _resets(st, o.result_text, now)
        if text_hit or not res or res.get("is_error"):
            o.kind = "usage_limit"
            return o
    if not res:
        o.kind = "interrupted"
        return o
    if res.get("is_error"):
        status, low = res.get("api_error_status"), o.result_text.lower()
        if status in (401, 403) or "/login" in low or "authenticat" in low:
            o.kind = "auth"
        elif status in (429, 500, 502, 503, 504, 529) or "overloaded" in low or "refreshing" in low:
            o.kind = "transient"
        else:
            o.kind = "error"
        return o
    if o.subtype == "error_max_turns":
        o.kind = "max_turns"
    elif o.subtype not in (None, "success"):
        o.kind = "error"
    return o
