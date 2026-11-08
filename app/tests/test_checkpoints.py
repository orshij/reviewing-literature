import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from litrev_app import checkpoints as cp

SKILL = Path(os.environ.get("LITREV_SKILL", Path.home() / ".claude/skills/reviewing-literature"))


def test_validate_shapes():
    assert cp.validate("scope", {"in": ["a"], "out": [], "protocol": [], "rationale": "r"}) is None
    assert "in" in cp.validate("scope", {"in": [], "out": []})
    assert cp.validate("boundaries", {"boundaries": [{"area": "x", "recommend": "in", "papers": ["P001"]}]}) is None
    assert cp.validate("boundaries", {"boundaries": [{"area": "x", "recommend": "maybe"}]})
    assert cp.validate("included_off_topic", {"papers": [{"id": "P012", "recommend": "drop"}]}) is None
    assert cp.validate("included_off_topic", {"papers": [{"id": "12", "recommend": "drop"}]})
    assert cp.validate("cap", {"current": 40, "proposed": 60, "reason": "r"}) is None
    assert cp.validate("cap", {"current": 40, "proposed": 0})
    assert cp.validate("kpi", {"kpis": [{"benchmark": "B001", "metric": "M001", "splits": ["test"]}]}) is None
    assert cp.validate("kpi", {"kpis": [{"benchmark": "B001", "dataset": "D001", "metric": "M001"}]})
    assert cp.validate("free", {"options": []}) is None
    assert cp.validate("nope", {}) == "unknown kind nope"


def test_answer_commands_golden():
    meta = {"scope": {"in": ["mono depth"], "out": ["stereo"], "protocol": ["zero-shot"]},
            "caps": {"max_papers": 40, "years": 3, "seminal_citations": 1000, "max_seminal": 10},
            "boundaries": [{"area": "event cameras"}, {"area": "neuromorphic"}, {"area": "stereo"}],
            "kpis": [{"id": "K001", "benchmark_id": "B009", "dataset_id": None, "metric_id": "M009", "metric_variant": None,
                      "splits": ["test"]}]}
    assert cp.answer_commands("scope", {"in": ["a", "b"], "out": ["c"], "protocol": [], "notes": ""}, meta) == [
        ["scope", "--in", "a", "--in", "b", "--out", "c", "--notes", "Accepted Claude's proposal (Litrev App)", "--by", "user"]]
    b = cp.answer_commands("boundaries", {"boundaries": [
        {"area": "event cameras", "aliases": ["neuromorphic"], "decision": "in", "reason": "useful"},
        {"area": "stereo", "aliases": [], "decision": "out", "reason": ""}]}, meta)
    assert b[0][:6] == ["decide", "--kind", "scope", "--boundary", "event cameras", "--boundary"]
    assert b[-1][:5] == ["scope", "--in", "mono depth", "--in", "event cameras"] and "--by" in b[-1]
    c = cp.answer_commands("cap", {"proposed": 60, "reason": "r"}, meta)
    assert json.loads(c[0][4]) == {**meta["caps"], "max_papers": 60} and c[1][:3] == ["decide", "--kind", "cap"]
    k = cp.answer_commands("kpi", {"kpis": [{"benchmark": "B001", "metric": "M002", "metric_variant": "@5",
                                             "splits": ["test", "none"], "rationale": "r"}], "remove": ["K001"]}, meta)
    assert k[0] == ["kpi", "--remove", "K001"]
    assert k[1] == ["kpi", "--add", "--benchmark", "B001", "--metric", "M002", "--metric-variant", "@5",
                    "--split", "test", "--split", "none", "--rationale", "r"]
    o = cp.answer_commands("included_off_topic", {"papers": [{"id": "P003", "decision": "drop", "reason": "UAV only"}]}, meta)
    assert o[0] == ["screen", "--id", "P003", "--decision", "excluded", "--reason", "user (Litrev App): UAV only"]
    assert o[1][:5] == ["decide", "--kind", "screen", "--refs", "P003"]
    assert cp.answer_commands("free", {"text": "x"}, meta) == []


