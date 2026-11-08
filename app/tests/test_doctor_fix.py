import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from apphelpers import make_skill, recorder
from litrev_app import doctor_fix as fx
from litrev_app.doctor_checks import Inputs, check_pwc


def inputs(tmp_path, claude):
    return Inputs(claude=claude, skill=make_skill(tmp_path / "skill"), python=Path("/venv/bin/python"),
                  root=tmp_path / "root", appdir=tmp_path / "App Support" / "LitrevApp", home=tmp_path / "home",
                  env={"PATH": "/usr/bin:/bin"})


def test_login_opens_a_terminal_command_without_the_api_key(tmp_path):
    rec = tmp_path / "rec.json"
    claude = recorder(tmp_path / "my bin" / "claude", rec)
    (tmp_path / "home").mkdir()
    opened = []
    res = fx.fix("auth", inputs(tmp_path, claude), opener=opened.append)
    assert res["started"] and opened == [tmp_path / "App Support" / "LitrevApp" / "login.command"]
    assert os.access(opened[0], os.X_OK)
    subprocess.run(["/bin/sh", str(opened[0])], env={"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk",
                                                     "CLAUDE_CODE_USE_VERTEX": "1"}, check=True, timeout=30)
    got = json.loads(rec.read_text())
    assert got["argv"] == ["auth", "login", "--claudeai"]
    assert "ANTHROPIC_API_KEY" not in got["env"] and "CLAUDE_CODE_USE_VERTEX" not in got["env"]
    assert os.path.realpath(got["cwd"]) == os.path.realpath(tmp_path / "home")


def test_update_and_unknown_fixes(tmp_path):
    opened = []
    assert fx.fix("claude", inputs(tmp_path, Path("/opt/claude")), opener=opened.append)["started"]
    assert "/opt/claude update" in opened[0].read_text()
    assert not fx.fix("auth", inputs(tmp_path, None), opener=opened.append)["started"]
    with pytest.raises(KeyError):
        fx.fix("root", inputs(tmp_path, Path("/opt/claude")), opener=opened.append)


def fake_hf(calls):
    counts = {"evaluation-tables": 2, "links-between-paper-and-code": 1, "datasets": 1}

    def get_json(url):
        name = url.split("/pwc-archive/")[1].split("/")[0]
        return {"default": {"train": [f"https://hf/{name}/{i}.parquet" for i in range(counts[name])]}}

    def download(url, dest):
        calls.append(url)
        dest.write_bytes(b"PAR1")
    return get_json, download


def test_build_pwc_downloads_the_shards_then_indexes(tmp_path):
    inp = inputs(tmp_path, Path("/opt/claude"))
    cache = tmp_path / "root" / "cache" / "pwc"
    cache.mkdir(parents=True)
    (cache / "eval_0.parquet").write_bytes(b"PAR1")                     # already there: not downloaded again
    calls, progress = [], []
    get_json, download = fake_hf(calls)

    def run(argv, env=None, timeout=60):
        assert argv[-1] == "pwc-index" and env["LITREV_HOME"] == str(tmp_path / "root")
        (cache / "eval_index.json").write_text("{}")
        return subprocess.CompletedProcess(argv, 0, '{"rows": 3, "keys": 2}\n', "")
    out = fx.build_pwc(inp, progress.append, run=run, get_json=get_json, download=download)
    assert out == {"rows": 3, "keys": 2}
    assert sorted(p.name for p in cache.glob("*.parquet")) == [
        "ds_0.parquet", "eval_0.parquet", "eval_1.parquet", "links_0.parquet"]
    assert "https://hf/evaluation-tables/0.parquet" not in calls and len(calls) == 3
    assert not list(cache.glob("*.part")) and progress[-1].startswith("indexing")


def test_pwc_fix_is_a_background_job(tmp_path):
    inp = inputs(tmp_path, Path("/opt/claude"))
    jobs, gate, calls = fx.Jobs(), threading.Event(), []
    get_json, download = fake_hf(calls)

    def run(argv, env=None, timeout=60):
        gate.wait(10)
        return subprocess.CompletedProcess(argv, 1, "", "pyarrow missing")
    kw = dict(jobs=jobs, run=run, get_json=get_json, download=download)
    assert fx.fix("pwc", inp, **kw)["started"]
    assert not fx.fix("pwc", inp, **kw)["started"]                     # one build at a time
    assert check_pwc(inp, jobs.snapshot()["pwc"]).detail.get("building")
    gate.set()
    jobs.join("pwc", 10)
    snap = jobs.snapshot()["pwc"]
    assert snap["state"] == "failed" and "pyarrow missing" in snap["error"]
    assert "pyarrow missing" in check_pwc(inp, snap).message
