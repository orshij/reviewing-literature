import json

import pytest

from m2help import DATE, HAS_SKILL, candidate, import_papers, make_app, mini_review
from litrev_app import checkpoints as cp
from litrev_app.store import Store

needs_skill = pytest.mark.skipif(not HAS_SKILL, reason="needs the skill")
META = {"caps": {"max_papers": 2, "years": 3, "seminal_citations": 1000, "max_seminal": 10},
        "last_refresh": {"date": DATE}}


def test_validate_refresh():
    ok = {"papers": [{"id": "P003", "recommend": "include", "why": "w"}, {"id": "P004", "recommend": "exclude"}]}
    assert "refresh" in cp.KINDS and cp.validate("refresh", ok) is None
    assert cp.validate("refresh", {"papers": []})
    assert cp.validate("refresh", {"papers": [{"id": "3", "recommend": "include"}]})
    assert cp.validate("refresh", {"papers": [{"id": "P003", "recommend": "maybe"}]})


def test_refresh_answer_commands_golden():
    cmds = cp.answer_commands("refresh", {"approve": ["P003"], "reject": ["P004", "P005"], "raise_cap_to": 3,
                                          "note": ""}, META)
    assert cmds[0][:4] == ["set-meta", "--field", "caps", "--json"]
    assert json.loads(cmds[0][4]) == {**META["caps"], "max_papers": 3}
    assert cmds[1][:5] == ["decide", "--kind", "cap", "--decision", "cap 2 -> 3"] and cmds[1][-2:] == ["--by", "user"]
    assert cmds[2] == ["screen", "--id", "P003", "--decision", "included", "--reason",
                       f"user (Litrev App): approved in the refresh of {DATE}"]
    assert cmds[3] == ["screen", "--id", "P004,P005", "--decision", "excluded", "--reason",
                       f"user (Litrev App): not approved in the refresh of {DATE}"]
    assert cmds[4][:5] == ["decide", "--kind", "screen", "--refs", "P003,P004,P005"]
    assert cmds[4][-2:] == ["--by", "user"]
    assert [c[0] for c in cp.answer_commands("refresh", {"approve": [], "reject": ["P004"]}, META)] == ["screen", "decide"]
    with pytest.raises(ValueError):
        cp.answer_commands("refresh", {"approve": [], "reject": []}, META)
    assert cp.summarize("refresh", {"approve": ["P003"], "reject": ["P004"], "raise_cap_to": 3}) == \
        "approved P003; left out 1; cap raised to 3"


def _validate(app, r) -> str:
    p = app.engine.litrev(r).run(["validate"])
    return (p.stdout or "") + (p.stderr or "")


@needs_skill
def test_refresh_answer_on_a_review_at_its_cap(tmp_path):
    app = make_app(tmp_path)
    trap = mini_review(app, slug="u")                       # cap 2, 2 included
    assert import_papers(app, trap, [candidate("C", 1), candidate("D", 2)]) == ["P003", "P004"]
    lit = app.engine.litrev(trap)
    res = cp.record(lit.run, "refresh", {"approve": ["P003"], "reject": ["P004"], "date": DATE}, lit.meta())
    assert res.ok and "cap exceeded" in _validate(app, trap)    # the trap the form guards against

    r = mini_review(app, slug="t")
    import_papers(app, r, [candidate("C", 1), candidate("D", 2)])
    lit = app.engine.litrev(r)
    res = cp.record(lit.run, "refresh", {"approve": ["P003"], "reject": ["P004"], "raise_cap_to": 3, "date": DATE},
                    lit.meta())
    assert res.ok, res.error
    assert "cap exceeded" not in _validate(app, r)
    P = json.loads((r / "data" / "papers.json").read_text())
    assert (P["P003"]["status"], P["P003"]["manual"], P["P004"]["status"]) == ("included", True, "excluded")
    meta = json.loads((r / "meta.json").read_text())
    assert meta["caps"]["max_papers"] == 3
    assert [(d["kind"], d.get("by")) for d in meta["decisions"]] == [("cap", "user"), ("screen", "user")]


@needs_skill
def test_engine_answer_records_a_parked_refresh_without_resuming_a_step(tmp_path):
    app = make_app(tmp_path)
    r = mini_review(app, cap=5)
    import_papers(app, r, [candidate("C", 1), candidate("D", 2)])
    store = Store(r)
    store.put_ask({"id": "q1", "sid": "S1", "kind": "refresh", "question": "Which?", "status": "parked",
                   "proposal": {"papers": [{"id": "P003", "recommend": "include"}]}})
    res = app.engine.answer(r, {"approve": ["P003"], "reject": ["P004"], "date": DATE})
    assert res.ok, res.error
    st = store.load()
    assert (st.step, st.phase) == (7, "done") and st.answers == ["refresh: approved P003; left out 1"]
    assert store.ask()["status"] == "recorded"