def _review(tmp_path):
    env = {**os.environ, "LITREV_HOME": str(tmp_path)}
    r = tmp_path / "reviews" / "t"
    subprocess.run([sys.executable, str(SKILL / "scripts/litrev.py"), "init", "--path", str(r), "--slug", "t",
                    "--purpose", "p", "--seed", "s"], check=True, capture_output=True, env=env)
    def run(argv):
        return subprocess.run([sys.executable, str(SKILL / "scripts/litrev.py"), *argv, "--review", str(r)],
                              capture_output=True, text=True, env=env)
    return r, run


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_record_scope_with_the_real_cli(tmp_path):
    r, run = _review(tmp_path)
    res = cp.record(run, "scope", {"in": ["monocular depth"], "out": ["stereo"], "protocol": ["zero-shot"], "notes": "ok"}, {})
    assert res.ok and res.reply.startswith("User answered; already recorded with: scope")
    meta = json.loads((r / "meta.json").read_text())
    assert meta["scope"]["in"] == ["monocular depth"] and any(d.get("by") == "user" for d in meta["decisions"])


@pytest.mark.skipif(not (SKILL / "scripts/litrev.py").exists(), reason="needs the skill")
def test_record_stops_at_the_first_failing_command(tmp_path):
    r, run = _review(tmp_path)
    res = cp.record(run, "kpi", {"kpis": [{"benchmark": "B999", "metric": "M999", "splits": ["test"]}]}, {})
    assert not res.ok and res.error and res.reply == "" and len(res.outputs) == 1


def test_boundary_aliases_are_limited_to_stored_areas():
    # litrev `decide --boundary X` fails unless X is a stored boundary area (case/space-insensitive)
    meta = {"boundaries": [{"area": "Event  Cameras"}, {"area": "neuromorphic sensing"}]}
    cmds = cp.answer_commands("boundaries", {"boundaries": [
        {"area": "event cameras", "aliases": ["Neuromorphic Sensing", "spiking vision"], "decision": "out", "reason": "r"}]}, meta)
    argv = cmds[0]
    assert argv.count("--boundary") == 2 and "Neuromorphic Sensing" in argv and "spiking vision" not in argv


def test_kpi_retry_skips_what_is_already_recorded():
    # M1b review: a rejected KPI answer was half-applied; every retry then failed on "already exists" / "is not a KPI"
    meta = {"kpis": [{"id": "K001", "benchmark_id": "B001", "dataset_id": None, "metric_id": "M001", "metric_variant": None,
                      "splits": ["test"]}]}
    cmds = cp.answer_commands("kpi", {"kpis": [
        {"benchmark": "B001", "metric": "M001", "splits": ["test"], "rationale": "r"},
        {"benchmark": "B002", "metric": "M001", "splits": ["test"], "rationale": "r"}], "remove": ["K003"]}, meta)
    adds = [c for c in cmds if c[:2] == ["kpi", "--add"]]
    assert len(adds) == 1 and "B002" in adds[0]
    assert not any(c[:2] == ["kpi", "--remove"] for c in cmds)


def test_boundary_answer_uses_stored_names_and_skips_decided_areas():
    # live run 2026-10-11: Claude grouped stored areas under a new umbrella name; `decide --boundary <new name>` failed
    # ("no boundary ...") after the first two areas had been recorded, so a retry would also duplicate those
    meta = {"boundaries": [{"area": "robust monocular depth under adverse conditions", "decision_id": "DEC003"},
                           {"area": "depth estimation from thermal or event cameras"},
                           {"area": "event and thermal camera depth estimation"},
                           {"area": "omnidirectional 360 depth", "decision_id": "DEC005"}]}
    cmds = cp.answer_commands("boundaries", {"boundaries": [
        {"area": "robust monocular depth under adverse conditions", "aliases": [], "decision": "in", "reason": "r"},
        {"area": "depth from thermal, event or other non-RGB cameras", "decision": "out", "reason": "r",
         "aliases": ["depth estimation from thermal or event cameras", "Event and thermal camera depth estimation"]},
        {"area": "omnidirectional (360) panoramic depth", "aliases": ["omnidirectional 360 depth"], "decision": "out"},
        {"area": "brand-new area", "aliases": [], "decision": "out", "reason": "r"}]}, meta)
    decides = [c for c in cmds if c[0] == "decide"]
    assert len(decides) == 2                                   # adverse and omnidirectional were decided already
    thermal = decides[0]
    assert thermal.count("--boundary") == 2 and "depth from thermal, event or other non-RGB cameras" not in thermal
    assert "--boundary" not in decides[1] and "brand-new area" in " ".join(decides[1])   # nothing stored: plain decision
    scope = [c for c in cmds if c[0] == "scope"]               # decided earlier, but still widens scope.in
    assert len(scope) == 1 and "robust monocular depth under adverse conditions" in scope[0]


