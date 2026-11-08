import json
import os
import stat
from pathlib import Path

from litrev_app import locate


def _exe(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_find_claude_searches_path_then_common_dirs(tmp_path):
    exe = _exe(tmp_path / "bin" / "claude")
    assert locate.find_claude(str(tmp_path / "bin")) == exe
    assert locate.find_claude(str(tmp_path / "nothing")) in (None, *[Path(os.path.expanduser(d)) / "claude" for d in locate.EXTRA_BIN])


def test_plugin_skill_dir_reads_install_path(tmp_path):
    skill = tmp_path / "plug" / "skills" / "reviewing-literature"
    skill.mkdir(parents=True); (skill / "SKILL.md").write_text("x")
    listing = json.dumps([{"id": "other@x", "installPath": "/nope", "enabled": True},
                          {"id": locate.PLUGIN_ID, "installPath": str(tmp_path / "plug"), "enabled": True}])
    assert locate.plugin_skill_dir(listing) == skill
    assert locate.plugin_skill_dir("not json") is None
    assert locate.plugin_skill_dir(json.dumps([{"id": locate.PLUGIN_ID, "installPath": str(tmp_path / "gone")}])) is None


def test_find_skill_prefers_override_then_user_skill(tmp_path):
    own = tmp_path / ".claude" / "skills" / "reviewing-literature"
    own.mkdir(parents=True); (own / "SKILL.md").write_text("x")
    other = tmp_path / "custom"; other.mkdir(); (other / "SKILL.md").write_text("x")
    assert locate.find_skill(None, override=str(other), home=tmp_path) == other
    assert locate.find_skill(None, home=tmp_path) == own
    assert locate.find_skill(None, override=str(tmp_path / "missing"), home=tmp_path) is None


def test_default_root_uses_the_authors_folder_when_present(tmp_path):
    assert locate.default_root(tmp_path) == tmp_path / "LitReviews"
    (tmp_path / "code" / "literatureReview" / "reviews").mkdir(parents=True)
    assert locate.default_root(tmp_path) == tmp_path / "code" / "literatureReview"
