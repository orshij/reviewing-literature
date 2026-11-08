import os
import subprocess
import sys
from pathlib import Path

import pytest

from litrev_app import config, locate
from litrev_app.config import Keychain, Settings


class StubKeychain:
    def __init__(self, keys=None):
        self._keys = keys or {}

    def keys(self):
        return dict(self._keys)


def test_settings_roundtrip_is_private(tmp_path):
    s = Settings(root=str(tmp_path / "r"), parallel=2, step5_model="sonnet", wait_s=900)
    config.save(s, tmp_path)
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600
    assert config.load(tmp_path) == s


def test_effort_defaults_to_medium_and_is_checked(tmp_path):
    (tmp_path / "settings.json").write_text('{"parallel": 2}')          # saved before the effort setting existed
    base = config.load(tmp_path)
    assert base.effort == "medium"
    assert config.update(base, {"effort": "xhigh"}).effort == "xhigh"
    assert config.update(base, {"effort": ""}).effort is None             # Claude Code's default
    with pytest.raises(ValueError, match="Effort must be one of"):
        config.update(base, {"effort": "huge"})


def test_load_fills_the_default_root(tmp_path):
    assert config.load(tmp_path / "none").root == str(locate.default_root())


def test_update_checks_every_field(tmp_path):
    base = Settings(root=str(tmp_path))
    s = config.update(base, {"parallel": "4", "wait_s": 600, "step5_model": "sonnet", "model": "",
                             "mailto": "a@b.co", "root": "~/LitReviews"})
    assert (s.parallel, s.wait_s, s.step5_model, s.model, s.mailto) == (4, 600, "sonnet", None, "a@b.co")
    assert s.root == str(Path.home() / "LitReviews") and base.parallel == 3
    for bad, words in (({"parallel": 9}, "between 1 and 6"), ({"parallel": "x"}, "whole number"),
                       ({"root": "relative/dir"}, "full path"), ({"root": ""}, "Choose a folder"),
                       ({"mailto": "nope"}, "email"), ({"step5_model": "rm -rf /"}, "model name"),
                       ({"skill_override": str(tmp_path / "missing")}, "SKILL.md"),
                       ({"wait_s": 5}, "between 60 and 7200")):
        with pytest.raises(ValueError, match=words):
            config.update(base, bad)


def test_skill_override_must_be_this_users_and_writable_only_by_them(tmp_path, monkeypatch):
    # security F4: the app runs the skill's scripts/litrev.py as you, so a folder anyone else can write is their code
    base = Settings(root=str(tmp_path))
    skill = tmp_path / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    (skill / "scripts" / "litrev.py").write_text("")
    for f, mode in ((skill, 0o755), (skill / "scripts", 0o755), (skill / "scripts" / "litrev.py", 0o644)):
        f.chmod(mode)
    real = str(skill.resolve())
    assert config.update(base, {"skill_override": str(skill)}).skill_override == real
    for f, mode in ((skill, 0o775), (skill, 0o757), (skill / "scripts", 0o777), (skill / "scripts" / "litrev.py", 0o664),
                    (skill / "scripts" / "litrev.py", 0o646)):
        old = f.stat().st_mode & 0o777
        f.chmod(mode)
        with pytest.raises(ValueError, match="Other users can change"):
            config.update(base, {"skill_override": str(skill)})
        f.chmod(old)
    link = tmp_path / "link"
    link.symlink_to(skill)                      # kept as the real folder: a link can be re-pointed after the check
    assert config.update(base, {"skill_override": str(link)}).skill_override == real
    with monkeypatch.context() as m:
        m.setattr(os, "getuid", lambda: os.stat(skill).st_uid + 1)
        with pytest.raises(ValueError, match="belongs to another user"):
            config.update(base, {"skill_override": str(skill)})
    assert config.update(Settings(root=str(tmp_path), skill_override=real), {"skill_override": ""}).skill_override is None
    no_scripts = tmp_path / "stub"; no_scripts.mkdir(); (no_scripts / "SKILL.md").write_text("x")
    assert config.update(base, {"skill_override": str(no_scripts)}).skill_override == str(no_scripts.resolve())


