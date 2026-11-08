"""Prompt for a step session (app-mode.md is the contract it points at)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from string import Template

STEP_TITLES = {0: "init, scope, surveys and taxonomy", 1: "search, screening, select, snowball and coverage",
               2: "tasks", 3: "datasets, benchmarks and metrics", 4: "paper records, full texts and enrichment",
               5: "method details", 6: "KPIs, ranking, clusters and explainers",
               7: "adjacent topics, the review report, validate and render",
               8: "narrated video: chapter plan, scripts, voice and MP4"}
TEMPLATE = Template((Path(__file__).parent / "briefs" / "step.txt").read_text())


def step_brief(*, skill: Path, review: Path, root: Path, step: int, sid: str, parallel: int, new: dict | None = None,
               cont: str | None = None, answers=(), notes: str = "") -> str:
    extra = []
    if new:
        extra.append(f'User\'s request: seed "{new["seed"]}", purpose "{new["purpose"]}". The review is already '
                     "initialised with their caps; set arXiv categories with set-meta --field categories after "
                     "checking the seed.")
    if cont:
        extra.append(f"This continues an earlier session ({cont}); its work is on disk. Check status first.")
    if answers:
        extra.append("Answered since the last session (already recorded; do not ask again): " + " | ".join(answers))
    if notes:
        extra.append(f"Standing instruction for this run: {notes}")
    return TEMPLATE.substitute(skill=skill, review=review, root=root, step=step, title=STEP_TITLES[step], sid=sid,
                               parallel=parallel, extra="\n".join(extra)).replace("\n\n\n", "\n\n")


def rerun_note(step: int, why: str) -> str:
    """Standing instruction for steps `step`..7 after papers were screened in by hand (refresh, chat). None for a run
    from step 1: select, snowball and coverage are that step's own work."""
    if step < 2:
        return ""
    return (f"Steps {step}-7 run again after {why}. Work from step {step} on only: do not run select, snowball or "
            "coverage, even though `status` shows step-1 rows as stale after a manual `screen`; the person decided "
            "those papers by hand.")


CHAT_TEMPLATE = Template((Path(__file__).parent / "briefs" / "chat.txt").read_text())
CHAT_MODES = {
    False: "Mode: read-write. You may change this review now, one writing command at a time.",
    True: ("Mode: read-only. One of the app's step sessions is working on this review, so only read-only commands "
           "and file reads work right now. If the teammate asks for a change, say exactly what you would do and ask "
           "them to send it again when the tab's Read-only badge is gone."),
}


def app_status(st, busy: str | None) -> str:
    """What the app is doing with the review as a message is sent (RunState, writers.review_busy). Live M2 check:
    without it, chat guessed "nothing is running" during step 4."""
    msg = (st.message or "").removeprefix("Paused: ").strip()
    tail = f" ({msg})" if msg else ""
    if busy and "refresh" in busy:
        now = "a refresh is checking for new papers"
    elif st.phase == "running":
        now = f"step {st.step} is running in a step session{tail}" if st.sid else f"step {st.step} starts next{tail}"
    elif st.phase == "waiting_user":
        now = f"step {st.step} is waiting for the teammate to answer a question in the Review tab{tail}"
    elif st.phase == "paused":
        at = f" until {datetime.fromtimestamp(st.paused_until).strftime('%H:%M')}" if st.paused_until else ""
        now = f"step {st.step} is paused{at}; it resumes by itself{tail}"
    elif st.phase == "failed":
        now = f"step {st.step} failed and waits for the teammate to press Retry{tail}"
    elif st.phase == "done":
        now = "every step is done; the review is ready to publish"
    else:
        now = f"Nothing is running; step {st.step} is next when the teammate presses Run{tail}"
    return ("App status when this message was sent (the app's own record: trust it over anything said earlier in "
            f"this conversation, and don't try to check processes): {now}.")


def chat_prompt(*, skill: Path, review: Path, root: Path, read_only: bool, status: str = "") -> str:
    """The --append-system-prompt of a chat message (not app-mode: chat asks in its reply)."""
    mode = CHAT_MODES[read_only] + (f"\n{status}" if status else "")
    return CHAT_TEMPLATE.substitute(skill=skill, review=review, root=root, mode=mode)


REFRESH_TEMPLATE = Template((Path(__file__).parent / "briefs" / "refresh.txt").read_text())


def refresh_brief(*, skill: Path, review: Path, root: Path, sid: str, parallel: int, date: str,
                  ids: list[str]) -> str:
    """Brief of the short session that screens a refresh's new candidates and asks once (kind refresh)."""
    return REFRESH_TEMPLATE.substitute(skill=skill, review=review, root=root, sid=sid, parallel=parallel, date=date,
                                       n=len(ids), ids=", ".join(ids))

