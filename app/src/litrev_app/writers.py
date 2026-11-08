"""App-side coordination (M2). At most one app-side writer per review: a step session, a refresh, or a chat message
that may change the review (chat_rw). Read-only chat messages are counted, and an Update freezes everything (a
plugin update replaces the skill folder that sessions read from). litrev's own file lock (cache/litrev.lock) still
guards each command; this layer keeps the app from starting two writers that would fight over it."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .store import Store


class Busy(Exception):
    """A read-only chat message refused because an update is running."""


class WriterLocks:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._held: dict[str, str] = {}      # str(review path) -> "step" | "refresh" | "chat"
        self._readers = 0                    # read-only chat messages in flight
        self._frozen = False                 # an update is running

    def acquire(self, key: str, who: str, timeout: float = 0.0) -> bool:
        """Become the review's writer; wait up to `timeout` s while another writer holds it or an update runs."""
        end = time.monotonic() + timeout
        with self._cv:
            while key in self._held or self._frozen:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            self._held[key] = who
            return True

    def release(self, key: str, who: str) -> None:
        with self._cv:
            if self._held.get(key) == who:
                del self._held[key]
                self._cv.notify_all()

    def holder(self, key: str) -> str | None:
        with self._cv:
            return self._held.get(key)

    def start_reading(self) -> None:
        """A session that writes nothing (chat_ro) starts. Refused while an update runs."""
        with self._cv:
            if self._frozen:
                raise Busy("The app is updating; send the message again in a minute.")
            self._readers += 1

    def stop_reading(self) -> None:
        with self._cv:
            self._readers = max(0, self._readers - 1)
            self._cv.notify_all()

    def freeze(self) -> str | None:
        """Stop new sessions for an update. None when frozen, else what is still running."""
        with self._cv:
            if self._frozen:
                return "an update is already running"
            busy = [f"a {who} on {Path(k).name}" for k, who in sorted(self._held.items())]
            if self._readers:
                busy.append(f"{self._readers} read-only chat message(s)")
            if busy:
                return ", ".join(busy)
            self._frozen = True
            return None

    def thaw(self) -> None:
        with self._cv:
            self._frozen = False
            self._cv.notify_all()

    @property
    def frozen(self) -> bool:
        with self._cv:
            return self._frozen


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def review_busy(engine, review: Path) -> str | None:
    """Why a step-type session counts as working on this review now (chat then runs read-only), else None: a live
    session, a step or refresh holding the writer lock, or the review auto-advancing (RunState phase running)."""
    key = str(review)
    if key in engine.live:
        return "a session is running on this review"
    who = engine.writers.holder(key)
    if who in ("step", "refresh"):
        return f"a {who} is writing this review"
    if Store(review).publish_waiting():
        return "a publish is waiting for your approval in Terminal"
    if Store(review).load().phase == "running":
        return "the review is running its steps"
    return None


def wake_scheduler(app) -> None:
    """Nudge M1b's scheduler after a step was queued on disk (RunState phase running), when it offers wake()."""
    fn = getattr(getattr(app, "scheduler", None), "wake", None)
    if callable(fn):
        fn()
