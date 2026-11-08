import json
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
MARKETS = [{"name": "litrev", "installLocation": "/clone/reviewing-literature-plugin"}]
PLUGINS = [{"id": "reviewing-literature@litrev", "enabled": True}]


def claude_stub(auth="claude.ai", markets=MARKETS, plugins=PLUGINS):
    a = json.dumps({"loggedIn": auth != "none", "authMethod": auth})
    return f'''case "$*" in
  "--version") echo "2.1.296 (Claude Code)" ;;
  "auth status --json") echo '{a}' ;;
  "plugin marketplace list --json") echo '{json.dumps(markets)}' ;;
  "plugin list --json") echo '{json.dumps(plugins)}' ;;
  *) echo "unexpected: claude $*" >&2; exit 9 ;;
esac'''


ALL = {   # everything already installed
    "claude": claude_stub(),
    "git": "exit 0",
    "xcode-select": "echo /Library/Developer/CommandLineTools",
    "uv": 'case "$*" in "tool dir --bin") echo "$STUB" ;; "--version") echo "uv 0.9.11" ;; esac',
    "litrev-app": 'case "$1" in sessions) exit ${SESSIONS_RC:-0} ;; *) exit 0 ;; esac',
    "pdftotext": "exit 0",
}


def run_script(tmp_path, name, stubs, extra_env=None):
    home, stub = tmp_path / "home", tmp_path / "stubbin"
    home.mkdir(exist_ok=True)
    stub.mkdir(exist_ok=True)
    for n, body in stubs.items():
        f = stub / n
        f.write_text("#!/bin/bash\n" + body.replace("$STUB", str(stub)) + "\n")
        f.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin", "LITREV_INSTALL_DRY_RUN": "1",
           "LITREV_INSTALL_EXTRA_PATH": "", **(extra_env or {})}
    p = subprocess.run(["/bin/bash", str(APP / name)], env=env, capture_output=True, text=True, timeout=60)
    return p, [ln[2:] for ln in p.stdout.splitlines() if ln.startswith("+ ")]


MARKET_URL = "orshij/reviewing-literature"


def test_install_on_a_fresh_mac_runs_every_step_in_order(tmp_path):
    p, plus = run_script(tmp_path, "install.sh", {"xcode-select": "exit 2", "git": "exit 128"})
    assert p.returncode == 0, p.stdout + p.stderr
    joined = "\n".join(plus)
    order = ["xcode-select --install", "curl -fsSL https://claude.ai/install.sh | bash",
             "env -u ANTHROPIC_API_KEY claude auth login --claudeai",
             f"claude plugin marketplace add {MARKET_URL}",
             "claude plugin install reviewing-literature@litrev",
             "curl -LsSf https://astral.sh/uv/install.sh | sh", "uv tool install --python 3.12", "osacompile",
             "litrev-app doctor"]
    pos = [joined.find(s) for s in order]
    assert all(x >= 0 for x in pos) and pos == sorted(pos), list(zip(order, pos))
    assert not [ln for ln in plus if ln.startswith("gh ") or "install gh" in ln or "github.com" in ln]


def test_install_checks_a_github_repo_over_https(tmp_path):
    log = tmp_path / "git.log"
    p, plus = run_script(tmp_path, "install.sh", {**ALL, "git": f'echo "$*" >> {log}; exit 0'})
    assert p.returncode == 0, p.stdout + p.stderr
    assert "ls-remote https://github.com/orshij/reviewing-literature.git HEAD" in log.read_text()
    assert not [ln for ln in plus if "ssh-keygen" in ln or "pbcopy" in ln]


def test_install_stops_when_git_cannot_read_the_repo(tmp_path):
    p, plus = run_script(tmp_path, "install.sh", {**ALL, "git": "exit 128"}, {"LITREV_INSTALL_DRY_RUN": ""})
    assert p.returncode != 0 and "git cannot read orshij/reviewing-literature" in p.stderr


def test_install_takes_the_marketplace_url_from_the_environment(tmp_path):
    p, plus = run_script(tmp_path, "install.sh", {**ALL, "claude": claude_stub(markets=[])},
                         {"LITREV_MARKET_URL": "https://git.example.com/team/plugins.git"})
    assert p.returncode == 0, p.stdout + p.stderr
    assert "claude plugin marketplace add https://git.example.com/team/plugins.git" in plus


