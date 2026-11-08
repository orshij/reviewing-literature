"""Doctor checks (spec: doctor.py): what must be true on this Mac before the app can run a review. Every program
the checks run goes through an injected runner, so tests replay recorded outputs (spike S5 fixtures)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import locate
from .claude_cmd import STRIP_ENV, agents

MIN_CLAUDE = (2, 1, 296)      # oldest Claude Code with every flag the app passes (the M0 spikes ran on 2.1.296)
TESTED_UP_TO = (2, 1, 296)    # newest version the app was checked on; raise it after the M1 checks pass on a newer one
MIN_SKILL = (1, 19, 0)        # app-mode.md, status skill_version, the F1-F5 status fixes
AGENTS = ("litrev-details", "litrev-reader", "litrev-dataset", "litrev-screener")
ICLOUD = ("Library", "Mobile Documents")
LOGIN_FIX = ("Log in with your Claude account (Team, Pro or Max) in the Terminal window that opens; a browser page "
             "asks you to confirm.")
KEY_NOTE = "An API key in your environment would override your Claude login; the app removes it for its sessions."


@dataclass
class Check:
    id: str
    title: str
    level: str                      # ok | warn | fail
    message: str
    fix: str | None = None          # what the person can do, in words
    action: str | None = None       # button label when POST /api/doctor/<id>/fix can do it for them
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Only 'fail' blocks a review; warnings don't."""
        return self.level != "fail"

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


@dataclass
class Inputs:
    claude: Path | None
    skill: Path | None
    python: Path
    root: Path
    appdir: Path
    home: Path
    env: dict
    keys: dict = field(default_factory=dict)   # the Keychain's API keys, by env name (config.KEY_ENV)


Runner = Callable[..., subprocess.CompletedProcess]


def run_cmd(argv, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    """subprocess.run that never raises: a missing program is returncode 127, a timeout 124."""
    argv = [str(a) for a in argv]
    try:
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout:.0f} s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def clean_env(env: dict) -> dict:
    """What the app's Claude Code processes get: no API key, token, gateway or cloud-provider switch."""
    return {k: v for k, v in env.items() if k not in STRIP_ENV and not k.startswith("CLAUDE_CODE_USE_")}


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _v(t) -> str:
    return ".".join(str(x) for x in t)


def check_claude(inp: Inputs, run: Runner = run_cmd) -> Check:
    t = "Claude Code"
    if not inp.claude:
        return Check("claude", t, "fail", "Claude Code is not installed: no `claude` program was found.",
                     fix="Run install.sh again, or in Terminal: curl -fsSL https://claude.ai/install.sh | bash")
    p = run([inp.claude, "--version"], env=clean_env(inp.env), timeout=30)
    v = parse_version(p.stdout)
    if p.returncode != 0 or not v:
        return Check("claude", t, "fail", f"`claude --version` failed: {(p.stderr or p.stdout).strip()[:200]}",
                     fix="Reinstall Claude Code: run install.sh again.", detail={"path": str(inp.claude)})
    d = {"version": _v(v), "path": str(inp.claude)}
    if v < MIN_CLAUDE:
        return Check("claude", t, "fail", f"Claude Code {_v(v)} is older than {_v(MIN_CLAUDE)}, the oldest version "
                     "the app works with.", fix="Update Claude Code.", action="Update Claude Code", detail=d)
    if v > TESTED_UP_TO:
        return Check("claude", t, "warn", f"Claude Code {_v(v)} is newer than the newest version the app was tested "
                     f"with ({_v(TESTED_UP_TO)}). It updates itself and should keep working; if a step stops at "
                     "startup with a login or 'app update needed' message, run update.sh.", detail=d)
    return Check("claude", t, "ok", f"Claude Code {_v(v)}", detail=d)


