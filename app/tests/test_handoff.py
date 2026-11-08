import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apphelpers import recorder
from litrev_app import terminal
from litrev_app.handoff import (command_lines, find_transcript, meta_url, publish_prompt, publish_settings,
                                transcript_url)

KW = dict(review=Path("/r/reviews/t"), hub=Path("/r/reviews/t/rendered/hub.html"), python=Path("/venv/bin/python"),
          litrev=Path("/sk/scripts/litrev.py"))
ATLAS = "https://claude.ai/artifact/OtherArtifact000000000"           # another artifact the hub links to


def test_prompt_first_publish_and_republish():
    new = publish_prompt(**KW, hub_url=None)
    old = publish_prompt(**KW, hub_url="https://claude.ai/artifact/OLD1")
    for p in (new, old):
        assert "artifact-design" in p and '{"downloads": true}' in p and "/r/reviews/t/rendered/hub.html unchanged" in p
        assert "/venv/bin/python /sk/scripts/litrev.py set-meta --review /r/reviews/t --field hub_url --json" in p
    assert "create a new artifact" in new and "OLD1" not in new
    assert "url https://claude.ai/artifact/OLD1" in old and "refused once" in old and "nothing on the live page" in old


def test_settings_allow_only_the_set_meta_command():
    s = publish_settings(python=KW["python"], litrev=KW["litrev"], review=KW["review"])
    assert s["disableAllHooks"] is True
    assert s["permissions"]["allow"] == ["Bash(/venv/bin/python /sk/scripts/litrev.py set-meta *)",
                                         "Read(//r/reviews/t/**)"]
    assert {"Edit(//r/reviews/t/**)", "Write(//r/reviews/t/**)"} <= set(s["permissions"]["deny"])


def test_prompt_goes_first_because_add_dir_is_variadic():
    lines = command_lines(root=Path("/r"), claude=Path("/c/claude"), sid="S1", settings=Path("/a b/settings.json"),
                          review=Path("/r/reviews/t"), prompt="do it")
    assert lines[0] == "cd /r || exit 1"
    argv = shlex.split(lines[1])
    i = argv.index("/c/claude")
    assert argv[:2] == ["exec", "env"] and argv[i + 1] == "do it" and argv[-2:] == ["--add-dir", "/r/reviews/t"]
    assert "CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1" in argv[:i]
    assert "LITREV_APP_REVIEW=/r/reviews/t" in argv[:i]           # security F1: set-meta stays in this review