def test_install_again_changes_nothing(tmp_path):
    p, plus = run_script(tmp_path, "install.sh", ALL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert len(plus) == 3 and plus[0].startswith("mkdir -p") and plus[1].startswith("osacompile")
    assert plus[2].endswith("litrev-app doctor")


def test_install_skips_the_plugin_when_the_skill_is_personal(tmp_path):
    skill = tmp_path / "home" / ".claude" / "skills" / "reviewing-literature"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    p, plus = run_script(tmp_path, "install.sh", {**ALL, "claude": claude_stub(plugins=[])})
    assert p.returncode == 0 and "not installing the plugin" in p.stdout
    assert not [ln for ln in plus if "plugin install" in ln]


def test_install_from_a_checkout_skips_github_and_the_plugin(tmp_path):
    stubs = {k: v for k, v in ALL.items() if k != "litrev-app"}
    p, plus = run_script(tmp_path, "install.sh", {**stubs, "git": "exit 128"}, {"LITREV_APP_SRC": "/src/app"})
    assert p.returncode == 0, p.stdout + p.stderr
    assert not [ln for ln in plus if ln.startswith("gh ") or "marketplace" in ln or "install gh" in ln]
    assert "uv tool install --python 3.12 /src/app" in plus


def test_update_refuses_while_a_session_runs(tmp_path):
    p, plus = run_script(tmp_path, "update.sh", ALL, {"SESSIONS_RC": "3"})
    assert p.returncode != 0 and "running" in p.stderr
    assert not [ln for ln in plus if "uv tool install" in ln]


def test_update_order(tmp_path):
    p, plus = run_script(tmp_path, "update.sh", ALL)
    assert p.returncode == 0, p.stdout + p.stderr
    joined = "\n".join(plus)
    order = ["litrev-app stop", "claude plugin marketplace update litrev",
             "claude plugin update reviewing-literature@litrev",
             "uv tool install --force --reinstall --python 3.12 /clone/reviewing-literature-plugin/app",
             "litrev-app doctor"]
    pos = [joined.find(s) for s in order]
    assert all(x >= 0 for x in pos) and pos == sorted(pos), list(zip(order, pos))


@pytest.mark.parametrize("name", ["install.sh", "update.sh"])
def test_scripts_are_bash_32(name):
    text = (APP / name).read_text()
    subprocess.run(["/bin/bash", "-n", str(APP / name)], check=True)
    for bad in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "coproc"):
        assert bad not in text, bad


@pytest.mark.skipif(not Path("/usr/bin/osacompile").exists(), reason="macOS only")
def test_launcher_compiles(tmp_path):
    src = 'property litrevApp : "/Users/x/.local/bin/litrev-app"\n' + (APP / "launcher" / "Litrev.applescript").read_text()
    subprocess.run(["/usr/bin/osacompile", "-o", str(tmp_path / "Litrev.app")], input=src, text=True, check=True)
    assert (tmp_path / "Litrev.app" / "Contents" / "Resources" / "Scripts" / "main.scpt").exists()


def test_install_upgrades_an_older_app(tmp_path):
    # real install 2026-10-11: step 7 said "ok: installed" and kept an older copy without `litrev-app doctor`
    clone = tmp_path / "clone"
    (clone / "app").mkdir(parents=True)
    (clone / "app" / "pyproject.toml").write_text('[project]\nname = "litrev-app"\nversion = "0.2.0"\n')
    stubs = lambda have: dict(ALL, claude=claude_stub(markets=[{"name": "litrev", "installLocation": str(clone)}]),
                              uv='case "$*" in "tool dir --bin") echo "$STUB" ;; "--version") echo "uv 0.9.11" ;; '
                                 f'"tool list") echo "litrev-app v{have}" ;; esac')
    p, plus = run_script(tmp_path, "install.sh", stubs("0.1.0"))
    assert any(c.startswith("uv tool install --force --reinstall --python 3.12") for c in plus), plus
    p, plus = run_script(tmp_path, "install.sh", stubs("0.2.0"))
    assert not any(c.startswith("uv tool install") for c in plus) and "ok: installed" in p.stdout


def test_install_never_replaces_the_app_under_a_running_session(tmp_path):
    # M1c review: the force-reinstall branch rebuilt the venv while a review step ran in it
    clone = tmp_path / "clone"
    (clone / "app").mkdir(parents=True)
    (clone / "app" / "pyproject.toml").write_text('[project]\nname = "litrev-app"\nversion = "0.2.0"\n')
    stubs = dict(ALL, claude=claude_stub(markets=[{"name": "litrev", "installLocation": str(clone)}]),
                 uv='case "$*" in "tool dir --bin") echo "$STUB" ;; "--version") echo "uv 0.9.11" ;; '
                    '"tool list") echo "litrev-app v0.1.0" ;; esac')
    p, plus = run_script(tmp_path, "install.sh", stubs, {"SESSIONS_RC": "3"})
    assert p.returncode != 0 and not any(c.startswith("uv tool install") for c in plus)
    assert "running" in (p.stdout + p.stderr)


@pytest.mark.parametrize("name", ["install.sh", "update.sh", "README.md", "launcher/Litrev.applescript"])
def test_teammate_texts_name_no_person(name):
    """Any teammate installs and runs Litrev on their own Mac and Claude account: no message sends them to one
    person by name."""
    import re
    text = (APP / name).read_text()
    assert not re.search(r"\bOr\b|Or's", text), re.findall(r".{0,40}\bOr\b.{0,40}", text)