def classify_auth(stdout: str, returncode: int, env_has_key: bool) -> Check:
    """Spike S5: ready only when loggedIn and authMethod is 'claude.ai'."""
    t = "Claude login"
    try:
        d = json.loads(stdout or "")
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return Check("auth", t, "fail", "Could not read `claude auth status`.", fix=LOGIN_FIX, action="Log in")
    method = d.get("authMethod")
    detail = {k: d[k] for k in ("authMethod", "email", "subscriptionType", "orgName", "apiKeySource") if d.get(k)}
    if returncode != 0 or not d.get("loggedIn") or method in (None, "none"):
        return Check("auth", t, "fail", "Claude Code is not logged in.", fix=LOGIN_FIX, action="Log in", detail=detail)
    if method == "claude.ai":
        msg = f"Logged in as {d.get('email') or 'your Claude account'}"
        msg += f" ({d['subscriptionType']} plan)" if d.get("subscriptionType") else ""
        if env_has_key:
            return Check("auth", t, "warn", f"{msg}. {KEY_NOTE}", detail=detail)
        return Check("auth", t, "ok", msg, detail=detail)
    if method == "api_key" and d.get("apiKeySource") == "ANTHROPIC_API_KEY":
        # this ran with ANTHROPIC_API_KEY removed from the environment (check_auth), so the key is set in an `env`
        # block of a Claude Code settings file, which reaches every session the app starts (M1c review)
        return Check("auth", t, "fail", "Claude Code uses an ANTHROPIC_API_KEY from its settings instead of your "
                     "Claude login: reviews would be billed to that key.",
                     fix="Remove ANTHROPIC_API_KEY from the \"env\" block of ~/.claude/settings.json (and any project "
                         "settings), then press Check again.", detail=detail)
    return Check("auth", t, "fail", f"Claude Code uses an API key ({d.get('apiKeySource') or method}) instead of your "
                 "Claude login, and the app cannot remove it: its sessions would be billed to that key.",
                 fix="Remove that key (or the apiKeyHelper setting in ~/.claude/settings.json), then log in with your "
                     "Claude account.", action="Log in", detail=detail)


def check_auth(inp: Inputs, run: Runner = run_cmd) -> Check:
    if not inp.claude:
        return Check("auth", "Claude login", "fail", "Needs Claude Code first.", fix="Install Claude Code (above).")
    p = run([inp.claude, "auth", "status", "--json"], env=clean_env(inp.env), timeout=30)
    return classify_auth(p.stdout, p.returncode, "ANTHROPIC_API_KEY" in inp.env)


def skill_version(skill: Path) -> tuple[int, int, int] | None:
    """SKILL_VERSION in scripts/litrev.py: the value `status --json` reports as skill_version. `status` needs a
    review, and a fresh install has none, so the constant is read directly."""
    try:
        src = (skill / "scripts" / "litrev.py").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r'^SKILL_VERSION\s*=\s*"([^"]+)"', src, re.M)
    return parse_version(m.group(1)) if m else None


def _status_version(inp: Inputs, run: Runner) -> tuple[int, int, int] | None:
    """Fallback: `status --json` on the first review under the root, if there is one."""
    for meta in sorted((inp.root / "reviews").glob("*/meta.json"))[:1]:
        p = run([inp.python, inp.skill / "scripts" / "litrev.py", "status", "--json", "--review", meta.parent],
                env={**clean_env(inp.env), "LITREV_HOME": str(inp.root)}, timeout=120)
        try:
            return parse_version(json.loads(p.stdout).get("skill_version"))
        except (ValueError, AttributeError):
            return None
    return None


