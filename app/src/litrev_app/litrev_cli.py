"""Run litrev commands for one review (as a subprocess: SystemExit and module globals make in-process calls unsafe)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


def init_review(python: Path, skill: Path, path: Path, *, slug: str, purpose: str, seed: str, cap: int, years: int,
                env: dict) -> subprocess.CompletedProcess:
    """`init` from the New review form (spike S9: categories are set later by the step-0 session)."""
    return subprocess.run([str(python), str(skill / "scripts" / "litrev.py"), "init", "--path", str(path),
                           "--slug", slug, "--purpose", purpose, "--seed", seed, "--max-papers", str(cap),
                           "--years", str(years)], capture_output=True, text=True, env=env)


class Litrev:
    def __init__(self, python: Path, skill: Path, review: Path, env: dict, *, sleep=time.sleep,
                 lock_retries: int = 10, lock_wait: float = 6.0):
        self.python, self.skill, self.review, self.env = python, skill, review, env
        self.sleep, self.lock_retries, self.lock_wait = sleep, lock_retries, lock_wait

    def run(self, argv: list[str]) -> subprocess.CompletedProcess:
        """One litrev command on this review; waits and retries while another writer holds the review's lock."""
        cmd = [str(self.python), str(self.skill / "scripts" / "litrev.py"), *argv, "--review", str(self.review)]
        for i in range(self.lock_retries + 1):
            p = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
            if p.returncode != 0 and "is locked by" in (p.stderr or "") and i < self.lock_retries:
                self.sleep(self.lock_wait)
                continue
            return p
        return p

    def status(self) -> dict:
        p = self.run(["status", "--json"])
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"rows": [], "next": None, "error": (p.stderr or p.stdout)[-500:]}

    def meta(self) -> dict:
        return json.loads((self.review / "meta.json").read_text())
