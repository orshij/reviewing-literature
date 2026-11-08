"""Helpers shared by the M1c tests (doctor, publish, scripts)."""
import json
import subprocess
import sys
from pathlib import Path

AGENTS = ("litrev-details", "litrev-reader", "litrev-dataset", "litrev-screener")


class FakeRun:
    """Stands in for doctor_checks.run_cmd: answers an argv by the first key found in it, records (argv, env)."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def __call__(self, argv, env=None, timeout=60):
        argv = [str(a) for a in argv]
        self.calls.append((argv, env))
        line = " ".join(argv)
        for key, (code, out, err) in self.answers.items():
            if key in line:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 127, "", "not found")


def make_skill(d: Path, version: str | None = "1.19.0", agents=AGENTS, app_mode: bool = True) -> Path:
    """A minimal skill folder the doctor accepts (version None: no SKILL_VERSION line)."""
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "agents").mkdir(exist_ok=True)
    (d / "SKILL.md").write_text("skill")
    if app_mode:
        (d / "app-mode.md").write_text("app mode")
    const = f'SKILL_VERSION = "{version}"    # bump with plugin.json\n' if version else ""
    (d / "scripts" / "litrev.py").write_text("import os\n" + const)
    for a in agents:
        (d / "agents" / f"{a}.md").write_text(
            f"---\nname: {a}\ndescription: d\ntools: Read\nmodel: inherit\n---\n\nbody\n")
    return d


def recorder(path: Path, record: Path) -> Path:
    """An executable (a fake `claude`) that writes its argv, cwd and environment to `record` as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}}, "
                    f"open({str(record)!r}, 'w'))\n")
    path.chmod(0o755)
    return path


def write_publish_transcript(projects: Path, sid: str, hub: Path, url: str) -> Path:
    """What Claude Code writes to its transcript when the person approved the publish of `hub`."""
    d = projects / "-root"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a1", "name": "Artifact",
             "input": {"file_path": str(hub), "capabilities": {"downloads": True}}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a1", "content": f"Published {hub} at {url} (Version 1)"}]},
            "toolUseResult": {"url": url, "path": str(hub)}}),
    ]) + "\n")
    return p