def test_in_area_decided_before_a_failure_still_widens_the_scope():
    # live run: the first try recorded "adverse conditions: in scope" (DEC003) but failed before the scope command;
    # the retry skips the decided boundary and must still add the area to scope.in
    meta = {"scope": {"in": ["monocular depth"], "out": [], "protocol": []},
            "boundaries": [{"area": "robust depth under adverse conditions", "decision_id": "DEC003"}]}
    cmds = cp.answer_commands("boundaries", {"boundaries": [
        {"area": "robust depth under adverse conditions", "aliases": [], "decision": "in", "reason": "r"}]}, meta)
    assert [c[0] for c in cmds] == ["scope"] and "robust depth under adverse conditions" in cmds[0]
    meta["scope"]["in"].append("Robust depth under adverse conditions")              # already widened: nothing to do
    assert cp.answer_commands("boundaries", {"boundaries": [
        {"area": "robust depth under adverse conditions", "aliases": [], "decision": "in", "reason": "r"}]}, meta) == []


def _k(kid, bench, splits, metric="M010", variant=None):
    return {"id": kid, "benchmark_id": bench, "dataset_id": None, "metric_id": metric, "metric_variant": variant,
            "splits": splits}


def test_kpi_answer_replaces_a_board_whose_splits_changed():
    """Live M2 run (refresh, step 6): Claude proposed K006-K008 with P786's split label added. The recorder only
    added what wasn't recorded, so it made near-duplicate boards K009/K010 and stopped at "max 6 KPIs"."""
    meta = {"kpis": [_k("K005", "B005", ["test"], "M002"), _k("K006", "B007", ["a", "b"]),
                     _k("K007", "B008", ["a", "c"]), _k("K008", "B009", ["a", "d"])]}
    ans = {"kpis": [{"benchmark": "B007", "metric": "M010", "splits": ["a", "b", "new"]},
                    {"benchmark": "B008", "metric": "M010", "splits": ["a", "c", "new"]},
                    {"benchmark": "B009", "metric": "M010", "splits": ["a", "d", "new"]}]}
    cmds = cp.answer_commands("kpi", ans, meta)
    removes = [c[2] for c in cmds if c[:2] == ["kpi", "--remove"]]
    adds = [c for c in cmds if c[:2] == ["kpi", "--add"]]
    assert removes == ["K006", "K007", "K008"] and len(adds) == 3
    assert cmds.index(["kpi", "--remove", "K008"]) < cmds.index(adds[0])     # removes first: never over 6 boards
    assert "K005" not in removes and cmds[-1][:3] == ["decide", "--kind", "kpi"]


def test_kpi_answer_cleans_the_duplicates_the_old_recorder_left():
    meta = {"kpis": [_k("K005", "B005", ["test"], "M002"), _k("K006", "B007", ["a", "b"]),
                     _k("K007", "B008", ["a", "c"]), _k("K008", "B009", ["a", "d"]),
                     _k("K009", "B007", ["a", "b", "new"]), _k("K010", "B008", ["a", "c", "new"])]}
    ans = {"kpis": [{"benchmark": "B007", "metric": "M010", "splits": ["a", "b", "new"]},
                    {"benchmark": "B008", "metric": "M010", "splits": ["a", "c", "new"]},
                    {"benchmark": "B009", "metric": "M010", "splits": ["a", "d", "new"]}]}
    cmds = cp.answer_commands("kpi", ans, meta)
    assert [c[2] for c in cmds if c[:2] == ["kpi", "--remove"]] == ["K006", "K007", "K008"]
    assert [c[c.index("--benchmark") + 1] for c in cmds if c[:2] == ["kpi", "--add"]] == ["B009"]


def test_kpi_answer_keeps_a_same_metric_board_on_other_splits():
    meta = {"kpis": [_k("K001", "B007", ["dev"]), _k("K002", "B007", ["test"])]}
    cmds = cp.answer_commands("kpi", {"kpis": [{"benchmark": "B007", "metric": "M010", "splits": ["test", "test (500)"]}]},
                              meta)
    assert [c[2] for c in cmds if c[:2] == ["kpi", "--remove"]] == ["K002"]
