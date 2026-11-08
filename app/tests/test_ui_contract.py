import json
import shutil
import subprocess

import pytest

from litrev_app import checkpoints
from litrev_app.server import STATIC

HARNESS = r"""
global.window = global;
global.LR = { askForms: {}, h: () => null, md: () => null, fmt: { estimate: (c) => c } };
require(process.argv[2]);
const proposals = JSON.parse(process.argv[3]);
const out = {};
for (const [k, p] of Object.entries(proposals)) out[k] = LR.askShape[k](p);
process.stdout.write(JSON.stringify(out));
"""
PROPOSALS = {
    "scope": {"in": ["monocular depth"], "out": ["stereo"], "protocol": ["zero-shot"], "rationale": "r"},
    "boundaries": {"boundaries": [
        {"area": "event cameras", "aliases": ["neuromorphic"], "papers": ["P012"], "examples": ["t"],
         "recommend": "in", "reason": "useful"},
        {"area": "stereo", "aliases": [], "papers": [], "examples": [], "recommend": "out", "reason": ""}]},
    "included_off_topic": {"papers": [
        {"id": "P003", "title": "t", "screen_reason": "s", "recommend": "drop", "why": "UAV only"},
        {"id": "P004", "title": "t", "screen_reason": "s", "recommend": "keep", "why": "fits"}]},
    "cap": {"current": 40, "proposed": 60, "reason": "many strong papers"},
    "kpi": {"kpis": [
        {"benchmark": "B001", "dataset": None, "metric": "M002", "metric_variant": "@5", "splits": ["test", "none"],
         "rationale": "r", "papers": 7},
        {"benchmark": None, "dataset": "D004", "metric": "M001", "metric_variant": None, "splits": ["val"],
         "rationale": "", "papers": 3}]},
    "free": {"options": ["Keep going", "Stop here"]},
}
META = {"scope": {"in": ["mono depth"], "out": [], "protocol": []}, "caps": {"max_papers": 40, "years": 3},
        "boundaries": [{"area": "event cameras"}, {"area": "neuromorphic"}]}


@pytest.fixture(scope="module")
def shaped(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("harness") / "harness.js"
    harness.write_text(HARNESS)
    p = subprocess.run(["node", str(harness), str(STATIC / "asks.js"), json.dumps(PROPOSALS)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_every_kind_has_a_form_and_a_default_answer():
    src = (STATIC / "asks.js").read_text()
    for kind in [k for k in checkpoints.KINDS if k != "refresh"]:     # refresh's form lives in refresh.js (M2)
        assert f"LR.askForms.{kind} =" in src and f"  {kind}: (p) =>" in src, kind
    # the refresh question's card points to the New papers tab (its form lives there)
    assert "LR.askForms.refresh =" in (STATIC / "refresh.js").read_text()


def test_accepting_the_proposal_gives_the_engine_its_answer_shape(shaped):
    for kind, proposal in PROPOSALS.items():
        assert checkpoints.validate(kind, proposal) is None, kind
        checkpoints.answer_commands(kind, shaped[kind], META)              # raises on a wrong shape
    assert shaped["scope"] == {"in": ["monocular depth"], "out": ["stereo"], "protocol": ["zero-shot"], "notes": ""}
    b = checkpoints.answer_commands("boundaries", shaped["boundaries"], META)
    assert b[0][:7] == ["decide", "--kind", "scope", "--boundary", "event cameras", "--boundary", "neuromorphic"]
    assert b[0][b[0].index("--decision") + 1] == "event cameras: in scope" and b[-1][0] == "scope"
    o = checkpoints.answer_commands("included_off_topic", shaped["included_off_topic"], META)
    assert o[0][:5] == ["screen", "--id", "P003", "--decision", "excluded"] and o[1][4] == "included"
    assert checkpoints.answer_commands("cap", shaped["cap"], META)[0][4] == json.dumps({"max_papers": 60, "years": 3})
    k = checkpoints.answer_commands("kpi", shaped["kpi"], META)
    assert k[0] == ["kpi", "--add", "--benchmark", "B001", "--metric", "M002", "--metric-variant", "@5",
                    "--split", "test", "--split", "none", "--rationale", "r"]
    assert k[1][:4] == ["kpi", "--add", "--dataset", "D004"] and k[-1][:3] == ["decide", "--kind", "kpi"]
    assert shaped["free"] == {"text": "Keep going"}


def test_review_page_contract():
    src = (STATIC / "review.js").read_text()
    assert "LR.pages.r = " in src and "LR.askCard(" in src
    for tab in ("progress", "sessions", "hub"):
        assert f"id: '{tab}'" in src
    assert "LR.features.publish" in src and "/publish`" in src
    assert "sandbox: 'allow-scripts allow-popups allow-downloads'" in src