def check_skill(inp: Inputs, run: Runner = run_cmd) -> Check:
    t = "reviewing-literature skill"
    if not inp.skill:
        return Check("skill", t, "fail", "The reviewing-literature skill was not found (no installed plugin and no "
                     "~/.claude/skills/reviewing-literature).", fix="Run install.sh again; it installs the plugin.")
    s = inp.skill
    missing = [n for n in ("SKILL.md", "app-mode.md", "scripts/litrev.py") if not (s / n).exists()]
    try:
        have = set(agents(s))
    except (OSError, KeyError, ValueError):
        have = set()
    missing += [f"agents/{a}.md" for a in AGENTS if a not in have]
    if missing:
        return Check("skill", t, "fail", f"The skill at {s} is missing {', '.join(missing)}.",
                     fix="Update the plugin: run update.sh.", detail={"path": str(s)})
    v = skill_version(s) or _status_version(inp, run)
    if not v or v < MIN_SKILL:
        return Check("skill", t, "fail", f"Skill version {_v(v) if v else 'unknown'} is older than {_v(MIN_SKILL)}, "
                     "which the app needs.", fix="Update the plugin: run update.sh.", detail={"path": str(s)})
    return Check("skill", t, "ok", f"reviewing-literature {_v(v)}", detail={"path": str(s), "version": _v(v)})


def check_python(inp: Inputs, run: Runner = run_cmd) -> Check:
    t = "Python packages"
    p = run([inp.python, "-c", "import openpyxl, pyarrow; print(openpyxl.__version__, pyarrow.__version__)"],
            timeout=120)
    d = {"python": str(inp.python)}
    if p.returncode == 0:
        v = (p.stdout.split() + ["?", "?"])[:2]
        return Check("python", t, "ok", f"openpyxl {v[0]}, pyarrow {v[1]}", detail=d)
    m = re.search(r"No module named '([^']+)'", p.stderr or "")
    if m:
        return Check("python", t, "warn", f"{m.group(1)} is missing from the app's Python: reviews run, but the Excel "
                     "export and the archived leaderboards are skipped.", fix="Reinstall the app: run update.sh.",
                     detail=d)
    return Check("python", t, "fail", f"The app's Python ({inp.python}) does not run: "
                 f"{(p.stderr or p.stdout).strip()[:200]}", fix="Reinstall the app: run update.sh.", detail=d)


def check_pdftotext(inp: Inputs, which: Callable = shutil.which) -> Check:
    t = "pdftotext (optional)"
    hit = which("pdftotext", path=os.pathsep.join([inp.env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]))
    if hit:
        return Check("pdftotext", t, "ok", f"found at {hit}")
    return Check("pdftotext", t, "warn", "Not installed. A paper whose only full text is a PDF that arXiv and jina "
                 "cannot read falls back to its abstract.",
                 fix="Optional: install Homebrew (https://brew.sh), then run `brew install poppler` in Terminal.")


def pwc_dir(inp: Inputs) -> Path:
    """Where litrev keeps the pwc-archive shards and index: $LITREV_PWC_CACHE, else <root>/cache/pwc."""
    return Path(inp.env.get("LITREV_PWC_CACHE") or inp.root / "cache" / "pwc").expanduser()


def check_pwc(inp: Inputs, job: dict | None = None) -> Check:
    t = "Archived leaderboards (optional)"
    idx = pwc_dir(inp) / "eval_index.json"
    if job and job.get("state") == "running":
        return Check("pwc", t, "warn", f"Building: {job.get('progress') or 'starting'}",
                     detail={"path": str(idx), "building": True})
    if idx.exists():
        return Check("pwc", t, "ok", f"Index built ({idx.stat().st_size // 2**20} MB)", detail={"path": str(idx)})
    msg = ("Not built: `enrich` skips the historical Papers with Code leaderboard rows (an archive frozen on "
           "2025-09-13).")
    if job and job.get("state") == "failed":
        msg = f"Building it failed: {job.get('error')}"
    return Check("pwc", t, "warn", msg, fix="Downloads about 190 MB once, then indexes it (a minute or two).",
                 action="Build it", detail={"path": str(idx)})


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def voice_home(inp: Inputs) -> Path:
    """Where `litrev.py voice-setup` puts the voice venv: $LITREV_VOICE_HOME, else ~/.local/share/litrev/tts."""
    return Path(inp.env.get("LITREV_VOICE_HOME") or inp.home / ".local" / "share" / "litrev" / "tts").expanduser()


