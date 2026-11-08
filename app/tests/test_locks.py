import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from litrev_app import locks

PS = """    1     0 /sbin/launchd
  500     1 /Applications/Claude.app/Contents/MacOS/Claude
  600     1 -zsh
  601   600 claude
  700     1 /usr/local/bin/claude
  800     1 python3
  801   800 /Users/me/.local/bin/claude
  802   801 /bin/zsh
  803   802 python3
"""


def fake_run(review: Path, holders=(), cwd_map=None):
    cwd_map = cwd_map or {}

    def run(argv, **kw):
        if argv[0].endswith("ps"):
            return subprocess.CompletedProcess(argv, 0, PS, "")
        if argv[0].endswith("lsof") and argv[1] == "-t":
            return subprocess.CompletedProcess(argv, 0 if holders else 1, "".join(f"{p}\n" for p in holders), "")
        if argv[0].endswith("lsof"):
            pids = [int(x) for x in argv[argv.index("-p") + 1].split(",")]
            out = "".join(f"p{p}\nfcwd\nn{cwd_map[p]}\n" for p in pids if p in cwd_map)
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(argv)
    return run


def _review(tmp_path):
    r = tmp_path / "reviews" / "t"
    (r / "cache").mkdir(parents=True)
    (r / "cache" / "litrev.lock").write_text("")
    return r


def test_process_table_and_claude_names():
    t = locks.process_table(fake_run(Path("/x")))
    assert t[601] == (600, "claude") and t[500][1].endswith("/Claude")
    assert sorted(p for p, (_, c) in t.items() if os.path.basename(c) == "claude") == [601, 700, 801]


def test_is_ours_walks_parents():
    t = locks.process_table(fake_run(Path("/x")))
    assert locks.is_ours(803, t, {800}) and locks.is_ours(801, t, {801}) and not locks.is_ours(601, t, {800})
    assert not locks.is_ours(99999, t, {800})


def test_outside_claude_open_in_the_review_is_reported(tmp_path):
    r = _review(tmp_path)
    run = fake_run(r, cwd_map={601: str(r / "data"), 700: str(tmp_path), 801: str(r)})
    o = locks.outside_writers(r, {800}, run)
    assert o["claude"] == [601] and o["lock"] == [] and "open in this review" in o["message"]


def test_app_sessions_and_their_children_are_not_reported(tmp_path):
    r = _review(tmp_path)
    run = fake_run(r, holders=[803], cwd_map={801: str(r)})
    assert locks.outside_writers(r, {800}, run) == {"lock": [], "claude": [], "message": None}
    o = locks.outside_writers(r, {9}, run)
    assert o["lock"] == [803] and o["claude"] == [801] and "working on this review" in o["message"]


def test_a_real_lock_held_by_a_child_process(tmp_path):
    r = _review(tmp_path)
    code = ("import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('locked', flush=True); time.sleep(30)")
    p = subprocess.Popen([sys.executable, "-c", code, str(r / "cache" / "litrev.lock")], stdout=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        assert p.stdout.readline().strip() == "locked"
        assert locks.lock_holders(r) == [p.pid]
        assert locks.outside_writers(r, {os.getpid()})["lock"] == []        # a child of this process is ours
        assert locks.outside_writers(r, set())["lock"] == [p.pid]
    finally:
        p.kill(); p.wait()
    assert locks.lock_holders(r) == []


def test_cache_reuses_a_recent_answer(tmp_path):
    r, calls, now = _review(tmp_path), [], [0.0]
    base = fake_run(r)

    def run(argv, **kw):
        calls.append(argv[0]); return base(argv, **kw)
    c = locks.OutsideCache(ttl=5, clock=lambda: now[0])
    c.get(r, set(), run); c.get(r, set(), run)
    n = len(calls)
    now[0] = 6.0
    c.get(r, set(), run)
    assert len(calls) == 2 * n
