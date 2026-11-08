import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apphelpers import FakeRun, make_skill
from litrev_app import doctor_checks as dc
from litrev_app.doctor_checks import Inputs

FX = Path(__file__).parent / "fixtures" / "auth_status"


def fixture(name):
    """A spike S5 file: what `claude auth status` printed, then a line `exit=N`."""
    body, _, code = (FX / f"{name}.txt").read_text().rpartition("exit=")
    return body.strip(), int(code.strip())


def inputs(tmp_path, **kw):
    base = dict(claude=Path("/opt/claude"), skill=None, python=Path("/venv/bin/python"), root=tmp_path / "root",
                appdir=tmp_path / "appdir", home=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    base.update(kw)
    return Inputs(**base)


def test_parse_version():
    assert dc.parse_version("2.1.296 (Claude Code)\n") == (2, 1, 296)
    assert dc.parse_version("garbage") is None and dc.parse_version(None) is None


def test_claude_version_range(tmp_path):
    def at(version):
        return dc.check_claude(inputs(tmp_path), FakeRun({"--version": (0, f"{version} (Claude Code)\n", "")}))
    ok, newer, old = at("2.1.296"), at("2.2.3"), at("2.1.250")
    assert (ok.level, newer.level, old.level) == ("ok", "warn", "fail")
    assert newer.ok and "newer than" in newer.message and newer.detail["version"] == "2.2.3"
    assert old.action == "Update Claude Code" and not old.ok
    assert dc.check_claude(inputs(tmp_path, claude=None)).level == "fail"
    assert dc.check_claude(inputs(tmp_path), FakeRun({})).level == "fail"          # the program does not run
    run = FakeRun({"--version": (0, "2.1.296", "")})
    dc.check_claude(inputs(tmp_path, env={"ANTHROPIC_API_KEY": "sk", "CLAUDE_CODE_USE_BEDROCK": "1"}), run)
    assert "ANTHROPIC_API_KEY" not in run.calls[0][1] and "CLAUDE_CODE_USE_BEDROCK" not in run.calls[0][1]


def test_auth_from_the_spike_fixtures():
    ok = dc.classify_auth(*fixture("claude_ai"), env_has_key=False)
    assert ok.level == "ok" and "team plan" in ok.message
    assert dc.classify_auth(*fixture("claude_ai"), env_has_key=True).level == "warn"
    # M1c review: check_auth runs claude with the key removed from the environment, so an api_key answer means the key
    # comes from a settings file's env block, which the app cannot remove: every session would be billed to it
    key = dc.classify_auth(*fixture("api_key"), env_has_key=True)
    assert key.level == "fail" and "settings.json" in key.fix
    out = dc.classify_auth(*fixture("isolated"), env_has_key=False)
    assert out.level == "fail" and out.action == "Log in"
    assert dc.classify_auth("not json", 0, False).level == "fail"
    helper = json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "apiKeyHelper"})
    assert dc.classify_auth(helper, 0, False).level == "fail"


def test_check_auth_runs_without_the_api_key(tmp_path):
    body, _ = fixture("claude_ai")
    run = FakeRun({"auth status": (0, body, "")})
    c = dc.check_auth(inputs(tmp_path, env={"ANTHROPIC_API_KEY": "sk", "PATH": "/bin"}), run)
    argv, env = run.calls[0]
    assert argv[-3:] == ["auth", "status", "--json"] and "ANTHROPIC_API_KEY" not in env
    assert c.level == "warn"                                   # logged in, but the shell exports a key


def test_skill_checks(tmp_path):
    good = dc.check_skill(inputs(tmp_path, skill=make_skill(tmp_path / "good")))
    assert good.level == "ok" and good.detail["version"] == "1.19.0"
    assert dc.check_skill(inputs(tmp_path, skill=make_skill(tmp_path / "old", version="1.18.4"))).level == "fail"
    no_mode = dc.check_skill(inputs(tmp_path, skill=make_skill(tmp_path / "nm", app_mode=False)))
    assert no_mode.level == "fail" and "app-mode.md" in no_mode.message
    no_agent = dc.check_skill(inputs(tmp_path, skill=make_skill(tmp_path / "na", agents=("litrev-details",))))
    assert "agents/litrev-dataset.md" in no_agent.message
    assert dc.check_skill(inputs(tmp_path, skill=None)).level == "fail"


def test_skill_version_falls_back_to_status_on_a_review(tmp_path):
    s = make_skill(tmp_path / "s", version=None)
    (tmp_path / "root" / "reviews" / "x").mkdir(parents=True)
    (tmp_path / "root" / "reviews" / "x" / "meta.json").write_text("{}")
    run = FakeRun({"status --json": (0, json.dumps({"rows": [], "skill_version": "1.19.2"}), "")})
    c = dc.check_skill(inputs(tmp_path, skill=s), run)
    assert c.level == "ok" and c.detail["version"] == "1.19.2"


def test_skill_found_when_plugin_list_fails(tmp_path):
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\necho 'plugin list failed' >&2\nexit 1\n")
    claude.chmod(0o755)
    own = make_skill(tmp_path / ".claude" / "skills" / "reviewing-literature")
    cfg = SimpleNamespace(claude=claude, skill=None, python=Path(sys.executable), root=tmp_path / "root",
                          appdir=tmp_path / "appdir")
    inp = dc.inputs_from_cfg(cfg, env={"PATH": str(claude.parent)}, home=tmp_path)
    assert inp.claude == claude and inp.skill == own
    assert dc.check_skill(inp).level == "ok"