# Kokoro's text-to-phonemes path (misaki, spaCy and its model, espeak) and the recogniser's import, without the voice
# model and without a download: about 8 s. misaki installs en_core_web_sm on first use, so without it the probe skips
# the phonemes instead of installing it.
VOICE_PROBE = """import spacy, mlx_whisper
from kokoro import KPipeline
if spacy.util.is_package("en_core_web_sm"):
    ps = [r.phonemes for r in KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", model=False)("The voice is ready.", split_pattern=None)]
    assert ps and ps[0], "no phonemes"
"""


def _venv_state(home: Path) -> tuple:
    """What changes when the venv does: the warm-up stamp, and the site-packages folder (an install or a removal adds
    or drops a *.dist-info entry, which changes the folder's mtime)."""
    paths = [home / ".litrev-warm", *sorted(home.glob("lib/python3*/site-packages"))]
    return tuple(p.stat().st_mtime_ns if p.exists() else None for p in paths)


class VoiceProbes:
    """One VOICE_PROBE run per venv state, in the background (`spawn`): it takes about 8 s and the Doctor redraws the
    video row on every request. A venv with all its files can still fail: on 2026-10-25 preshed 3.0.14 broke spaCy in
    a teammate's venv, and the files-only check called it ready."""

    def __init__(self, spawn: Callable[[Callable[[], None]], None] | None = None):
        self._spawn = spawn or (lambda f: threading.Thread(target=f, name="voice-probe", daemon=True).start())
        self._lock = threading.Lock()
        self._res: dict = {}

    def get(self, home: Path, run: Runner) -> dict:
        """{'state': 'running'}, or {'state': 'done', 'ok': bool, 'error': the last line of its stderr}."""
        key = (str(home), _venv_state(home))
        with self._lock:
            if key in self._res:
                return dict(self._res[key])
            self._res[key] = {"state": "running"}

        def probe() -> None:
            try:
                # 7-9 s on a used venv; 79 s on a new venv's first imports (measured 2026-10-26), hence the margin
                p = run([home / "bin" / "python", "-I", "-c", VOICE_PROBE], timeout=300)
                lines = [ln.strip() for ln in (p.stderr or "").splitlines() if ln.strip()]
                res = {"state": "done", "ok": p.returncode == 0, "error": "" if p.returncode == 0
                       else (lines[-1] if lines else f"exit code {p.returncode}")[:200]}
            except Exception as e:          # a probe that never ends would keep the row polling
                res = {"state": "done", "ok": False, "error": str(e)[:200]}
            with self._lock:
                self._res[key] = res
        self._spawn(probe)
        with self._lock:
            return dict(self._res[key])

    def forget_done(self) -> None:
        """Check again: probe once more. A probe that is still running is kept."""
        with self._lock:
            self._res = {k: v for k, v in self._res.items() if v["state"] == "running"}


VOICE_PROBES = VoiceProbes()


