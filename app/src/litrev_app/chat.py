"""Chat with Claude about one review (M2). Each message is one `claude -p` run in the review folder: the first
creates a persisted session (--session-id), later ones --resume it (spike S10). While a step or refresh session
works on the review, or the review is auto-advancing, a message runs read-only (chat_ro); otherwise it may change
the review (chat_rw) as the review's only app-side writer. Replies stream to the browser as bus events."""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import briefs
from .ask_mcp import read_json, write_json_atomic
from .claude_cmd import chat_argv, write_chat_files
from .outcome import classify
from .runner import Limits, Session, SessionSpec, run_session
from .store import SID_RE, Store
from .writers import Busy, review_busy, wake_scheduler

COST_IS_CUMULATIVE = True           # spike S10: a resumed -p run reports the conversation's running total
RESUME_MISSING_RE = re.compile(r"No conversation found", re.I)
RUN_FROM_RE = re.compile(r"^[ \t]*RUN FROM STEP ([1-7])[ \t]*$", re.M)
CHAT_LIMITS = Limits(init_s=120, idle_s=900, hard_s=3600, poll_s=0.25, grace_int=10, grace_term=10)
CHAT_MAX_TURNS = 80
SHOWN_ACTIVITY = {"litrev", "litrev_done", "agent_start", "agent_end", "denied", "limit"}
NEW_CONVERSATION = ("(The earlier conversation could not be resumed, so this is a new conversation. The messages "
                    "above stay here, but Claude no longer remembers them.)\n\n")
_SVC_LOCK = threading.Lock()


def message_cost(total: float | None, prev_total: float | None, cumulative: bool | None = None) -> float | None:
    """This message's cost. Per-run totals are the cost; cumulative ones are the difference from the previous
    message of the same session (a smaller total means a fresh session, so it is the cost)."""
    cumulative = COST_IS_CUMULATIVE if cumulative is None else cumulative
    if total is None:
        return None
    if cumulative and prev_total is not None and total >= prev_total:
        return round(total - prev_total, 6)
    return total


def stream_text(e: dict) -> str | None:
    """The main agent's text delta in a --include-partial-messages event. None for anything else, including a
    subagent's deltas (their text is not the reply)."""
    if not isinstance(e, dict) or e.get("type") != "stream_event" or e.get("parent_tool_use_id"):
        return None
    ev = e.get("event") or {}
    delta = ev.get("delta") or {}
    if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
        return delta.get("text") or None
    return None


def assistant_texts(e: dict) -> list[str]:
    """The main agent's complete text blocks in an `assistant` event."""
    if not isinstance(e, dict) or e.get("type") != "assistant" or e.get("parent_tool_use_id"):
        return []
    content = (e.get("message") or {}).get("content") or []
    return [c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]


def run_from(text: str) -> int | None:
    m = RUN_FROM_RE.search(text or "")
    return int(m.group(1)) if m else None


def strip_run_from(text: str) -> str:
    return RUN_FROM_RE.sub("", text or "").strip()


def _reply(o, texts: list[str], partial: str, stderr: str) -> str:
    text = "\n\n".join(t.strip() for t in texts if t.strip()) or partial.strip() or (o.result_text or "").strip()
    if o.kind == "ok":
        return text or "(Claude finished without a reply.)"
    if o.kind == "max_turns":
        return (text + "\n\n" if text else "") + "(Claude reached the turn limit for one message. Send “continue” to let it go on.)"
    if o.kind == "stopped":
        return (text + "\n\n" if text else "") + "(Stopped.)"
    if o.kind == "usage_limit":
        when = datetime.fromtimestamp(o.resets_at).strftime("%a %H:%M") if o.resets_at else "later"
        return f"Claude usage limit reached; it resets {when}. Send the message again then."
    if o.kind == "auth":
        return "Claude Code isn't logged in with your Claude account. Open Doctor to log in, then send it again."
    tail = (stderr or "").strip().splitlines()[-1:] or [""]
    return f"The chat session failed ({o.kind}). {text or tail[0]}".strip()


def status_line(engine, review: Path) -> str:
    """The app's run state for a chat message. A refresh's screening session is in engine.live too, so ask the
    writer lock first (M2 review: the chat was told "Nothing is running" during a refresh)."""
    busy = ("a refresh is writing this review" if engine.writers.holder(str(review)) == "refresh"
            else review_busy(engine, review))
    return briefs.app_status(Store(review).load(), busy)


def _session_id(state) -> str | None:
    for e in reversed([*state.inits, *state.results]):
        if e.get("session_id"):
            return e["session_id"]
    return None


class ChatBusy(Exception):
    pass


