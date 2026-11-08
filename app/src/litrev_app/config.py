"""Settings (~/Library/Application Support/LitrevApp/settings.json), API keys in the macOS Keychain, and the
AppConfig the engine runs with."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from . import locate
from .orchestrator import AppConfig

APPDIR = Path.home() / "Library" / "Application Support" / "LitrevApp"
SERVICE = "litrev-app"
KEY_ENV = {"openalex": "OPENALEX_API_KEY", "s2": "S2_API_KEY"}
SECURITY = "/usr/bin/security"
MODEL_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,64}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{1,512}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class Settings:
    root: str = ""                    # reviews root; load() fills it with locate.default_root()
    parallel: int = 3                 # subagents at once inside a session (spec: 429s at ~6+)
    step5_model: str | None = None    # litrev-details model; None keeps the skill's agent default
    model: str | None = None          # main session model; None = Claude Code's default
    effort: str | None = "medium"     # --effort for every session, subagents included; None = Claude Code's default
    mailto: str | None = None         # LITREV_MAILTO for OpenAlex's polite pool
    wait_s: int = 1800                # how long ask_user waits before parking (spike S1)
    skill_override: str | None = None


def load(appdir: Path = APPDIR) -> Settings:
    try:
        d = json.loads((appdir / "settings.json").read_text())
    except (OSError, ValueError):
        d = {}
    known = {f.name for f in fields(Settings)}
    s = Settings(**{k: v for k, v in d.items() if k in known}) if isinstance(d, dict) else Settings()
    if not s.root:
        s.root = str(locate.default_root())
    return s


def save(s: Settings, appdir: Path = APPDIR) -> None:
    appdir.mkdir(parents=True, exist_ok=True)
    p, tmp = appdir / "settings.json", appdir / "settings.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(s), f, indent=1)
    os.replace(tmp, p)


def _int(d: dict, key: str, lo: int, hi: int, label: str) -> int:
    try:
        v = int(d[key])
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return v


def _opt(d: dict, key: str) -> str | None:
    v = d.get(key)
    return (str(v).strip() or None) if v is not None else None


def _only_mine(p: Path) -> None:
    """Raise unless `p` is this user's and no one else can write it (security F4: the app runs the skill's
    scripts/litrev.py as you, so a folder another local user can write is their code running as you)."""
    st = p.stat()
    if st.st_uid != os.getuid():
        raise ValueError(f"{p} belongs to another user. Litrev runs the skill's scripts as you, so use your own copy")
    if st.st_mode & 0o022:
        raise ValueError(f"Other users can change {p}. Litrev runs the skill's scripts as you: run chmod go-w on it")


def _parent_ok(p: Path) -> None:
    """Raise when a parent of the skill folder belongs to another (non-root) user, or others can write it without the
    sticky bit (/tmp and /Users/Shared have it: others can't move your entries there)."""
    st = p.stat()
    if st.st_uid not in (0, os.getuid()):
        raise ValueError(f"{p} belongs to another user. Litrev runs the skill's scripts as you, so use your own copy")
    if st.st_mode & 0o022 and not st.st_mode & 0o1000:
        raise ValueError(f"Other users can change {p}, which holds the skill folder. Litrev runs the skill's scripts "
                         "as you: move the skill or run chmod go-w on that folder")


def update(base: Settings, d: dict) -> Settings:
    """A checked copy of `base` with the Settings form's fields. Raises ValueError with a sentence for the form."""
    s = replace(base)
    if "root" in d:
        root = _opt(d, "root")
        if not root:
            raise ValueError("Choose a folder for your reviews")
        p = Path(root).expanduser()
        if not p.is_absolute():
            raise ValueError("The reviews folder must be a full path, like ~/LitReviews")
        s.root = str(p)
    if "parallel" in d:
        s.parallel = _int(d, "parallel", 1, 6, "Subagents at once")
    if "wait_s" in d:
        s.wait_s = _int(d, "wait_s", 60, 7200, "Question wait (seconds)")
    for key in ("step5_model", "model"):
        if key in d:
            v = _opt(d, key)
            if v and not MODEL_RE.match(v):
                raise ValueError(f"'{v}' is not a model name Claude Code accepts (try opus, sonnet or haiku)")
            setattr(s, key, v)
    if "effort" in d:
        v = _opt(d, "effort")
        if v and v not in EFFORTS:
            raise ValueError(f"Effort must be one of: {', '.join(EFFORTS)}")
        s.effort = v
    if "mailto" in d:
        v = _opt(d, "mailto")
        if v and not EMAIL_RE.match(v):
            raise ValueError("That email address doesn't look right")
        s.mailto = v
    if "skill_override" in d:
        v = _opt(d, "skill_override")
        if v and not (Path(v).expanduser() / "SKILL.md").exists():
            raise ValueError(f"There is no SKILL.md in {v}")
        real = Path(v).expanduser().resolve() if v else None   # kept resolved: a symlink can be re-pointed later
        if real:                     # checked here only: settings.json itself is the app's own 0600 file (F4)
            for f in (real, real / "scripts", real / "scripts" / "litrev.py"):
                if f == real or f.exists():
                    _only_mine(f)
            for f in real.parents:   # whoever can write a parent can swap the folder in after the check
                _parent_ok(f)
        s.skill_override = str(real) if real else None
    return s


class Keychain:
    """OpenAlex / Semantic Scholar keys as generic passwords (service `litrev-app`, account = the env var name).
    Values reach `security` on stdin (`security -i`), never on argv, where `ps` would show them."""

    def __init__(self, keychain: str | None = None, service: str = SERVICE, run=subprocess.run):
        if keychain and '"' in keychain:
            raise ValueError("keychain path must not contain quotes")
        self.keychain, self.service, self.run = keychain, service, run

    def _target(self) -> list[str]:
        return [self.keychain] if self.keychain else []

    @staticmethod
    def _check(env: str) -> None:
        if env not in KEY_ENV.values():
            raise ValueError(f"{env} is not a key the app stores")

    def get(self, env: str) -> str | None:
        self._check(env)
        p = self.run([SECURITY, "find-generic-password", "-a", env, "-s", self.service, "-w", *self._target()],
                     capture_output=True, text=True)
        return (p.stdout.strip() or None) if p.returncode == 0 else None

    def set(self, env: str, value: str) -> None:
        self._check(env)
        value = (value or "").strip()
        if not KEY_RE.match(value):
            raise ValueError("An API key is one word of letters, digits and . _ ~ + / = : - (no spaces or quotes)")
        line = f'add-generic-password -U -a {env} -s {self.service} -w "{value}"'
        if self.keychain:
            line += f' "{self.keychain}"'
        self.run([SECURITY, "-i"], input=line + "\n", capture_output=True, text=True)
        if self.get(env) != value:                  # `security -i` exits 0 even when the command inside fails
            raise RuntimeError("Could not save the key in your Keychain")

    def delete(self, env: str) -> None:
        self._check(env)
        self.run([SECURITY, "delete-generic-password", "-a", env, "-s", self.service, *self._target()],
                 capture_output=True, text=True)

    def keys(self) -> dict:
        out = {}
        for env in KEY_ENV.values():
            v = self.get(env)
            if v:
                out[env] = v
        return out


def build_config(s: Settings, *, appdir: Path = APPDIR, keychain=None, claude: Path | None = None,
                 overrides: dict | None = None) -> tuple[AppConfig, list[str]]:
    """The engine's config plus the problems that stop sessions from running. Missing pieces become placeholder
    paths, so the server still starts and the person can fix things from Settings."""
    problems = []
    claude = claude or locate.find_claude()
    if not claude or not Path(claude).exists():
        claude = None
        problems.append("Claude Code is not installed (no `claude` program found). Install it, then restart Litrev.")
    skill = locate.find_skill(claude, override=s.skill_override)
    if not skill:
        problems.append("The reviewing-literature skill was not found. Install the plugin, or set the skill "
                        "folder in Settings.")
    try:
        keys = (keychain or Keychain()).keys()
    except OSError:
        keys = {}
    cfg = AppConfig(root=Path(s.root).expanduser(), skill=skill or Path("/missing/reviewing-literature"),
                    claude=claude or Path("/missing/claude"), python=Path(sys.executable), appdir=appdir,
                    parallel=s.parallel, step5_model=s.step5_model, model=s.model, effort=s.effort,
                    mailto=s.mailto, keys=keys, wait_s=s.wait_s)
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg, problems