def check_video(inp: Inputs, which: Callable = shutil.which, job: dict | None = None, run: Runner = run_cmd,
                probes: VoiceProbes | None = None) -> Check:
    """Step 8 (the narrated video, optional): Google Chrome for the stills, ffmpeg for the video, and the voice venv
    with its models (a session cannot install packages: litrev refuses voice-setup in an app session). A venv with
    its files gets one quick run per venv state (VoiceProbes)."""
    t = "Video tools (optional, step 8)"
    path = os.pathsep.join([inp.env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    home = voice_home(inp)
    d = {"voice_venv": str(home), "chrome": Path(inp.env.get("LITREV_CHROME") or CHROME).exists(),
         "ffmpeg": bool(which("ffmpeg", path=path) and which("ffprobe", path=path)),
         "voice": (home / "bin" / "python").exists() and (home / ".litrev-warm").exists()}
    if job and job.get("state") == "running":
        return Check("video", t, "warn", f"Installing the voice tools: {job.get('progress') or 'starting'}",
                     detail={**d, "installing": True})
    probe = (probes or VOICE_PROBES).get(home, run) if d["voice"] else None
    if probe and probe["state"] == "running":            # `building` makes the page ask again every 3 s
        return Check("video", t, "warn", "Checking that the voice tools run (about 10 seconds; a minute the first time)",
                     detail={**d, "building": True})
    broken = bool(probe) and not probe["ok"]
    if broken:
        d.update(voice=False, voice_error=probe["error"])
    missing = [n for n, ok in (("Google Chrome", d["chrome"]), ("ffmpeg", d["ffmpeg"]), ("the voice tools", d["voice"])) if not ok]
    if not missing:
        return Check("video", t, "ok", "Chrome, ffmpeg and the voice tools are ready", detail=d)
    msg = "Missing: " + ", ".join(missing) + ". Reviews work without them; only the narrated video needs them."
    if broken:
        others = [n for n in missing if n != "the voice tools"]
        msg = (f"The voice tools are installed but do not run ({probe['error']})."
               + (f" Missing: {', '.join(others)}." if others else ""))
    if job and job.get("state") == "failed":
        msg = f"Installing the voice tools failed: {job.get('error')}"
    fixes = [x for x, need in (("install Google Chrome from https://www.google.com/chrome/", not d["chrome"]),
                               ("run `brew install ffmpeg` in Terminal", not d["ffmpeg"])) if need]
    voice_fix = (f"Press Install voice tools: it repairs them. If they still do not run, delete the folder {home}, "
                 "then press it again." if broken else "Install the voice tools here: about 1.6 GB, a few minutes.")
    return Check("video", t, "warn", msg,
                 fix=("; ".join(fixes) + ("; " if fixes and not d["voice"] else "")).capitalize()
                     + (voice_fix if not d["voice"] else ""),
                 action="Install voice tools" if not d["voice"] else None, detail=d)


def check_openalex(inp: Inputs) -> Check:
    """Without a key OpenAlex gives 1,000 credits a day per IP address (measured 2026-10-13), shared by everyone on
    the network, and one review needs more: a teammate's review lost its OpenAlex data until 00:00 UTC."""
    t = "OpenAlex API key"
    if inp.keys.get("OPENALEX_API_KEY") or inp.env.get("OPENALEX_API_KEY"):
        return Check("openalex", t, "ok", "Set: 10,000 OpenAlex credits a day")
    return Check("openalex", t, "warn", "Not set. Without a key, OpenAlex gives 1,000 credits a day, shared by "
                 "everyone on your network, and one review needs more. When they run out, citation counts and "
                 "venues stop until 00:00 UTC.",
                 fix="Get a free key (10,000 credits a day) at https://openalex.org/settings/api, then paste it in "
                     "Settings → Paper sources → OpenAlex API key.")


def _nearest_existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _within(p: str, base: str) -> bool:
    return p == base or p.startswith(base.rstrip("/") + "/")


def check_root(inp: Inputs) -> Check:
    t = "Reviews folder"
    root = inp.root.expanduser()
    real = os.path.realpath(root)
    d = {"path": str(root)}
    if _within(real, os.path.realpath(inp.home.joinpath(*ICLOUD))):
        return Check("root", t, "fail", f"{root} is inside iCloud Drive, which syncs and evicts files while the app "
                     "writes them and corrupts reviews.", fix="Choose a folder outside iCloud Drive in Settings (for "
                     "example ~/LitReviews).", detail=d)
    base = _nearest_existing(root)
    if not base.is_dir() or not os.access(base, os.W_OK | os.X_OK):
        return Check("root", t, "fail", f"Cannot write to {base}.", fix="Choose a folder you own in Settings (for "
                     "example ~/LitReviews).", detail=d)
    if root.exists():
        try:
            fd, probe = tempfile.mkstemp(dir=root, prefix=".litrev-write-test-")
            os.close(fd)
            os.unlink(probe)
        except OSError as e:
            return Check("root", t, "fail", f"Cannot write to {root}: {e.strerror or e}.",
                         fix="Choose a folder you own in Settings.", detail=d)
        msg = f"{root} (writable)"
    else:
        msg = f"{root} (created with the first review)"
    if any(_within(real, os.path.realpath(inp.home / n)) for n in ("Documents", "Desktop")):
        return Check("root", t, "warn", f"{msg}. If iCloud's 'Desktop & Documents Folders' option is on, this folder "
                     "syncs to iCloud, which can corrupt reviews.", fix="Turn that option off, or choose a folder "
                     "outside Documents and Desktop in Settings.", detail=d)
    return Check("root", t, "ok", msg, detail=d)


def run_checks(inp: Inputs, run: Runner = run_cmd, which: Callable = shutil.which,
               jobs: dict | None = None, probes: VoiceProbes | None = None) -> list[Check]:
    """Every check, in display order. The four that run a program run in parallel (about 2 s in all); the voice
    probe runs in the background unless `probes` says otherwise."""
    with ThreadPoolExecutor(max_workers=4) as ex:
        slow = [ex.submit(f, inp, run) for f in (check_claude, check_auth, check_skill, check_python)]
        out = [f.result() for f in slow]
    return out + [check_pdftotext(inp, which), check_pwc(inp, (jobs or {}).get("pwc")), check_root(inp),
                  check_openalex(inp), check_video(inp, which, (jobs or {}).get("video"), run, probes)]


def format_table(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{c.level.upper():<5} {c.title:<34} {c.message}")
        # Terminal has no button: a fix the app can do (c.action) says where its button is.
        fix = " ".join(filter(None, [c.action and f'In Litrev, open Settings and press "{c.action}" under Doctor.',
                                     c.fix]))
        if fix and c.level != "ok":
            lines.append(f"{'':<40} fix: {fix}")
    n = sum(1 for c in checks if c.level == "fail")
    lines.append("")
    lines.append(f"{n} problem(s) to fix before the app can run a review." if n else "Ready: the app can run reviews.")
    return "\n".join(lines)


def default_appdir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Application Support" / "LitrevApp"


def app_paths(home: Path | None = None) -> tuple[Path, Path, dict]:
    """(reviews root, app dir, saved settings) the way M1b's config.load resolves them, for commands run outside the
    server: settings.json's 'root', else the default root (config ignores $LITREV_HOME, so this does too)."""
    h = home or Path.home()
    appdir = default_appdir(h)
    try:
        s = json.loads((appdir / "settings.json").read_text())
    except (OSError, ValueError):
        s = {}
    s = s if isinstance(s, dict) else {}
    root = Path(s.get("root") or locate.default_root(h)).expanduser()
    return root, appdir, s


def default_inputs(env: dict | None = None, home: Path | None = None) -> Inputs:
    """Inputs for `litrev-app doctor` run in Terminal (no server): the places the app itself would use."""
    env = dict(os.environ if env is None else env)
    h = home or Path.home()
    root, appdir, s = app_paths(h)
    claude = locate.find_claude(env.get("PATH"))
    skill = locate.find_skill(claude, override=s.get("skill_override") or None, home=h)
    try:                                      # install.sh's doctor: the key lives in the Keychain, not the shell
        from .config import Keychain
        keys = Keychain().keys()
    except Exception:
        keys = {}
    return Inputs(claude, skill, Path(sys.executable), root, appdir, h, env, keys)


def inputs_from_cfg(cfg, env: dict | None = None, home: Path | None = None) -> Inputs:
    """Inputs from the running app's AppConfig (M1a); re-locates claude or the skill if the config's path is gone."""
    env = dict(os.environ if env is None else env)
    claude = (Path(cfg.claude) if getattr(cfg, "claude", None) and Path(cfg.claude).exists()
              else locate.find_claude(env.get("PATH")))
    skill = (Path(cfg.skill) if getattr(cfg, "skill", None) and (Path(cfg.skill) / "SKILL.md").exists()
             else locate.find_skill(claude, home=home))
    return Inputs(claude, skill, Path(cfg.python), Path(cfg.root), Path(cfg.appdir), home or Path.home(), env,
                  dict(getattr(cfg, "keys", None) or {}))