def test_python_packages(tmp_path):
    i = inputs(tmp_path)
    good = dc.check_python(i, FakeRun({"import openpyxl": (0, "3.1.5 17.0.0\n", "")}))
    assert good.message == "openpyxl 3.1.5, pyarrow 17.0.0"
    miss = dc.check_python(i, FakeRun({"import openpyxl": (1, "", "ModuleNotFoundError: No module named 'pyarrow'\n")}))
    assert miss.level == "warn" and "pyarrow" in miss.message
    assert dc.check_python(i, FakeRun({})).level == "fail"


def test_pdftotext_is_optional(tmp_path):
    assert dc.check_pdftotext(inputs(tmp_path), lambda n, path=None: "/opt/homebrew/bin/pdftotext").level == "ok"
    c = dc.check_pdftotext(inputs(tmp_path), lambda n, path=None: None)
    assert c.level == "warn" and "brew install poppler" in c.fix


def test_pwc_index(tmp_path):
    i = inputs(tmp_path)
    c = dc.check_pwc(i)
    assert c.level == "warn" and c.action == "Build it"
    assert c.detail["path"] == str(tmp_path / "root" / "cache" / "pwc" / "eval_index.json")
    assert dc.check_pwc(i, {"state": "running", "progress": "downloading file 2 of 6"}).detail["building"]
    assert "boom" in dc.check_pwc(i, {"state": "failed", "error": "boom"}).message
    (tmp_path / "root" / "cache" / "pwc").mkdir(parents=True)
    (tmp_path / "root" / "cache" / "pwc" / "eval_index.json").write_text("{}")
    assert dc.check_pwc(i).level == "ok"
    other = inputs(tmp_path, env={"LITREV_PWC_CACHE": str(tmp_path / "elsewhere")})
    assert dc.check_pwc(other).detail["path"] == str(tmp_path / "elsewhere" / "eval_index.json")


def test_reviews_root(tmp_path):
    (tmp_path / "root").mkdir()
    assert dc.check_root(inputs(tmp_path)).level == "ok"
    later = dc.check_root(inputs(tmp_path, root=tmp_path / "LitReviews"))
    assert later.level == "ok" and "created with the first review" in later.message
    icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Reviews"
    assert dc.check_root(inputs(tmp_path, root=icloud)).level == "fail"
    assert dc.check_root(inputs(tmp_path, root=tmp_path / "Documents" / "Reviews")).level == "warn"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_reviews_root_not_writable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert dc.check_root(inputs(tmp_path, root=ro / "reviews")).level == "fail"
    finally:
        ro.chmod(0o700)


def test_run_checks_order_and_json(tmp_path):
    body, _ = fixture("claude_ai")
    run = FakeRun({"--version": (0, "2.1.296 (Claude Code)", ""), "auth status": (0, body, ""),
                   "import openpyxl": (0, "3.1.5 17.0.0", "")})
    checks = dc.run_checks(inputs(tmp_path, skill=make_skill(tmp_path / "s")), run, which=lambda n, path=None: None)
    assert [c.id for c in checks] == ["claude", "auth", "skill", "python", "pdftotext", "pwc", "root", "openalex", "video"]
    data = json.loads(json.dumps([c.to_dict() for c in checks]))
    assert all("ok" in c and c["level"] in ("ok", "warn", "fail") for c in data)
    table = dc.format_table(checks)
    assert "Ready: the app can run reviews." in table and "WARN" in table


def test_format_table_names_the_button_for_checks_with_an_action():
    """In Terminal (install.sh) there is no button: a check the app can fix says where to press it."""
    checks = [dc.Check("pwc", "Archived leaderboards (optional)", "warn", "Not built.",
                       fix="Downloads about 190 MB once, then indexes it (a minute or two).", action="Build it"),
              dc.Check("auth", "Claude login", "warn", "Logged in with an API key.", action="Log in"),
              dc.Check("pdftotext", "pdftotext (optional)", "warn", "Not installed.", fix="Run `brew install poppler`."),
              dc.Check("root", "Reviews folder", "ok", "writable", action="Unused")]
    lines = dc.format_table(checks).splitlines()
    fixes = [ln.strip() for ln in lines if ln.strip().startswith("fix:")]
    assert fixes == [
        'fix: In Litrev, open Settings and press "Build it" under Doctor. Downloads about 190 MB once, then indexes '
        'it (a minute or two).',
        'fix: In Litrev, open Settings and press "Log in" under Doctor.',
        "fix: Run `brew install poppler`."]


def test_openalex_key_check_warns_without_a_key_and_names_the_setting(tmp_path):
    c = dc.check_openalex(inputs(tmp_path))
    assert c.level == "warn" and c.ok and "1,000" in c.message
    assert "openalex.org/settings/api" in c.fix and "Settings → Paper sources → OpenAlex API key" in c.fix
    assert dc.check_openalex(inputs(tmp_path, keys={"OPENALEX_API_KEY": "k"})).level == "ok"
    assert dc.check_openalex(inputs(tmp_path, env={"OPENALEX_API_KEY": "k"})).level == "ok"


def test_inputs_from_cfg_carries_the_keychain_keys(tmp_path):
    from types import SimpleNamespace
    cfg = SimpleNamespace(claude=None, skill=None, python="/p", root=str(tmp_path), appdir=str(tmp_path),
                          keys={"OPENALEX_API_KEY": "k"})
    inp = dc.inputs_from_cfg(cfg, env={"PATH": ""}, home=tmp_path)
    assert inp.keys == {"OPENALEX_API_KEY": "k"} and dc.check_openalex(inp).level == "ok"
