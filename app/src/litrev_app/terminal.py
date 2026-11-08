"""Run something in a Terminal window through a .command file. `open x.command` needs no Automation permission
(spec: Publish handoff), and the window shows the person what runs."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .claude_cmd import STRIP_ENV

UNSET = (*STRIP_ENV, "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def q(x) -> str:
    return shlex.quote(str(x))


def claude_line(claude: Path, args: list, set_env: dict | None = None) -> str:
    """`exec env -u ANTHROPIC_API_KEY … [K=V …] <claude> <args>`. Terminal starts a login shell whose profile may
    export an API key, and a key always wins over the person's Claude login."""
    sets = "".join(f" {k}={q(v)}" for k, v in (set_env or {}).items())
    return ("exec env " + " ".join(f"-u {k}" for k in UNSET) + sets + f" {q(claude)} "
            + " ".join(q(a) for a in args))


def write_command(path: Path, title: str, lines: list[str], *, fresh: bool = False) -> Path:
    """An executable sh script; `title` becomes a one-line comment. `fresh`: the file must not exist yet (O_EXCL,
    so a file or symlink already there fails the write instead of being run) and is owner-only, 0700 (security F4).
    Doctor's login/update commands rewrite one fixed name, so they keep the plain write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("#!/bin/sh\n# Litrev App: " + " ".join(title.split()) + " (generated; safe to delete)\n"
            + "\n".join(lines) + "\n")
    if fresh:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w") as f:
            os.fchmod(fd, 0o700)                     # whatever the umask
            f.write(text)
        return path
    path.write_text(text)
    path.chmod(0o755)
    return path


def open_command(path: Path) -> None:
    """Open the .command in Terminal through Launch Services (no Automation prompt)."""
    subprocess.run(["/usr/bin/open", str(path)], check=True, capture_output=True, timeout=30)