class ChatStore:
    """<review>/cache/app/chat.json: the app's own record of the conversation (Claude keeps the full transcript)."""

    def __init__(self, review: Path):
        self.path = review / "cache" / "app" / "chat.json"

    def load(self) -> dict:
        d = read_json(self.path)
        if isinstance(d, dict) and isinstance(d.get("messages"), list):
            return d
        return {"sid": None, "messages": [], "cost_usd": 0.0, "last_total_usd": None}

    def save(self, d: dict) -> None:
        write_json_atomic(self.path, d)

    def reset(self) -> None:
        if self.path.exists():
            self.path.rename(self.path.with_name(f"chat-{int(time.time())}.json"))


class ChatService:
    def __init__(self, engine, bus, *, limits: Limits = CHAT_LIMITS, clock=time.time):
        self.engine, self.bus, self.limits, self.clock = engine, bus, limits, clock
        self.live: dict[str, Session] = {}
        self.partial: dict[str, str] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def mode(self, review: Path) -> str:
        return "read_only" if review_busy(self.engine, review) else "read_write"

    def is_running(self, review: Path) -> bool:
        with self._lock:
            return str(review) in self._running

    def running_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._running)

    def history(self, review: Path) -> dict:
        key = str(review)
        return {**ChatStore(review).load(), "running": self.is_running(review),
                "partial": self.partial.get(key, ""), "mode": self.mode(review)}

    def send(self, review: Path, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("Type a message first.")
        key = str(review)
        with self._lock:
            if key in self._running:
                raise ChatBusy("Claude is still answering the previous message.")
            self._running.add(key)
        try:
            profile, release = self._claim(review)
        except BaseException:
            with self._lock:
                self._running.discard(key)
            raise
        threading.Thread(target=self._run, args=(review, text, profile, release), daemon=True).start()
        return {"profile": profile}

    def _claim(self, review: Path):
        """chat_rw as the review's writer when no step-type session works on it, else chat_ro (counted so an update
        waits). Raises writers.Busy while an update runs."""
        w, key = self.engine.writers, str(review)
        if not review_busy(self.engine, review) and w.acquire(key, "chat"):
            return "chat_rw", lambda: w.release(key, "chat")
        w.start_reading()
        return "chat_ro", w.stop_reading

    def stop(self, review: Path) -> bool:
        s = self.live.get(str(review))
        if s:
            s.stop("user")
        return bool(s)

    def reset(self, review: Path) -> None:
        if self.is_running(review):
            raise ChatBusy("Wait for Claude's answer first.")
        ChatStore(review).reset()

    def _pub(self, slug: str, event: str, **kw) -> None:
        self.bus.publish(slug, {"type": "chat", "event": event, **kw})

    def _attempt(self, review: Path, text: str, profile: str, sid: str, resume: bool, n: int):
        cfg, key, slug = self.engine.cfg, str(review), review.name
        litrev = cfg.skill / "scripts" / "litrev.py"
        files = write_chat_files(cfg.appdir / "sessions" / f"chat-{sid}", profile=profile, litrev=litrev,
                                 skill=cfg.skill, review=review, step5_model=cfg.step5_model)
        status = status_line(self.engine, review)
        prompt = briefs.chat_prompt(skill=cfg.skill, review=review, root=cfg.root, read_only=profile == "chat_ro",
                                    status=status)
        argv = chat_argv(cfg.claude, text, sid, files, resume=resume, skill=cfg.skill, review=review, litrev=litrev,
                         system_prompt=prompt, model=cfg.model, effort=cfg.effort, max_turns=CHAT_MAX_TURNS)
        if cfg.claude.suffix == ".py":           # tests: the fake claude is a script
            argv = [str(cfg.python), *argv]
        store, tag = Store(review), f"chat-{sid[:8]}-{n:03d}"
        spec = SessionSpec(tag, argv, self.engine.env(review), review, store.events_path(tag), store.stderr_path(tag))
        texts: list[str] = []

        def on_event(e: dict) -> None:
            d = stream_text(e)
            if d:
                self.partial[key] = self.partial.get(key, "") + d
                self._pub(slug, "delta", text=d)
            texts.extend(assistant_texts(e))

        def on_activity(a) -> None:
            if a.kind in SHOWN_ACTIVITY:
                self._pub(slug, "activity", activity=a.to_dict())

        session = Session(spec, caffeinate=cfg.caffeinate, limits=self.limits)
        self.live[key] = session
        state, code, reason = run_session(session, on_activity, self.limits, on_event=on_event)
        outcome = classify(state, code, reason, datetime.now().astimezone())
        err = spec.stderr_path.read_text(errors="replace") if spec.stderr_path.exists() else ""
        return state, outcome, texts, err

    def _run(self, review: Path, text: str, profile: str, release) -> None:
        key, slug, chats = str(review), review.name, ChatStore(review)
        msg = None
        try:
            chat = chats.load()
            user = {"role": "user", "text": text, "ts": self.clock()}
            chat["messages"].append(user)
            chats.save(chat)
            self.partial[key] = ""
            self._pub(slug, "start", profile=profile, message=user)
            old = chat.get("sid")
            ok = isinstance(old, str) and bool(SID_RE.match(old))  # chat.json is session-writable; sid builds paths (F2)
            sid, resume = (old, True) if ok else (str(uuid.uuid4()), False)
            n = len(chat["messages"])
            state, outcome, texts, err = self._attempt(review, text, profile, sid, resume, n)
            note = ""
            if resume and outcome.kind != "ok" and RESUME_MISSING_RE.search(f"{err} {outcome.result_text}"):
                note, sid, resume = NEW_CONVERSATION, str(uuid.uuid4()), False
                self.partial[key] = ""
                state, outcome, texts, err = self._attempt(review, text, profile, sid, False, n)
            if state.inits:
                new = _session_id(state)
                chat["sid"] = new if isinstance(new, str) and SID_RE.match(new) else sid
            total = state.cost_usd
            cost = message_cost(total, chat.get("last_total_usd") if resume else None)
            if total is not None:
                chat["last_total_usd"] = total
            body = note + _reply(outcome, texts, self.partial.get(key, ""), err)
            msg = {"role": "assistant", "text": strip_run_from(body), "ts": self.clock(), "cost_usd": cost,
                   "total_cost_usd": total, "profile": profile, "outcome": outcome.kind,
                   "run_from": run_from(body) if profile == "chat_rw" and outcome.kind == "ok" else None,
                   "error": outcome.kind not in ("ok", "max_turns", "stopped")}
            chat["messages"].append(msg)
            chat["cost_usd"] = round((chat.get("cost_usd") or 0.0) + (cost or 0.0), 6)
            chats.save(chat)
        except Exception as e:
            msg = {"role": "assistant", "text": f"The app hit an error running this message: {e}", "ts": self.clock(),
                   "cost_usd": None, "total_cost_usd": None, "profile": profile, "outcome": "app_error",
                   "run_from": None, "error": True}
            try:
                c = chats.load()
                c["messages"].append(msg)
                chats.save(c)
            except Exception:
                pass
        finally:
            release()
            self.live.pop(key, None)
            self.partial.pop(key, None)
            with self._lock:
                self._running.discard(key)
            self._pub(slug, "done", message=msg, mode=self.mode(review), cost_usd=ChatStore(review).load()["cost_usd"])


def service(app) -> ChatService:
    with _SVC_LOCK:
        svc = getattr(app, "chat_service", None)
        if svc is None:
            svc = ChatService(app.engine, app.bus)
            app.chat_service = svc
        return svc


def _review(app, slug: str) -> Path | None:
    r = app.engine.review_path(slug)
    return r if (r / "meta.json").exists() else None


def get_chat(app, req, slug):
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review named {slug}"}, {}
    return 200, service(app).history(r), {}


def post_chat(app, req, slug):
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review named {slug}"}, {}
    try:
        out = service(app).send(r, str((req.json or {}).get("text") or ""))
    except ValueError as e:
        return 400, {"error": str(e)}, {}
    except (ChatBusy, Busy) as e:
        return 409, {"error": str(e)}, {}
    return 202, {"ok": True, **out}, {}


def post_stop(app, req, slug):
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review named {slug}"}, {}
    return 200, {"ok": service(app).stop(r)}, {}


def post_reset(app, req, slug):
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review named {slug}"}, {}
    try:
        service(app).reset(r)
    except ChatBusy as e:
        return 409, {"error": str(e)}, {}
    return 200, {"ok": True}, {}


def post_run_steps(app, req, slug):
    """Queue steps n..7 after a change made in chat (the reply's RUN FROM STEP n line)."""
    r = _review(app, slug)
    if r is None:
        return 404, {"error": f"No review named {slug}"}, {}
    step = (req.json or {}).get("step")
    if not isinstance(step, int) or not 1 <= step <= 7:
        return 400, {"error": "step must be 1-7"}, {}
    why = review_busy(app.engine, r) or ("Claude is still answering" if service(app).is_running(r) else None)
    phase = Store(r).load().phase
    if not why and phase in ("waiting_user", "paused"):
        why = f"the review is {phase.replace('_', ' ')}"
    if why:
        return 409, {"error": f"Not now: {why}."}, {}
    step = app.engine.rerun_start(r, step)
    st = app.engine.queue_from(r, step, message=f"after a change made in chat (steps {step}-7)",
                               notes=briefs.rerun_note(step, "a change made in the Ask Claude chat"))
    wake_scheduler(app)
    return 200, {"ok": True, "state": asdict(st)}, {}


def register(router) -> None:
    base = r"^/api/reviews/(?P<slug>[a-z0-9-]+)/chat"
    router.add("GET", base + "$", get_chat)
    router.add("POST", base + "$", post_chat)
    router.add("POST", base + "/stop$", post_stop)
    router.add("POST", base + "/reset$", post_reset)
    router.add("POST", base + "/run-steps$", post_run_steps)