def test_command_file_runs_with_spaces_and_quotes(tmp_path):
    root = tmp_path / "Or's Reviews"
    review = root / "reviews" / "mde-test"
    (review / "rendered").mkdir(parents=True)
    rec = tmp_path / "rec.json"
    claude = recorder(tmp_path / "my bin" / "claude", rec)
    settings = tmp_path / "Application Support" / "LitrevApp" / "sessions" / "S1" / "settings.json"
    prompt = publish_prompt(review=review, hub=review / "rendered" / "hub.html", python=Path("/venv/bin/python"),
                            litrev=Path("/sk/scripts/litrev.py"), hub_url="https://claude.ai/artifact/OLD1")
    cmd = terminal.write_command(settings.parent / "publish.command", "publish", command_lines(
        root=root, claude=claude, sid="S1", settings=settings, review=review, prompt=prompt), fresh=True)
    subprocess.run(["/bin/sh", str(cmd)], env={"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk",
                                               "ANTHROPIC_BASE_URL": "x", "LITREV_APP_REVIEW": "/elsewhere"},
                   check=True, timeout=30)
    got = json.loads(rec.read_text())
    assert got["argv"] == [prompt, "--session-id", "S1", "--settings", str(settings), "--add-dir", str(review)]
    assert os.path.realpath(got["cwd"]) == os.path.realpath(root)
    assert "ANTHROPIC_API_KEY" not in got["env"] and "ANTHROPIC_BASE_URL" not in got["env"]
    assert got["env"]["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"
    assert got["env"]["LITREV_APP_REVIEW"] == os.path.realpath(review)     # wins over the login shell's


def test_a_fresh_command_file_is_new_and_owner_only(tmp_path):
    # security F4: Terminal runs the publish .command later, as the person. A file or symlink already at its path
    # (planted by a session) fails the write instead of being run or written through
    cmd = terminal.write_command(tmp_path / "S1" / "publish.command", "publish", ["true"], fresh=True)
    assert cmd.stat().st_mode & 0o777 == 0o700 and cmd.read_text().startswith("#!/bin/sh\n")
    with pytest.raises(FileExistsError):
        terminal.write_command(cmd, "publish", ["echo again"], fresh=True)
    assert "again" not in cmd.read_text()
    target = tmp_path / "victim"
    (tmp_path / "link.command").symlink_to(target)                   # dangling: must not be created through
    with pytest.raises(FileExistsError):
        terminal.write_command(tmp_path / "link.command", "publish", ["true"], fresh=True)
    assert not target.exists()


def line(role, content, **top):
    return json.dumps({"type": role, "message": {"role": role, "content": content}, **top})


def tool_use(id_, name, inp):
    return line("assistant", [{"type": "tool_use", "id": id_, "name": name, "input": inp}])


def tool_result(id_, content, is_error=False, **top):
    return line("user", [{"type": "tool_result", "tool_use_id": id_, "content": content, "is_error": is_error}], **top)


def test_transcript_url_takes_only_this_hubs_artifact_result(tmp_path):
    root = tmp_path / "root"
    hub = root / "reviews" / "t" / "rendered" / "hub.html"
    lines = [
        line("user", "Republish in place by passing url https://claude.ai/artifact/OLD1"),
        tool_use("r1", "Read", {"file_path": str(hub)}),
        tool_result("r1", f'<a href="{ATLAS}">How this review was built</a>'),
        tool_use("a1", "Artifact", {"file_path": str(hub), "url": "https://claude.ai/artifact/OLD1"}),
        tool_result("a1", "You hadn't viewed the live version of this artifact, so the publish was refused.",
                    is_error=True),
        tool_use("a2", "Artifact", {"file_path": str(tmp_path / "other.html")}),
        tool_result("a2", "Published other.html at https://claude.ai/artifact/OTHER9 (Version 1)"),
    ]
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(lines) + "\n")
    assert transcript_url(t, hub, root) is None
    lines += [tool_use("a3", "Artifact", {"file_path": str(hub), "url": "https://claude.ai/artifact/OLD1",
                                          "capabilities": {"downloads": True}}),
              tool_result("a3", f"Published {hub} at https://claude.ai/artifact/NEW2 (Version 5, version id 1-2)",
                          toolUseResult={"url": "https://claude.ai/artifact/NEW2", "path": str(hub)})]
    t.write_text("\n".join(lines) + "\nnot json\n")
    assert transcript_url(t, hub, root) == "https://claude.ai/artifact/NEW2"


def test_transcript_url_with_a_relative_path_and_a_code_link(tmp_path):
    root = tmp_path / "root"
    hub = root / "reviews" / "t" / "rendered" / "hub.html"
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join([
        tool_use("a1", "Artifact", {"file_path": "reviews/t/rendered/hub.html"}),
        tool_result("a1", [{"type": "text", "text": "Published at https://claude.ai/code/artifact/0a1b-2c3d"}])]) + "\n")
    assert transcript_url(t, hub, root) == "https://claude.ai/code/artifact/0a1b-2c3d"
    assert transcript_url(tmp_path / "missing.jsonl", hub, root) is None


def test_meta_url_needs_a_set_meta_at_or_after_the_start():
    since = datetime(2026, 10, 10, 20, 0, 0, tzinfo=timezone.utc).timestamp() + 0.4
    m = {"hub_url": "https://claude.ai/artifact/X", "step_log": {"set-meta:hub_url": "2026-10-10T20:00:00Z"}}
    assert meta_url(m, since) == "https://claude.ai/artifact/X"           # same second counts
    m["step_log"]["set-meta:hub_url"] = "2026-10-10T19:59:59Z"
    assert meta_url(m, since) is None
    assert meta_url({"hub_url": "x"}, since) is None and meta_url({}, since) is None


def test_find_transcript(tmp_path):
    (tmp_path / "-Users-x-root").mkdir()
    (tmp_path / "-Users-x-root" / "S1.jsonl").write_text("")
    assert find_transcript(tmp_path, "S1") == tmp_path / "-Users-x-root" / "S1.jsonl"
    assert find_transcript(tmp_path, "S2") is None
