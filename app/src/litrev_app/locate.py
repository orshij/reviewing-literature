"""Find what the app runs: the claude binary, the skill folder and the reviews root."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

PLUGIN_ID = "reviewing-literature@litrev"
EXTRA_BIN = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.claude/local")


def find_claude(env_path: str | None = None) -> Path | None:
    """Absolute path of `claude`. An app started from Finder gets a minimal PATH, so common install dirs are added."""
    dirs = (env_path if env_path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    dirs += [os.path.expanduser(d) for d in EXTRA_BIN]
    hit = shutil.which("claude", path=os.pathsep.join(d for d in dirs if d))
    return Path(hit) if hit else None


def plugin_skill_dir(plugin_list_json: str) -> Path | None:
    """The skill folder inside the installed plugin, from `claude plugin list --json` output."""
    try:
        items = json.loads(plugin_list_json)
    except ValueError:
        return None
    for it in items if isinstance(items, list) else []:
        if it.get("id") == PLUGIN_ID and it.get("enabled", True) and it.get("installPath"):
            d = Path(it["installPath"]) / "skills" / "reviewing-literature"
            if (d / "SKILL.md").exists():
                return d
    return None


def find_skill(claude: Path | None, override: str | None = None, home: Path | None = None) -> Path | None:
    """The override, else the installed plugin's skill, else the user's own skill (the author's machine)."""
    if override:
        p = Path(override).expanduser()
        return p if (p / "SKILL.md").exists() else None
    if claude:
        try:
            out = subprocess.run([str(claude), "plugin", "list", "--json"], capture_output=True, text=True,
                                 timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        d = plugin_skill_dir(out)
        if d:
            return d
    p = (home or Path.home()) / ".claude" / "skills" / "reviewing-literature"
    return p if (p / "SKILL.md").exists() else None


def default_root(home: Path | None = None) -> Path:
    """Reviews root: the author's existing folder when it has reviews, else ~/LitReviews."""
    h = home or Path.home()
    author = h / "code" / "literatureReview"
    return author if (author / "reviews").is_dir() else h / "LitReviews"