def test_keychain_values_never_reach_argv():
    calls, store = [], {}

    def run(argv, input=None, **kw):
        calls.append((argv, input))
        if argv[1] == "-i":
            store["v"] = input.split('-w "')[1].split('"')[0]
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0 if "v" in store else 44, store.get("v", "") + "\n", "")

    kc = Keychain(run=run)
    kc.set("OPENALEX_API_KEY", "sk-secret-123")
    assert kc.get("OPENALEX_API_KEY") == "sk-secret-123"
    assert all("sk-secret-123" not in " ".join(argv) for argv, _ in calls)
    with pytest.raises(ValueError):
        kc.set("OPENALEX_API_KEY", 'has"quote')
    with pytest.raises(ValueError):
        kc.set("HOME", "x")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
def test_keychain_roundtrip_in_a_temporary_keychain(tmp_path):
    path = str(tmp_path / "test.keychain-db")
    subprocess.run(["/usr/bin/security", "create-keychain", "-p", "pw", path], check=True)
    try:
        subprocess.run(["/usr/bin/security", "unlock-keychain", "-p", "pw", path], check=True)
        kc = Keychain(keychain=path, service="litrev-app-test")
        assert kc.get("S2_API_KEY") is None
        kc.set("S2_API_KEY", "abc-123_XYZ")
        kc.set("S2_API_KEY", "new-value")
        assert kc.keys() == {"S2_API_KEY": "new-value"}
        kc.delete("S2_API_KEY")
        assert kc.get("S2_API_KEY") is None
        with pytest.raises(RuntimeError):
            Keychain(keychain=str(tmp_path / "missing.keychain-db"), service="litrev-app-test").set("S2_API_KEY", "v")
    finally:
        subprocess.run(["/usr/bin/security", "delete-keychain", path])


def test_build_config_reports_problems_with_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(locate, "find_claude", lambda *a, **k: None)
    monkeypatch.setattr(locate, "find_skill", lambda *a, **k: None)
    cfg, problems = config.build_config(Settings(root=str(tmp_path)), appdir=tmp_path, keychain=StubKeychain())
    assert len(problems) == 2 and "Claude Code" in problems[0] and "skill" in problems[1]
    assert cfg.claude == Path("/missing/claude") and cfg.root == tmp_path


def test_build_config_with_everything_found(tmp_path):
    skill = tmp_path / "skill"; skill.mkdir(); (skill / "SKILL.md").write_text("x")
    s = Settings(root=str(tmp_path / "root"), parallel=2, step5_model="haiku", mailto="a@b.co",
                 skill_override=str(skill), wait_s=120, effort="low")
    cfg, problems = config.build_config(s, appdir=tmp_path / "appdir", claude=Path("/bin/echo"),
                                        keychain=StubKeychain({"OPENALEX_API_KEY": "k"}),
                                        overrides={"caffeinate": False})
    assert problems == []
    assert (cfg.skill, cfg.claude, cfg.python) == (skill, Path("/bin/echo"), Path(sys.executable))
    assert (cfg.parallel, cfg.step5_model, cfg.mailto, cfg.wait_s, cfg.keys) == (2, "haiku", "a@b.co", 120, {"OPENALEX_API_KEY": "k"})
    assert cfg.appdir == tmp_path / "appdir" and cfg.caffeinate is False and cfg.effort == "low"


def test_skill_override_parents_must_not_be_open_to_others(tmp_path):
    # a folder that others can write (and that has no sticky bit) lets them rename the skill folder and put their
    # own in its place after the check; /tmp-like sticky folders are fine (others can't move your entries there)
    base = Settings(root=str(tmp_path))
    parent = tmp_path / "shared"
    skill = parent / "skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    skill.chmod(0o755)
    parent.chmod(0o777)
    with pytest.raises(ValueError, match="Other users can change"):
        config.update(base, {"skill_override": str(skill)})
    parent.chmod(0o1777)
    assert config.update(base, {"skill_override": str(skill)}).skill_override == str(skill.resolve())
    parent.chmod(0o755)
