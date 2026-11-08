"""Step 8 (opt-in): the presentation's chapter plan and narration script. Run: python3 -m pytest tests -q"""
import json
import re

import pytest

from test_litrev import DIAGRAM_OK, make_review, step6_full, write_explainer, write_limitations, write_synthesis  # sets sys.path
import litrev  # noqa: E402


def note(value, quote):
    return litrev.evidence(value, status="author-reported", source="cached text", checked="2026-10-26", quote=quote,
                           location="Sec. 3")


def report_doc(ps, **over):
    """A review report (step 7) the fixture review accepts: every part, the refs exist, no number outside the packet."""
    d = {"executive_summary": {k: {"text": f"The {k.replace('_', ' ')} part of the summary.", "refs": ["TK1"]}
                               for k in litrev.PRES_EXEC_KEYS},
         "verdict": [{"goal": "Accuracy indoors", "choice": "A diffusion prior", "cluster_ids": ["C001"],
                      "principle": "A generator trained on many images already knows the shapes of indoor scenes.",
                      "condition": "when synthetic training data is available", "support": "few", "refs": ["K001", ps[0]["id"]]},
                     {"goal": "A simple first system", "choice": "A plain regression network", "cluster_ids": ["C002"],
                      "principle": "One forward pass and one loss leave little to tune.", "condition": "when speed matters more than detail",
                      "support": "untested", "refs": ["C002"]}],
         "requirements": {"application_goal": "Depth from one photo for an indoor robot.", "formal_task": "Monocular depth estimation.",
                          "inputs_outputs": "One RGB image in; a depth map out.", "operating_conditions": "not stated in the review",
                          "constraints": "not stated in the review", "research_questions": ["Which family is more accurate indoors?"]},
         "unmeasured": [{"text": "No test set measures the runtime on a robot.", "refs": ["M001"]}],
         "conditions": [{"kind": "works", "text": "The diffusion prior leads the board.", "refs": ["K001", ps[0]["id"]]},
                        {"kind": "unresolved", "text": "The two families train on different data.", "refs": ["L1"]}],
         "gaps": [{"type": "evidence-gap", "text": "The reviewed studies do not establish outdoor accuracy.", "refs": ["L1"]},
                  {"type": "engineering-gap", "text": "No code release was found for the best papers.", "refs": [ps[0]["id"]]},
                  {"type": "research-opportunity", "text": "A shared test protocol could settle the comparison.", "refs": ["OP1"]}],
         "roadmap": {"baseline": {"paper_id": ps[2]["id"], "reason": "It is the simplest family to run."},
                     "shortlist": [{"paper_id": ps[0]["id"], "reason": "It leads the board."}],
                     "experiments": [{"experiment": "Run the baseline on our own photos.", "question": "How large is the gap to the board?",
                                      "decision": "Whether adaptation is necessary.", "needs": "A small labelled set of target photos.",
                                      "criteria": "Stop when the gap is small.", "refs": [ps[2]["id"]]},
                                     {"experiment": "Compare both families under matched data.", "question": "Which family gives the better trade-off?",
                                      "decision": "Which family to develop.", "needs": "The same training data for both.",
                                      "criteria": "Continue with the family that wins on the target set.", "refs": ["C001", "C002"]}]}}
    d.update(over)
    return d


def write_report(rv, doc):
    d = rv.path / "data" / "report"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(doc))


def pres_review(tmp_path, minutes=5, report=True):
    """step6_full with a method diagram on C001, data and loss notes on both best papers (three of the five dimensions),
    the synthesis, the limitations and the review report merged, and the presentation wanted."""
    rv, ps = step6_full(tmp_path)
    write_explainer(rv, "C001", ps[0]["id"], diagram=DIAGRAM_OK)
    litrev.merge_explainers(rv)
    for p in (ps[0], ps[2]):
        p["data"] = note("Synthetic indoor scenes rendered from 3D assets.", "synthetic indoor scenes")
        p["loss"] = note("A scale-invariant depth loss.", "scale-invariant loss")
    write_synthesis(rv, ps)
    litrev.merge_synthesis(rv)
    write_limitations(rv, ps)
    litrev.merge_limitations(rv)
    if report:
        write_report(rv, report_doc(ps))
        rep = litrev.merge_report(rv)
        assert rep["errors"] == [], rep
    litrev.presentation_set(rv, minutes=minutes)
    return rv, ps


def chapter(cid, start, end, sections, visual, **over):
    c = {"id": cid, "title": f"Why chapter {cid[-1]} matters", "question": "What does this part answer?",
         "sections": sections, "start_s": start, "end_s": end, "prior_knowledge": "none",
         "explanation": "the idea", "evidence": {"supporting": [], "challenging": []}, "comparability": "",
         "cluster_ids": [], "deep_dive": None, "visual": visual, "visual_sequence": "", "example": "",
         "engagement": None, "takeaway": "one line", "transition": "next"}
    c.update(over)
    return c


def good_plan(ps):
    """A 5-minute plan over the ten review sections. The family chapters CH04 and CH05 carry section 7 (results) here,
    so the script tests can write them without data scenes; section 6 and its dimensions scene sit on CH07."""
    return {"schema": 2, "audience": "vision researchers", "outcome": "After this video the audience can choose a family.",
            "organizing_axis": "methods", "axis_reason": "the families differ by prior",
            "headline_answer": ["Both families reach the top of the board.", "Evidence is thin."],
            "recurring_example": {"text": "One kitchen photo.", "illustrative": True, "refs": []},
            "chapters": [
                chapter("CH01", 0, 30, [1], {"kind": "executive-summary", "refs": [], "message": "the answer"},
                        summaries=["executive-summary", "verdict"]),
                chapter("CH02", 30, 60, [2, 3], {"kind": "evidence-flow", "refs": [], "message": "how papers were found"},
                        summaries=["requirements", "scope", "search"]),
                chapter("CH03", 60, 90, [4, 5], {"kind": "taxonomy", "refs": [], "message": "two families"},
                        summaries=["benchmarks", "metrics", "unmeasured"]),
                chapter("CH04", 90, 165, [7], {"kind": "diagram", "refs": ["C001"], "message": "how it works"},
                        cluster_ids=["C001"], evidence={"supporting": [ps[0]["id"], "R001"], "challenging": []},
                        engagement={"kind": "prediction", "prompt": "Which family wins?"}),
                chapter("CH05", 165, 225, [7], {"kind": "kpi-chart", "refs": ["K001"], "message": "the board"},
                        cluster_ids=["C002"]),
                chapter("CH06", 225, 270, [7, 8, 9], {"kind": "matrix", "refs": [], "message": "shared problems"},
                        summaries=["provenance", "conditions", "feasibility", "gaps"]),
                chapter("CH07", 270, 300, [6, 10], {"kind": "roadmap", "refs": [], "message": "what to do"},
                        summaries=["dimensions", "roadmap"])],
            "folded_clusters": [], "companion": ["the hub's Results tab"], "missing_information": []}


def write_plan(rv, plan):
    d = rv.path / "data" / "presentation"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(plan))


SENT = "This family reuses a pretrained image generator to predict depth from one photo."   # 13 words


def scene(sid, n, visual=None, **over):
    s = {"id": sid, "narration": [SENT] * n, "visual": visual or {"kind": "diagram", "refs": ["C001"], "reveal": []},
         "on_screen": "", "claim_type": "reported", "sources": ["P001"], "pause_after_s": 0.5, "transition": "cut",
         "engagement": None}
    s.update(over)
    return s


def write_script(rv, ch, scenes, prons=None):
    d = rv.path / "data" / "presentation" / "script"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ch}.json").write_text(json.dumps({"schema": 1, "chapter_id": ch, "scenes": scenes, "pronunciations": prons or []}))


def merged_plan(tmp_path):
    rv, ps = pres_review(tmp_path)
    write_plan(rv, good_plan(ps))
    rep = litrev.merge_presentation_plan(rv)
    assert rep["errors"] == [], rep
    return rv, ps


def rows8(rv):
    return {r["key"]: r for r in litrev.review_status(rv)["rows"] if r["step"] == 8}


# ---------- opt-in ----------

def test_presentation_opt_in_and_status_rows(tmp_path):
    rv, ps = step6_full(tmp_path)
    assert rows8(rv) == {}                                            # never asked: no step-8 rows at all
    litrev.presentation_set(rv, minutes=20)
    p = rv.meta["presentation"]
    assert p["wanted"] is True and p["minutes"] == 20 and p["by"] == "user" and p["audience"] == litrev.PRES_DEFAULT_AUDIENCE
    r = rows8(rv)
    assert r["plan"]["state"] == "todo" and "presentation-input" in r["plan"]["next"]
    assert r["script"]["state"] == "todo"
    litrev.presentation_set(rv, minutes=20, audience="product managers", by="agent")
    assert rv.meta["presentation"]["audience"] == "product managers" and rv.meta["presentation"]["by"] == "agent"
    litrev.presentation_set(rv, off=True)
    assert rv.meta["presentation"]["wanted"] is False and rows8(rv) == {}
    with pytest.raises(SystemExit, match="minutes"):
        litrev.presentation_set(rv, minutes=2)
    errs, warns = litrev.validate_review(rv)
    assert not any(re.search(r"\bpresentation\b", x) for x in errs + warns)   # opt-in: absence is never a warning


# ---------- 8.1 chapter plan ----------

def test_presentation_input_packet(tmp_path):
    rv, ps = pres_review(tmp_path, minutes=20)
    pk = litrev.presentation_input(rv)
    t = pk["target"]
    assert t["seconds"] == 1200 and t["wpm"] == litrev.PRES_WPM and sum(b["seconds"] for b in t["allocation"]) == 1200
    assert pk["review"]["purpose"] == "test purpose"
    flow = {s["stage"]: s["n"] for s in pk["evidence_base"]["flow"]}
    assert flow["included"] == 4 and flow["found"] == 4 and "not a systematic review" in pk["evidence_base"]["note"]
    c1 = next(c for c in pk["clusters"] if c["id"] == "C001")
    assert [n["id"] for n in c1["diagram_nodes"]] == ["g", "s", "f", "p"] and c1["has_figure"] is True
    assert c1["explainer"]["headline"] and c1["winner"] == ps[0]["id"]
    assert [x["kind"] for x in c1["winner_weak_points"]] == ["evaluation", "cost"]
    assert pk["synthesis"][0]["id"] == "TK1" and pk["limitations"]["problems"][0]["id"] == "L1"
    assert pk["limitations"]["open_problems"][0]["id"] == "OP1"
    assert pk["ranking"][0]["paper_id"] == ps[0]["id"] and pk["ranking"][0]["per_kpi"]["K001"]["value"] == 0.05
    assert set(pk["visual_kinds"]) == set(litrev.VISUAL_KINDS) and set(pk["sections"]) == {str(i) for i in range(1, 11)}
    assert pk["sections"]["5"]["data_scenes"] == ["benchmarks", "metrics", "unmeasured"] and pk["data_scene_min_s"] == 20
    assert pk["report"]["roadmap"]["experiments"][0]["id"] == "X1" and pk["provenance"][0]["id"] == "K001"
    assert c1["winner_dimensions"].keys() == {"data", "topology", "loss"} and pk["feasibility"][0]["id"] == ps[0]["id"]
    assert pk["output_path"].endswith("data/presentation/plan.json")
    assert len(json.dumps(pk)) < 60000
    rv2, _ = step6_full(tmp_path / "nosyn")
    with pytest.raises(SystemExit, match="synthesis"):
        litrev.presentation_input(rv2)
    rv3, _ = pres_review(tmp_path / "norep", report=False)
    with pytest.raises(SystemExit, match="review report"):
        litrev.presentation_input(rv3)


def test_allocation_scales_to_other_lengths():
    a = litrev.pres_allocation(12)
    assert sum(b["seconds"] for b in a) == 720 and [b["section"] for b in a] == list(range(1, 11))
    # methods at 5:00 in 20 minutes, the search log under a minute (an outside review of the first 30-minute video,
    # 2026-10-26); section 1 holds the summary and the top-down verdict, two data scenes (2026-10-28)
    assert [b["seconds"] for b in litrev.pres_allocation(20)] == [100, 40, 30, 70, 60, 470, 130, 80, 90, 130]
    assert sum(b["seconds"] for b in litrev.pres_allocation(20)[:5]) == 300
    for m in (5, 10, 20, 30):          # each section's data scenes fit its share of the time (sections 2 and 3 share
        a = {b["section"]: b["seconds"] for b in litrev.pres_allocation(m)}          # a chapter in a short video)
        need = {n: len(ks) * litrev._pres_scene_min_s(m) for n, ks in litrev.PRES_SECTION_SCENES.items() if n != 6}
        if m < 20:
            a[2], need[2], need[3] = a[2] + a.pop(3), need[2] + need.pop(3), 0
        assert all(a[n] >= need[n] for n in need if n in a), m
    assert all(sum(v) == k * 60 for k, v in litrev.PRES_ALLOC.items())


def test_merge_presentation_plan_stores_it_and_status_moves_on(tmp_path):
    rv, ps = merged_plan(tmp_path)
    pres = litrev.load_presentation(rv)
    assert [c["id"] for c in pres["plan"]["chapters"]][:2] == ["CH01", "CH02"]
    assert pres["plan"]["target_s"] == 300 and pres["plan"]["merged_at"]
    r = rows8(rv)
    assert r["plan"]["state"] == "done"
    assert r["script"]["state"] == "todo" and "CH01" in r["script"]["detail"] and "script-input" in r["script"]["next"]


def test_merge_presentation_plan_errors_change_nothing(tmp_path):
    rv, ps = merged_plan(tmp_path)
    before = (rv.path / "data" / "presentation" / "merged.json").read_text()
    bad = good_plan(ps)
    bad["chapters"][1]["start_s"] = 35                                  # gap after CH01
    bad["chapters"][-1]["end_s"] = 290                                  # total 290 s, target 300 s
    bad["chapters"][-1]["sections"] = [6]                               # nobody carries section 10
    bad["chapters"][4]["cluster_ids"] = []                              # C002 in no chapter and not folded
    bad["chapters"][2]["visual"] = {"kind": "slideshow", "refs": []}    # unknown kind
    bad["chapters"][3]["visual"] = {"kind": "diagram", "refs": [ps[0]["id"]]}   # a diagram takes a cluster id
    write_plan(rv, bad)
    rep = litrev.merge_presentation_plan(rv)
    e = " | ".join(rep["errors"])
    for needle in ("CH02 starts at 35", "300", "section 10", "C002", "slideshow", "diagram visual takes one cluster id"):
        assert needle in e, needle
    assert (rv.path / "data" / "presentation" / "merged.json").read_text() == before
    folded = good_plan(ps)
    folded["chapters"][4]["cluster_ids"] = []
    folded["folded_clusters"] = [{"cluster_id": "C002", "into": "CH04", "reason": "one paper; shown beside C001"}]
    write_plan(rv, folded)
    assert litrev.merge_presentation_plan(rv)["errors"] == []


def test_merge_presentation_plan_warnings(tmp_path):
    rv, ps = pres_review(tmp_path)
    p = good_plan(ps)
    p["chapters"][0]["title"] = "Paper 4"
    p["chapters"][3]["evidence"]["supporting"].append("P999")
    p["chapters"][4]["engagement"] = {"kind": "prediction", "prompt": "a"}
    p["chapters"][5]["engagement"] = {"kind": "quiz", "prompt": "b"}
    p["chapters"][3]["deep_dive"] = {"paper_id": "P998", "reason": "x"}
    write_plan(rv, p)
    rep = litrev.merge_presentation_plan(rv)
    w = " | ".join(rep["warnings"])
    assert rep["errors"] == []
    assert "Paper 4" in w and "P999" in w and "quiz" in w and "prediction moments" in w and "P998" in w
    ch = {c["id"]: c for c in litrev.load_presentation(rv)["plan"]["chapters"]}
    assert "P999" not in ch["CH04"]["evidence"]["supporting"] and ch["CH06"]["engagement"] is None
    assert ch["CH04"]["deep_dive"] is None


# ---------- 8.2 script ----------

def test_script_input_packet(tmp_path):
    rv, ps = merged_plan(tmp_path)
    pk = litrev.script_input(rv, "CH04")
    assert pk["chapter"]["id"] == "CH04" and pk["chapter"]["duration_s"] == 75
    assert pk["previous"]["transition"] == "next" and pk["next"]["question"]
    assert [c["id"] for c in pk["outline"]] == [f"CH0{i}" for i in range(1, 8)]
    assert pk["records"]["C001"]["diagram"]["nodes"][0]["id"] == "g" and pk["records"]["R001"]["value"] == 0.05
    opts = {(o["kind"], tuple(o["refs"])): o for o in pk["visual_options"]}
    assert opts[("diagram", ("C001",))]["element_ids"] == ["g", "s", "f", "p"]
    assert "0.05" in pk["allowed_numbers"] and pk["engagement"]["kind"] == "prediction"
    assert pk["words_budget"] == int(75 * litrev.PRES_WPM / 60 * litrev.PRES_BUDGET)
    assert pk["output_path"].endswith("data/presentation/script/CH04.json")
    with pytest.raises(SystemExit, match="CH99"):
        litrev.script_input(rv, "CH99")


def test_merge_script_stores_chapter_and_lexicon(tmp_path):
    rv, ps = merged_plan(tmp_path)
    reveal = [{"at": 0, "show": ["g", "s"]}, {"at": 3, "show": ["f", "p"]}]
    write_script(rv, "CH04", [
        scene("CH04-S01", 6, visual={"kind": "diagram", "refs": ["C001"], "reveal": reveal}),
        scene("CH04-S02", 6, narration=[SENT] * 5 + ["Its error on the board is 0.05, against 0.06 for the next family."],
              visual={"kind": "kpi-chart", "refs": ["K001"], "reveal": [{"at": 5, "show": [ps[0]["id"]]}]},
              sources=[ps[0]["id"], "R001"], engagement={"kind": "prediction"}, pause_after_s=2.0)],
        prons=[{"term": "DINOv2", "say": "dino v two"}])
    rep = litrev.merge_script(rv)
    assert rep["rejected"] == {} and rep["merged"] == ["CH04"]
    pres = litrev.load_presentation(rv)
    s = pres["script"]["CH04"]
    assert len(s["scenes"]) == 2 and s["scenes"][0]["start_s"] == 90 and s["est_s"] > 60
    assert s["scenes"][1]["start_s"] == pytest.approx(90 + s["scenes"][0]["est_s"])
    assert pres["lexicon"]["DINOv2"] == "dino v two"
    r = rows8(rv)
    assert r["script"]["state"] == "todo" and "CH04" not in r["script"]["detail"]


def test_merge_script_rejects_invented_numbers(tmp_path):
    rv, ps = merged_plan(tmp_path)
    ok_round = "Rounded, its error is about 0.1 and it ranks first of 4."      # 0.07 -> 0.1; small integers pass
    invented = "It cuts the error by 37 percent, to 0.031."
    write_script(rv, "CH04", [scene("CH04-S01", 11, narration=[SENT] * 10 + [ok_round]),
                              scene("CH04-S02", 2, narration=[SENT, invented])])
    rep = litrev.merge_script(rv)
    e = " | ".join(rep["rejected"]["CH04"])
    assert "0.031" in e and "37" in e and "0.1" not in e and "CH04-S02" in e
    assert "CH04" not in (litrev.load_presentation(rv).get("script") or {})
    write_script(rv, "CH04", [scene("CH04-S01", 11, narration=[SENT] * 10 + [ok_round]),
                              scene("CH04-S02", 2, narration=[SENT, invented], claim_type="illustrative", sources=[])])
    rep = litrev.merge_script(rv)
    assert rep["rejected"] == {} and any("illustrative" in w and "0.031" in w for w in rep["warnings"])


def test_merge_script_checks_structure_timing_and_engagement(tmp_path):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH05", [
        scene("CH05-S01", 1, sources=[]),                                                    # reported, no source
        scene("CH05-S01", 1, sources=["P777"]),                                              # duplicate id, unknown source
        scene("CH05-S03", 1, visual={"kind": "diagram", "refs": ["C001"], "reveal": [{"at": 9, "show": ["zz"]}]}),
        scene("CH05-S04", 1, visual={"kind": "example", "refs": [], "reveal": []}),          # example must be illustrative
        scene("CH05-S05", 1, engagement={"kind": "prediction"})])                            # CH05 was assigned none
    rep = litrev.merge_script(rv, chapter="CH05")
    e = " | ".join(rep["rejected"]["CH05"])
    for needle in ("CH05-S01: a reported scene needs", "duplicate scene id CH05-S01", "P777", "at 9", "zz",
                   "example", "too short"):
        assert needle in e, needle
    write_script(rv, "CH05", [scene("CH05-S01", 10, visual={"kind": "kpi-chart", "refs": ["K001"], "reveal": []},
                                    engagement={"kind": "prediction"})])
    rep = litrev.merge_script(rv, chapter="CH05")
    assert rep["rejected"] == {} and any("CH05" in w and "engagement" in w for w in rep["warnings"])
    assert litrev.load_presentation(rv)["script"]["CH05"]["scenes"][0]["engagement"] is None


def test_speech_estimate_is_spoken_words_at_the_voice_rate_plus_gaps_and_pause(tmp_path):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 12)])
    assert litrev.merge_script(rv)["rejected"] == {}
    want = 12 * 13 * 60 / litrev.PRES_WPM + 11 * litrev.PRES_SENT_GAP_S + 0.5
    assert litrev.load_presentation(rv)["script"]["CH04"]["scenes"][0]["est_s"] == pytest.approx(want, abs=0.05)


def test_density_notes_flag_sentences_that_carry_too_much():
    assert litrev._pres_density(SENT) is None
    assert litrev._pres_density("CROSS has the lowest error of all 14 methods.") is None
    assert litrev._pres_density("On TREC DL 2019 and DL 2020, it ranks 3 of 5.") is None    # years, small counts
    stacked = ("MoGe-3 leads 3 boards, Modality Forcing leads NYUv2, and PointDiT, trained only on synthetic images, "
               "ranks 2 to 4 on all 4.")
    assert "4 clause mark(s)" in litrev._pres_density(stacked)
    assert "3 result number(s)" in litrev._pres_density("It scores 0.486 on DL 2019, 0.5 on DL 2020, and 0.607 overall.")
    assert litrev._pres_density(" ".join(["word"] * 19) + ".").startswith("19 words")
    assert litrev.PRES_NAME.findall("On TREC DL 2019 it reaches 0.486.") == ["TREC DL 2019"]


def test_density_notes_are_listed_but_never_reject(tmp_path):
    rv, ps = merged_plan(tmp_path)
    dense = "This family, trained on 4 datasets, reuses a pretrained generator, a frozen encoder and a small head."
    write_script(rv, "CH04", [scene("CH04-S01", 12, narration=[SENT] * 11 + [dense])])
    rep = litrev.check_script(rv, "CH04")
    assert rep["ok"] and len(rep["density"]) == 1 and rep["density"][0].startswith("CH04-S01 sentence 11: 17 words")
    m = litrev.merge_script(rv)
    assert m["merged"] == ["CH04"] and m["density"] == rep["density"]


def test_check_script_warns_on_a_wrong_count_of_ranked_papers(tmp_path):
    # retrieval-confidence CH12 said "7 of the 13 ranked papers", copied from an old takeaway; 12 papers were ranked
    rv, ps = merged_plan(tmp_path)
    n = litrev.synthesis_counts(rv)["ranked"]
    say = [SENT] * 11 + [f"Code was found for 1 of the {n + 1} ranked papers."]
    write_script(rv, "CH04", [scene("CH04-S01", 12, narration=say, claim_type="illustrative", sources=[])])
    rep = litrev.check_script(rv, "CH04")
    assert any(w.startswith("CH04-S01 sentence 11:") and f"the {n} ranked papers" in w for w in rep["warnings"])
    say[-1] = f"Code was found for 1 of the {n} ranked papers."
    write_script(rv, "CH04", [scene("CH04-S01", 12, narration=say, claim_type="illustrative", sources=[])])
    assert not any("ranked papers" in w for w in litrev.check_script(rv, "CH04")["warnings"])


def test_matrix_scene_with_three_on_screen_lines_warns(tmp_path):
    rv, ps = merged_plan(tmp_path)
    mx = {"kind": "matrix", "refs": [], "reveal": []}
    write_script(rv, "CH06", [scene("CH06-S01", 5, visual=mx, claim_type="synthesis", on_screen="Line one\nLine two"),
                              scene("CH06-S02", 5, visual=mx, claim_type="synthesis", on_screen="One\nTwo\nThree")])
    rep = litrev.check_script(rv, "CH06")
    hits = [w for w in rep["warnings"] if "matrix shows on_screen lines" in w]
    assert len(hits) == 1 and hits[0].startswith("CH06-S02")


def test_plan_remerge_marks_scripts_stale_and_drops_removed_chapters(tmp_path):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 12)])
    assert litrev.merge_script(rv)["rejected"] == {}
    rv.meta["step_log"]["merge-synthesis"] = "2999-01-01T00:00:00Z"
    assert rows8(rv)["plan"]["state"] == "stale"
    p = good_plan(ps)
    p["chapters"][2]["end_s"] = 165
    p["chapters"][3]["start_s"] = 165; p["chapters"][3]["end_s"] = 225
    p["chapters"][4]["start_s"] = 225; p["chapters"][4]["end_s"] = 300
    p["chapters"][4]["sections"] = [6, 7, 8, 9, 10]
    p["chapters"][4]["cluster_ids"] = ["C001", "C002"]
    p["chapters"][4]["summaries"] = ["dimensions", "provenance", "conditions", "feasibility", "gaps", "roadmap"]
    del p["chapters"][5:]
    p["chapters"][3]["cluster_ids"] = []
    p["chapters"][3]["id"], p["chapters"][4]["id"] = "CH09", "CH05"
    write_plan(rv, p)
    rep = litrev.merge_presentation_plan(rv)
    assert rep["errors"] == [] and rep["script_dropped"] == ["CH04"]
    assert "CH04" not in litrev.load_presentation(rv).get("script", {})


# ---------- render + CLI ----------

def test_render_writes_plan_script_and_hub_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_PIPELINE_URL", "")
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 12)], prons=[{"term": "DUSt3R", "say": "duster"}])
    litrev.merge_script(rv)
    litrev.render_review(rv)
    out = rv.path / "rendered" / "presentation"
    plan_md, script_md = (out / "PLAN.md").read_text(), (out / "SCRIPT.md").read_text()
    assert "| 01:30–02:45 |" in plan_md and "Why chapter 4 matters" in plan_md and "methods" in plan_md
    assert SENT in script_md and "DUSt3R" in script_md and "not written yet" in script_md
    hub = (rv.path / "rendered" / "hub.html").read_text()
    assert hub.isascii() and "presentationPage" in hub and '"presentation":' in hub


def test_cli_presentation_commands(tmp_path, capsys):
    rv, ps = pres_review(tmp_path)
    rv.save()
    path = str(rv.path)
    assert litrev.main(["presentation", "--review", path, "--minutes", "10", "--audience", "engineers"]) == 0
    assert litrev.Review.load(rv.path).meta["presentation"]["minutes"] == 10
    capsys.readouterr()
    assert litrev.main(["presentation-input", "--review", path]) == 0
    assert json.loads(capsys.readouterr().out)["target"]["seconds"] == 600
    bad = good_plan(ps)                                 # a 300 s plan against the 600 s target: refused, exit 1
    write_plan(rv, bad)
    assert litrev.main(["merge-presentation-plan", "--review", path]) == 1
    assert "merge-presentation-plan" not in litrev.Review.load(rv.path).meta["step_log"]


# ---------- 8.3 audio (the voice worker is faked: tests never need the TTS venv) ----------

def test_spoken_applies_the_lexicon_longest_term_first():
    lex = {"TREC": "trek", "TREC DL": "trek D L", "DUSt3R": "duster"}
    assert litrev._spoken("On TREC DL 2019 and TREC 2020, DUSt3R wins; TRECx stays.", lex) == \
        "On trek D L 2019 and trek 2020, duster wins; TRECx stays."


def test_asr_compare_normalises_numbers_and_ignores_a_stray_you():
    ok = litrev._asr_compare("Kendall's tau is 0.428, against ten, for P D Q P P.", "Kendall's tau is 0.428 against 10 for PDQPP. you")
    assert ok == {"wer": 0.0, "missing": [], "extra": [], "end_heard": True, "char_sim": 1.0, "rule": litrev.AUDIO_CHECK_RULE}
    cut = litrev._asr_compare("The board has five papers on it.", "The board has five")
    assert cut["end_heard"] is False and cut["missing"] == ["papers", "on", "it"] and cut["wer"] > 0.4 and cut["char_sim"] < 0.9
    for said, heard in (("starts within 20 m.", "starts within 20M."), ("at 2.51 metres.", "at 2.51 meters."),
                        ("to a real dashcam?", "to a real dash cam?")):
        assert litrev._asr_compare(said, heard)["end_heard"], said              # spelling, not a cut-off end
    spelled = litrev._asr_compare("It tops hot pot Q A with kwen three.", "It tops HotpotQA with Qwen3.")
    assert spelled["wer"] > 0.3 and spelled["char_sim"] > 0.9                # a spelling difference, not a skipped word


def fake_worker(calls, heard_ok=True):
    import wave

    def run(mode, job, work):
        calls.append((mode, len(job["items"])))
        if mode == "tts":
            out = []
            for it in job["items"]:
                n = len(it["text"].split())
                with wave.open(it["out"], "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(litrev.VOICE_SR)
                    w.writeframes(b"\x01\x00" * int(0.4 * n * litrev.VOICE_SR))
                out.append({"id": it["id"], "out": it["out"], "dur": 0.4 * n, "trimmed_s": 0.0, "chunks": 1,
                            "tokens": [{"text": t, "start": 0.4 * i, "end": 0.4 * i + 0.35} for i, t in enumerate(it["text"].split())]})
            return {"engine": {"name": "fake"}, "items": out}
        return {"engine": {}, "items": [{"id": it["id"], "text": heard_ok[it["id"]] if heard_ok else "", "words": [] if not heard_ok else
                                         [{"word": w, "start": 0.4 * i, "end": 0.4 * i + 0.3} for i, w in enumerate(heard_ok[it["id"]].split())]}
                                        for it in job["items"]]}
    return run


def test_audio_build_measures_scenes_flags_and_caches(tmp_path, monkeypatch):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 6), scene("CH04-S02", 6, pause_after_s=1.0)], prons=[{"term": "DINOv2", "say": "dino v two"}])
    litrev.merge_script(rv)
    calls = []
    monkeypatch.setattr(litrev, "_run_worker", fake_worker(calls, heard_ok={"CH04-S01": " ".join([SENT] * 6), "CH04-S02": "This family"}))
    rep = litrev.audio_build(rv)
    a = litrev.load_audio(rv)
    s1, s2 = a["scenes"]["CH04-S01"], a["scenes"]["CH04-S02"]
    assert calls == [("tts", 1), ("asr", 2)]                         # 12 sentences, one distinct text: spoken once
    assert s1["sentence_starts"][1] == pytest.approx(len(SENT.split()) * 0.4 + litrev.AUDIO_SENT_GAP_S)
    assert s2["dur"] == pytest.approx(6 * len(SENT.split()) * 0.4 + 5 * litrev.AUDIO_SENT_GAP_S + 1.0, abs=0.01)
    assert (rv.path / s1["wav"]).exists() and s1["words"][0]["sentence"] == 0
    assert s1["asr"]["wer"] == 0.0 and s2["asr"]["end_heard"] is False
    assert [f["scene"] for f in rep["flags"]] == ["CH04-S02"]
    assert a["chapters"]["CH04"]["planned"] == 75 and "CH04" in rep["chapters_off_plan"]
    r = rows8(rv)
    assert r["audio"]["state"] == "warn"
    calls.clear()
    litrev.audio_build(rv)
    assert calls == []                                                # nothing changed: no speech, no recognition
    pres = litrev.load_presentation(rv)
    pres["lexicon"]["family"] = "fam ily"
    litrev._save_presentation(rv, pres)
    assert rows8(rv)["audio"]["state"] == "stale"                     # a lexicon change re-speaks the scenes
    pres["lexicon"].pop("family")
    litrev._save_presentation(rv, pres)
    litrev.presentation_set(rv, minutes=5)
    litrev.render_review(rv)
    md = (rv.path / "rendered" / "presentation" / "SCRIPT.md").read_text()
    assert "Recorded narration: 01:07 for a 05:00 plan" in md and "recorded 01:07, -11%" in md
    assert '"start_s":{"CH04-S01":0' in (rv.path / "rendered" / "hub.html").read_text().replace(" ", "")


def test_audio_sample_speaks_three_sentences(tmp_path, monkeypatch):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 14, narration=[SENT] * 12 + ["DINOv2 sees the scene.", "Its error is 0.05 on the board."]),
                              scene("CH04-S02", 1, narration=["So what comes next?"])], prons=[{"term": "DINOv2", "say": "dino v two"}])
    assert litrev.merge_script(rv)["rejected"] == {}
    calls = []
    monkeypatch.setattr(litrev, "_run_worker", fake_worker(calls, heard_ok={"sample": "dino v two sees the scene its error is 0.05 on the board so what comes next"}))
    rep = litrev.audio_build(rv, sample=True)
    assert rep["sentences"] == ["DINOv2 sees the scene.", "Its error is 0.05 on the board.", "So what comes next?"]
    assert rep["spoken"][0] == "dino v two sees the scene." and rep["check"]["wer"] == 0.0
    assert (rv.path / "rendered" / "presentation" / "sample.wav").exists() and not (rv.path / "data" / "presentation" / "audio.json").exists()


def test_check_script_reports_without_storing(tmp_path):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 2, narration=[SENT, "It cuts the error to 0.031."])])
    rep = litrev.check_script(rv, "CH04")
    assert rep["ok"] is False and any("0.031" in e for e in rep["errors"]) and any("too short" in e for e in rep["errors"])
    assert "script" not in litrev.load_presentation(rv) or "CH04" not in litrev.load_presentation(rv)["script"]


def test_check_script_warns_on_a_kpi_value_said_on_another_scale(tmp_path):
    rv, ps = merged_plan(tmp_path)
    for r in rv.tables["results"].values():     # one percent row: the board is drawn in percent, so 0.05 is "5%"
        if r["paper_id"] == ps[1]["id"]:
            r["value"], r["units"] = 7.0, "%"
    rv.save()
    write_script(rv, "CH04", [scene("CH04-S01", 2, sources=[ps[0]["id"], "K001"],
                                    narration=[SENT, "It ranks first at 0.05.", "And 0.05 versus 5 percent? One value."])])
    rep = litrev.check_script(rv, "CH04")
    scale = [w for w in rep["warnings"] if "another scale" in w]
    assert len(scale) == 1 and "sentence 1" in scale[0] and "say 5%" in scale[0]


def test_app_session_refuses_voice_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("LITREV_APP_REVIEW", str(tmp_path))
    with pytest.raises(SystemExit, match="voice-setup"):
        litrev.main(["voice-setup"])


# ---------- 8.4 video (data for the scene page; the render itself is in test_video.py) ----------

def test_lead_keeps_whole_sentences():
    assert litrev._lead("First one. Second one is longer. Third.", 30) == "First one."
    assert litrev._lead("First one. Second one is longer. Third.", 34) == "First one. Second one is longer."
    assert litrev._lead("Short. Next.", 6) == "Short."
    assert litrev._lead("One very long first sentence without any stop", 20).endswith("...")


def test_video_data_builds_every_scene_from_review_records(tmp_path, monkeypatch):
    rv, ps = merged_plan(tmp_path)
    reveal = [{"at": 0, "show": ["g"]}, {"at": 2, "show": ["s", "f", "p"]}]
    write_script(rv, "CH04", [scene("CH04-S01", 6, visual={"kind": "diagram", "refs": ["C001"], "reveal": reveal}),
                              scene("CH04-S02", 6, visual={"kind": "kpi-chart", "refs": ["K001"], "reveal": [{"at": 1, "show": [ps[0]["id"]]}]},
                                    sources=[ps[0]["id"], "R001", "K001"], transition="fade")])
    assert litrev.merge_script(rv)["rejected"] == {}
    pres = litrev.load_presentation(rv)                      # the video needs every chapter: give the rest one scene each
    for c in pres["plan"]["chapters"]:
        pres["script"].setdefault(c["id"], {"scenes": [dict(scene(f"{c['id']}-S01", 1), visual={"kind": "chapter", "refs": [], "reveal": []},
                                                            start_s=c["start_s"], est_s=5)], "words": 13, "est_s": 5})
    litrev._save_presentation(rv, pres)
    with pytest.raises(SystemExit, match="audio"):
        litrev.video_data(rv)
    monkeypatch.setattr(litrev, "_run_worker", fake_worker([], heard_ok=None))
    litrev.audio_build(rv, asr=False)
    for r in rv.tables["results"].values():     # one row of four in percent: MDE showed a fraction row "0.044" beside "3.42"
        if r["paper_id"] == ps[1]["id"]:
            r["value"], r["units"] = 7.0, "%"
    data, tl = litrev.video_data(rv)
    assert len(data["scenes"]) == len(tl) == 8 and data["scenes"][3]["id"] == "CH04-S01"
    d = data["scenes"][3]
    assert d["kind"] == "diagram" and d["states"][1] == {"shown": ["g", "s", "f", "p"], "latest": ["s", "f", "p"]}
    assert data["obj"]["C001"]["diagram"]["nodes"][0]["id"] == "g"
    k = data["obj"]["K001"]
    assert [r["paper_id"] for r in k["rows"]] == [ps[0]["id"], ps[2]["id"], ps[1]["id"], ps[3]["id"]]
    assert [r["value"] for r in k["rows"]] == [5.0, 6.0, 7.0, 8.0] and k["units"] == "%"      # one scale per board
    assert k["rows"][0]["label"] == "Paper 1" and k["direction"] == "lower is better"
    ctx = litrev._PresCtx(rv)                    # the writers get the same scale, so the narration says what the chart shows
    assert [e["shown"] for e in litrev._pres_record(rv, "K001", ctx)["board"]] == ["5%", "6%", "7%", "8%"]
    assert litrev._pres_record(rv, ps[0]["id"], ctx)["per_kpi"]["K001"]["shown"] == "5%"
    r0 = next(r["id"] for r in rv.tables["results"].values() if r["paper_id"] == ps[0]["id"])
    assert litrev._pres_record(rv, r0, ctx)["shown"] == "5%" and {"5", "6", "7", "8"} <= litrev._pres_numbers(litrev._pres_record(rv, "K001", ctx), set())
    # a reference a viewer recognises, each once: no record id, no 'board', no 'and N more'
    assert data["scenes"][4]["sources"] == "Paper 1 2024 · NYUv2 · comparison: NYUv2 · AbsRel"
    assert tl[4]["transition"] == "fade" and tl[3]["sentence_starts"][1] > 0 and Path(tl[3]["wav"]).exists()
    assert data["flow"][-1]["stage"] == "included" and data["matrix"]["rows"][0]["id"] == "L1"
    assert rows8(rv)["video"]["state"] == "todo"


from pathlib import Path  # noqa: E402


def test_a_review_given_by_a_relative_path_has_an_absolute_path(tmp_path, monkeypatch):
    make_review(tmp_path)              # `video --review .`: the voice worker runs in another folder and got a relative job path
    monkeypatch.chdir(tmp_path)
    assert litrev.Review.load("rv").path == (tmp_path / "rv").resolve()


def test_presentation_defaults_to_20_minutes(tmp_path):
    rv, ps = pres_review(tmp_path)
    rv.meta.pop("presentation")
    rv.save()
    assert litrev.main(["presentation", "--review", str(rv.path)]) == 0
    rv = litrev.Review.load(rv.path)
    assert rv.meta["presentation"]["minutes"] == litrev.PRES_DEFAULT_MINUTES == 20      # the user, 2026-10-28
    assert litrev.presentation_input(rv)["target"]["seconds"] == 1200


def test_skill_md_has_no_dollar_digit():
    """Claude Code replaces $0, $1, ... in a loaded SKILL.md with the skill's arguments: "$6" became a review path."""
    import re as _re
    text = (Path(__file__).parent.parent / "SKILL.md").read_text()
    assert not _re.findall(r"\$\d", text)


def test_a_settled_lexicon_wins_over_parallel_writers(tmp_path):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH04", [scene("CH04-S01", 12)], prons=[{"term": "FG2", "say": "F G two"}])
    write_script(rv, "CH05", [scene("CH05-S01", 10)], prons=[{"term": "FG2", "say": "F G squared"}])
    rep = litrev.merge_script(rv)
    assert any("settle it" in w for w in rep["warnings"])
    (rv.path / "data" / "presentation" / "lexicon.json").write_text(json.dumps({"FG2": "F G squared", "Loc2": "lock squared"}))
    rep = litrev.merge_script(rv)
    lex = litrev.load_presentation(rv)["lexicon"]
    assert lex["FG2"] == "F G squared" and lex["Loc2"] == "lock squared" and rep["lexicon_fixed"] == {"Loc2": "lock squared"}
    assert litrev.script_input(rv, "CH06")["lexicon"]["FG2"] == "F G squared"


# ---------- what the papers are tested on: tasks, benchmarks, metrics ----------

def summary_review(tmp_path):
    """pres_review + 2 tasks, a second test set and metric that 2 papers report, and a re-reported baseline row on it
    that must not count its paper."""
    rv, ps = pres_review(tmp_path)
    t1 = rv.add_task("Monocular depth estimation",
                     definition={"value": "Predict depth per pixel from one image (e.g. NYUv2), such as an indoor photo.", "status": "author-reported"})
    t2 = rv.add_task("Surface normal estimation", definition="Predict the surface normal of each pixel.")
    for p in ps:
        p["task_ids"] = [t1["id"]]
    ps[0]["task_ids"] = [t1["id"], t2["id"]]
    d2, m2 = rv.add_dataset("ETH3D"), rv.add_metric("delta1", higher_is_better=True)
    for p in ps[:2]:
        rv.add_result(paper_id=p["id"], dataset_id=d2["id"], metric_id=m2["id"], value=0.9, status="author-reported")
    rv.add_result(paper_id=ps[2]["id"], dataset_id=d2["id"], metric_id=m2["id"], value=0.8, status="author-reported",
                  caveats=f"baseline re-reported by {ps[2]['id']}")
    nyu = next(d["id"] for d in rv.tables["datasets"].values() if d["name"] == "NYUv2")
    absrel = next(m["id"] for m in rv.tables["metrics"].values() if m["name"] == "AbsRel")
    return rv, ps, {"t1": t1["id"], "t2": t2["id"], "nyu": nyu, "eth": d2["id"], "absrel": absrel, "d1": m2["id"]}


def test_summaries_count_included_papers_and_tag_the_kpis(tmp_path):
    rv, ps, i = summary_review(tmp_path)
    sm = litrev._pres_summaries(rv)
    t = sm["tasks"]["rows"]
    assert [(r["id"], r["papers"]) for r in t] == [(i["t1"], 4), (i["t2"], 1)]
    assert t[0]["definition"] == "Predict depth per pixel from one image."      # no example, no bracket
    assert t[0]["test_sets"] == ["NYUv2", "ETH3D"] and t[1]["test_sets"] == ["NYUv2", "ETH3D"]
    b = sm["benchmarks"]["rows"]                 # the baseline row of paper 3 does not count it for ETH3D
    assert [(r["id"], r["papers"], r["kpi"]) for r in b] == [(i["nyu"], 4, True), (i["eth"], 2, False)]
    assert b[0]["task"] == "Monocular depth estimation" and b[0]["metric"] == "AbsRel"     # task from its papers
    m = sm["metrics"]["rows"]
    assert [(r["id"], r["papers"], r["kpi"], r["direction"]) for r in m] == [
        (i["absrel"], 4, True, "lower is better"), (i["d1"], 2, False, "higher is better")]
    assert sm["benchmarks"]["note"].startswith("2 of 2 test sets") and sm["tasks"]["papers"] == 4


def test_summaries_keep_the_kpi_rows_and_two_more_per_task():
    # llm-to-domain-slm v1: six of the top eight test sets were zero-shot commonsense sets
    rows = [{"id": f"D{n:03d}", "papers": 50 - n, "task": "commonsense", "kpi": False} for n in range(1, 8)]
    rows += [{"id": "D100", "papers": 9, "task": "code", "kpi": False}, {"id": "D101", "papers": 3, "task": "math", "kpi": False},
             {"id": "D102", "papers": 2, "task": "law", "kpi": True}, {"id": "D103", "papers": 1, "task": "biology", "kpi": False}]
    rows.sort(key=lambda r: (-r["papers"], r["id"]))
    ids = [r["id"] for r in litrev._pres_pick(rows, lambda r: r["task"])]
    # the KPI row always; two commonsense sets, code and math first; then the most used; a 1-paper set waits
    assert ids == ["D001", "D002", "D003", "D004", "D005", "D100", "D101", "D102"]
    assert [r["id"] for r in litrev._pres_pick(rows)] == ["D001", "D002", "D003", "D004", "D005", "D006", "D007", "D102"]



def tasks_on_ch03(plan):
    """summary_review has tasks: CH03 takes the tasks scene too, with 10 s more for it."""
    plan["chapters"][2]["end_s"], plan["chapters"][3]["start_s"] = 100, 100
    plan["chapters"][2]["summaries"] = ["tasks", "benchmarks", "metrics", "unmeasured"]
    return plan


def test_the_plan_must_show_every_data_scene_and_give_each_time(tmp_path):
    rv, ps, i = summary_review(tmp_path)
    plan = good_plan(ps)
    plan["chapters"][2]["summaries"] = []
    write_plan(rv, plan)
    e = " | ".join(litrev.merge_presentation_plan(rv)["errors"])
    assert all(f"no chapter shows the {k} data scene" in e for k in ("tasks", "benchmarks", "metrics", "unmeasured"))
    plan["chapters"][2]["summaries"] = ["tasks", "slides"]
    write_plan(rv, plan)
    assert "summaries must list data scenes" in " | ".join(litrev.merge_presentation_plan(rv)["errors"])
    plan["chapters"][2]["summaries"] = ["tasks", "benchmarks", "metrics", "unmeasured"]       # 30 s for four scenes
    write_plan(rv, plan)
    assert "too short for 4 data scene(s)" in " | ".join(litrev.merge_presentation_plan(rv)["errors"])
    plan = tasks_on_ch03(good_plan(ps))
    plan["chapters"][2]["summaries"].append("metrics")
    write_plan(rv, plan)
    assert litrev.merge_presentation_plan(rv)["errors"] == []
    ch = {c["id"]: c for c in litrev.load_presentation(rv)["plan"]["chapters"]}
    assert ch["CH03"]["summaries"] == ["tasks", "benchmarks", "metrics", "unmeasured"] and "functions" not in ch["CH03"]
    assert ch["CH03"]["sections"] == [4, 5] and rows8(rv)["plan"]["state"] == "done"
    pres = litrev.load_presentation(rv)                  # a plan merged before v1.34.0: eight functions, schema 1
    pres["plan"]["schema"] = 1
    for c in pres["plan"]["chapters"]:
        c["functions"] = [5]
        c.pop("sections")
    litrev._save_presentation(rv, pres)
    r = rows8(rv)["plan"]
    assert r["state"] == "stale" and "8 chapter functions" in r["detail"] and "merge-presentation-plan" in r["next"]
    assert litrev._card_sections(pres["plan"]["chapters"][0]) == [6, 7]          # old plans still map onto the sections
    old = good_plan(ps)
    old["schema"] = 1
    write_plan(rv, old)
    rep = litrev.merge_presentation_plan(rv)
    assert "schema 1" in rep["errors"][0] and len(rep["errors"]) == 1


def test_the_plan_needs_every_section_and_dimensions_in_each_family_chapter(tmp_path):
    rv, ps = pres_review(tmp_path)
    plan = good_plan(ps)
    plan["chapters"][3]["sections"] = [6, 7]          # CH04 is about C001 now: it must show the best paper's dimensions
    plan["chapters"][6]["sections"] = [10]            # and section 6 then sits on CH04 only
    plan["chapters"][5]["summaries"].remove("gaps")
    write_plan(rv, plan)
    rep = litrev.merge_presentation_plan(rv)
    e, w = " | ".join(rep["errors"]), " | ".join(rep["warnings"])
    assert "CH04: a section-6 chapter about a family lists \"dimensions\"" in e and "gaps data scene of section 9" in e
    assert "dimensions data scene is on a chapter without section 6" in w
    plan = good_plan(ps)
    plan["chapters"][0]["sections"] = [2]
    plan["chapters"][-1]["sections"] = [6]
    plan["chapters"][1]["sections"] = [1, 2, 3]
    plan["chapters"][5]["sections"] = [7, 8, 9, 10]
    write_plan(rv, plan)
    rep = litrev.merge_presentation_plan(rv)
    w = " | ".join(rep["warnings"])
    assert rep["errors"] == [] and "should open the video" in w and "should close the video" in w


def test_a_summary_chapter_needs_a_scene_of_each_kind(tmp_path):
    rv, ps, i = summary_review(tmp_path)
    write_plan(rv, tasks_on_ch03(good_plan(ps)))
    assert litrev.merge_presentation_plan(rv)["errors"] == []
    pk = litrev.script_input(rv, "CH03")
    opts = {o["kind"]: o["element_ids"] for o in pk["visual_options"]}
    assert opts["tasks"] == [i["t1"], i["t2"]] and opts["benchmarks"] == [i["nyu"], i["eth"]] and opts["metrics"] == [i["absrel"], i["d1"]]
    assert opts["unmeasured"] == ["U1"] and pk["review"]["report"]["unmeasured"][0]["id"] == "U1"
    assert [s["n"] for s in pk["review"]["sections"]] == [4, 5]
    assert pk["chapter"]["summaries"] == ["tasks", "benchmarks", "metrics", "unmeasured"]
    assert pk["review"]["summaries"]["benchmarks"]["rows"][0]["papers"] == 4
    assert litrev.presentation_input(rv)["summaries"]["tasks"]["rows"][0]["id"] == i["t1"]
    tasks = scene("CH03-S01", 2, visual={"kind": "tasks", "refs": [], "reveal": [{"at": 0, "show": [i["t1"]]}]}, sources=[i["t1"]])
    bench = scene("CH03-S02", 2, visual={"kind": "benchmarks", "refs": [], "reveal": [{"at": 0, "show": [i["nyu"]]}, {"at": 1, "show": [i["eth"]]}]},
                  sources=[i["nyu"], i["eth"]])
    write_script(rv, "CH03", [tasks, bench])
    e = " | ".join(litrev.check_script(rv, "CH03")["errors"])
    assert "the plan gives this chapter the metrics data scene" in e and "benchmarks data scene" not in e
    assert "the plan gives this chapter the unmeasured data scene" in e and "(visual_options)" in e
    bad = scene("CH03-S03", 2, visual={"kind": "metrics", "refs": [], "reveal": [{"at": 0, "show": [i["t1"]]}]}, sources=[i["absrel"]])
    write_script(rv, "CH03", [tasks, bench, bad])
    assert "which the metrics visual does not have" in " | ".join(litrev.check_script(rv, "CH03")["errors"])
    good = scene("CH03-S03", 2, visual={"kind": "metrics", "refs": [], "reveal": [{"at": 1, "show": [i["absrel"]]}]}, sources=[i["absrel"]])
    unm = scene("CH03-S04", 2, visual={"kind": "unmeasured", "refs": [], "reveal": [{"at": 0, "show": ["U1"]}]},
                claim_type="synthesis", sources=["U1", "M001"])
    write_script(rv, "CH03", [tasks, bench, good, unm])
    rep = litrev.merge_script(rv, "CH03")
    assert rep["rejected"] == {}, rep


def test_video_data_draws_a_summary_table_whole_and_highlights_the_named_rows(tmp_path, monkeypatch):
    rv, ps, i = summary_review(tmp_path)
    write_plan(rv, tasks_on_ch03(good_plan(ps)))
    assert litrev.merge_presentation_plan(rv)["errors"] == []
    pres = litrev.load_presentation(rv)
    for c in pres["plan"]["chapters"]:
        pres.setdefault("script", {})[c["id"]] = {"scenes": [dict(scene(f"{c['id']}-S01", 2), visual={"kind": "chapter", "refs": [], "reveal": []},
                                                                  start_s=c["start_s"], est_s=5)], "words": 26, "est_s": 5}
    pres["script"]["CH03"]["scenes"][0]["visual"] = {"kind": "tasks", "refs": [], "reveal": [{"at": 1, "show": [i["t2"]]}]}
    litrev._save_presentation(rv, pres)
    monkeypatch.setattr(litrev, "_run_worker", fake_worker([], heard_ok=None))
    litrev.audio_build(rv, asr=False)
    data, tl = litrev.video_data(rv)
    d = next(s for s in data["scenes"] if s["id"] == "CH03-S01")
    assert d["kind"] == "tasks" and d["states"] == [{"shown": [i["t1"], i["t2"]], "latest": []},
                                                    {"shown": [i["t1"], i["t2"]], "latest": [i["t2"]]}]
    assert [r["id"] for r in data["summaries"]["metrics"]["rows"]] == [i["absrel"], i["d1"]]


# ---------- the review report (step 7): the ten sections in the hub, and what the video draws ----------

def test_report_input_and_merge_store_the_sections_judgement(tmp_path):
    rv, ps = pres_review(tmp_path, report=False)
    st = {r["key"]: r for r in litrev.review_status(rv)["rows"] if r["step"] == 7}
    assert st["report"]["state"] == "todo" and "report-input" in st["report"]["next"]
    assert any("review report missing" in w for w in litrev.validate_review(rv)[1])
    pk = litrev.report_input(rv)
    assert pk["output_path"].endswith("data/report/report.json") and pk["review"]["purpose"] == "test purpose"
    assert pk["search"]["included"] == 4 and pk["provenance"][0] == {"id": "K001", "label": "NYUv2 · AbsRel", "papers": 4,
                                                                     "reported": 4, "verified": 0, "reproduced": 0}
    assert [r["id"] for r in pk["feasibility"]] == [ps[0]["id"], ps[2]["id"]] and pk["feasibility"][0]["readiness"] == "insufficient information"
    assert pk["clusters"][0]["winner_dimensions"]["data"].startswith("Synthetic") and len(pk["included"]) == 4
    assert set(pk["gap_types"]) == set(litrev.REPORT_GAP_TYPES) and pk["weak_points"]["kinds"] == ["cost", "evaluation"]
    write_report(rv, report_doc(ps))
    rep = litrev.merge_report(rv)
    assert rep["errors"] == [] and rep["stored"]["gaps"] == 3
    r = rv.meta["report"]
    assert [x["id"] for x in r["gaps"]] == ["G1", "G2", "G3"] and [x["id"] for x in r["conditions"]] == ["CN1", "CN2"]
    assert [x["basis"] for x in r["gaps"]] == ["reviewed-papers", "reviewed-papers", "outside-knowledge"]
    assert r["roadmap"]["experiments"][1]["id"] == "X2" and r["roadmap"]["baseline"]["paper_id"] == ps[2]["id"]
    assert r["requirements"]["research_questions"] == ["Which family is more accurate indoors?"]
    assert litrev._pres_ref_ok(rv, "X2") and litrev._pres_ref_ok(rv, "CN1") and not litrev._pres_ref_ok(rv, "G9")
    # the roadmap's papers come first among the feasibility rows' sources; the baseline is a winner already
    assert [x["id"] for x in litrev._pres_feasibility(rv, litrev._PresCtx(rv))] == [ps[0]["id"], ps[2]["id"]]
    rv.meta.setdefault("step_log", {})["merge-report"] = r["merged_at"]
    st = {r["key"]: r for r in litrev.review_status(rv)["rows"] if r["step"] == 7}
    assert st["report"]["state"] == "done" and not any("report" in w for w in litrev.validate_review(rv)[1])
    rv.meta["step_log"]["merge-synthesis"] = "2999-01-01T00:00:00Z"
    st = {r["key"]: r for r in litrev.review_status(rv)["rows"] if r["step"] == 7}
    assert st["report"]["state"] == "stale" and "merge-synthesis ran after it" in st["report"]["detail"]
    rv.tables["papers"][ps[2]["id"]]["status"] = "excluded"
    assert any("no longer included" in w for w in litrev.report_warnings(rv))


def test_merge_report_refuses_missing_parts_bad_refs_and_invented_numbers(tmp_path):
    rv, ps = pres_review(tmp_path, report=False)
    doc = report_doc(ps)
    doc["executive_summary"].pop("uncertainty")
    doc["requirements"]["constraints"] = ""
    doc["unmeasured"] = [{"text": "No test set measures runtime.", "refs": ["M999"]}]          # its only ref is gone
    doc["gaps"][0]["type"] = "missing-data"
    doc["gaps"][1]["text"] = "No work exists on outdoor scenes."
    doc["roadmap"]["baseline"] = {"paper_id": "P999", "reason": "x"}
    doc["roadmap"]["experiments"][0]["needs"] = "About 250 labelled photos."
    write_report(rv, doc)
    rep = litrev.merge_report(rv)
    e, w = " | ".join(rep["errors"]), " | ".join(rep["warnings"])
    for needle in ("executive_summary.uncertainty", "requirements.constraints", "unmeasured[0]: cite the records",
                   "missing-data", "roadmap.baseline", "250"):
        assert needle in e, needle
    assert "M999" in w and "claims more than a selective review can see" in w
    assert "report" not in rv.meta


def test_hub_report_view_and_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_PIPELINE_URL", "")
    rv, ps = pres_review(tmp_path)
    v = litrev.hub_report(rv)
    assert sorted(v["sections"], key=int) == [str(n) for n in range(1, 11)] and v["sections"]["8"][0].startswith("Practical feasibility")
    assert [f["id"] for f in v["families"]] == ["C001", "C002"] and v["families"][0]["dims"]["loss"] == "A scale-invariant depth loss."
    assert v["provenance"][0]["reported"] == 4 and v["feasibility"][0]["id"] == ps[0]["id"] and v["labels"]["gaps"]["evidence-gap"]
    litrev.render_review(rv)
    hub = (rv.path / "rendered" / "hub.html").read_text()
    assert hub.isascii() and '"report_view":null' in hub        # the tab is off for now (HUB_REPORT_TAB); the code stays
    monkeypatch.setattr(litrev, "HUB_REPORT_TAB", True)
    litrev.render_review(rv)
    hub = (rv.path / "rendered" / "hub.html").read_text()
    assert hub.isascii() and "reportPage" in hub and '"report_view":{' in hub and '"report":' in hub
    assert '"verdict":[{"id":"VD1"' in hub and "Which approach is better, for which goal, and why" in hub


def test_dimensions_visual_and_the_new_scenes_check_their_ids(tmp_path):
    rv, ps = merged_plan(tmp_path)
    ctx = litrev._PresCtx(rv)
    errs, els = litrev._visual_check(rv, {"kind": "dimensions", "refs": [ps[0]["id"], ps[1]["id"]]}, ctx, "x")
    assert ps[1]["id"] in " ".join(errs) and "fewer than 2 method notes" in " ".join(errs)       # the sibling has one note
    errs, els = litrev._visual_check(rv, {"kind": "dimensions", "refs": [ps[0]["id"]]}, ctx, "x")
    assert errs == [] and els == ["data", "topology", "loss"]
    assert litrev._visual_check(rv, {"kind": "roadmap", "refs": []}, ctx, "x")[1] == ["baseline", "shortlist", "X1", "X2"]
    assert litrev._visual_check(rv, {"kind": "gaps", "refs": []}, ctx, "x")[1] == ["G1", "G2", "G3"]
    assert litrev._visual_check(rv, {"kind": "executive-summary", "refs": []}, ctx, "x")[1][0] == "problem"
    assert litrev._visual_check(rv, {"kind": "search", "refs": []}, ctx, "x")[1] == ["sources", "terms", "walks", "criteria", "gaps"]
    p = good_plan(ps)                                       # a family chapter offers its best paper's dimensions, alone and beside a sibling
    p["chapters"][3]["sections"], p["chapters"][3]["summaries"] = [6, 7], ["dimensions"]
    write_plan(rv, p)
    assert litrev.merge_presentation_plan(rv)["errors"] == []
    opts = {(o["kind"], tuple(o["refs"])): o["element_ids"] for o in litrev.script_input(rv, "CH04")["visual_options"]}
    assert opts[("dimensions", (ps[0]["id"],))] == ["data", "topology", "loss"]
    assert ("dimensions", (ps[0]["id"], ps[1]["id"])) not in opts            # the sibling's single note: not offered
    write_script(rv, "CH04", [scene("CH04-S01", 12)])
    assert "the plan gives this chapter the dimensions data scene" in " | ".join(litrev.check_script(rv, "CH04")["errors"])
    dims = scene("CH04-S02", 4, visual={"kind": "dimensions", "refs": [ps[0]["id"]], "reveal": [{"at": 0, "show": ["data"]}, {"at": 2, "show": ["loss"]}]})
    write_script(rv, "CH04", [scene("CH04-S01", 8), dims])
    assert "the plan gives this chapter the standing data scene" in " | ".join(litrev.merge_script(rv, "CH04")["rejected"]["CH04"])
    cid = litrev.load_presentation(rv)["plan"]["chapters"][3]["cluster_ids"][0]
    kp = [k["id"] for k in rv.meta["kpis"]]
    stand = scene("CH04-S00", 4, visual={"kind": "standing", "refs": [cid], "reveal": [{"at": 0, "show": kp[:1]}]}, sources=kp[:1])
    write_script(rv, "CH04", [scene("CH04-S01", 4), stand, dims])          # after the description: refused
    assert "the standing scene (where the family ranks on each comparison) opens the chapter" in " | ".join(litrev.check_script(rv, "CH04")["errors"])
    write_script(rv, "CH04", [stand, scene("CH04-S01", 4), dims])
    assert litrev.merge_script(rv, "CH04")["rejected"] == {}


def test_standing_places_the_family_beside_the_leaders_of_each_comparison(tmp_path):
    # the user, 2026-11-01: "please show for each line of work the place in the leader boards before its description"
    rv, ps = merged_plan(tmp_path)
    p = good_plan(ps)
    p["chapters"][3]["sections"], p["chapters"][3]["summaries"] = [6, 7], ["dimensions"]
    write_plan(rv, p)
    assert litrev.merge_presentation_plan(rv)["errors"] == []
    ctx = litrev._PresCtx(rv)
    plan = litrev.load_presentation(rv)["plan"]
    fam = [c for c in plan["chapters"] if 6 in c["sections"] and c["cluster_ids"]]
    assert fam and all(c["summaries"][0] == "standing" for c in fam)      # the merge adds it, first, to every family chapter
    cid = fam[0]["cluster_ids"][0]
    st = litrev._pres_standing(rv, ctx, cid)
    mem = set(rv.tables["clusters"][cid]["paper_ids"])
    for b in st["boards"]:
        board = ctx.board(b["id"])
        assert b["of"] == len(board) and [r["rank"] for r in b["rows"]] == sorted(r["rank"] for r in b["rows"])
        assert {r["paper_id"] for r in b["rows"]} >= {e["paper_id"] for e in board[:litrev.PRES_STANDING_TOP]}
        mine = [e for e in board if e["paper_id"] in mem]
        assert b["best"] == (mine[0]["rank"] if mine else None) and all(r["mine"] == (r["paper_id"] in mem) for r in b["rows"])
    errs, els = litrev._visual_check(rv, {"kind": "standing", "refs": [cid]}, ctx, "x")
    assert errs == [] and els == [k["id"] for k in rv.meta["kpis"]]
    assert litrev._visual_check(rv, {"kind": "standing", "refs": [ps[0]["id"]]}, ctx, "x")[0]          # a paper id: refused
    pk = litrev.script_input(rv, fam[0]["id"])
    assert pk["review"]["standing"][0]["cluster"] == cid and ("standing", (cid,)) in {(o["kind"], tuple(o["refs"])) for o in pk["visual_options"]}
    assert all(str(b["of"]) in pk["allowed_numbers"] for b in st["boards"])       # "sixth of forty-five": the rank's count passes
    # a standing scene is a frame: two chapters placing their families in the same words are no repeat
    frame = {"id": "CH09-S01", "narration": ["On the accuracy comparison, this family ranks sixth of nine papers."],
             "visual": {"kind": "standing", "refs": [cid]}}
    other = {"CH08": {"scenes": [dict(frame, id="CH08-S01")]}}
    assert litrev._repeat_notes(rv, plan, "CH09", [frame], other) == []


def test_a_count_of_members_after_a_named_family_checks_that_family(tmp_path):
    # MDE G6: "In C008, the review found code for 2 of the 4 members" was checked against all 15 family members
    rv, ps = pres_review(tmp_path)
    cx = litrev.count_context(rv)
    ok = [("gap", "In C001, code was found for 1 of the 2 members.")]
    bad = [("gap", "In C001, code was found for 1 of the 3 members.")]
    assert litrev.count_issues(cx, ok) == []
    assert litrev.count_issues(cx, bad) == ["gap: '1 of the 3 members' - 3 does not match the 2 C001 members"]
    whole = [("gap", "Code was found for 1 of the 3 members.")]           # no family named: all family members (4)
    assert litrev.count_issues(cx, whole) == ["gap: '1 of the 3 members' - 3 does not match the 4 family members"]


def test_a_model_size_in_a_record_may_be_said():
    # llm-to-domain-slm CH08: "6.74B to 3.52B parameters" gave only 6 and 3, so "3.52 billion" was refused
    got = litrev._pres_numbers({"r": "ATP prunes LLaMA-7B from 6.74B to 3.52B parameters, 1.5x faster, 70M tokens"}, set())
    assert {"6.74", "3.52", "7", "1.5", "70"} <= got
    assert litrev._pres_numbers({"r": "MAP@100 and BM25"}, set()) == {"100"}       # a number inside a name stays out


def test_a_scale_word_after_digits_is_not_a_number_word():
    # llm-to-domain-slm CH11: "25.5 billion medical tokens" was flagged as a number written in words
    assert not litrev.PRES_NUM_WORDS.search("trained on 25.5 billion medical tokens")
    assert litrev.PRES_NUM_WORDS.search("trained on a billion tokens") and litrev.PRES_NUM_WORDS.search("twenty papers")


def test_readiness_separates_evaluating_the_weights_from_retraining():
    av = lambda code, ck, kind="official": {"code": {"availability": code}, "checkpoint": {"availability": ck, "kind": kind}}
    assert litrev._pres_readiness(av("unknown", "yes"), {"library": "transformers"}) == "evaluate now; no code found"   # Meerkat
    assert litrev._pres_readiness(av("yes", "yes"), {}) == "evaluate now; code found"
    assert litrev._pres_readiness(av("unknown", "yes"), {}) == "weights only: find how to run them"
    assert litrev._pres_readiness(av("yes", "yes", "third-party"), {"library": "transformers"}) == "code only: train first"
    assert litrev._params_text(8030261248) == "8.0B" and litrev._params_text(356712448) == "357M" and litrev._params_text(None) is None
    assert litrev._size_range([1543714304, 32763876352]) == "1.5B\u201332.8B" and litrev._size_range(None) is None


def test_result_spread_reads_the_caveats_then_the_quote():
    assert litrev.result_spread({"caveats": "mean +/- 0.65 std over 3 runs; in-distribution"}) == {"pm": "0.65", "runs": 3}
    assert litrev.result_spread({"caveats": "", "quote": "54.35\\mathbin{\\scriptstyle\\pm 0.50}"}) == {"pm": "0.50"}
    assert litrev.result_spread({"caveats": "single value, no std or run count", "quote": "Cure-Med 51.50"}) is None


def test_viewer_text_drops_record_ids(tmp_path):
    rv, ps = merged_plan(tmp_path)
    c1 = rv.tables["clusters"]["C001"]["name"]
    s = litrev._viewer_text(rv, f"In C001 (weights removed once), the winner (P001) leads K001. C002 adds little (C001, C002).")
    assert not re.search(r"\b(C00\d|P001|K001)\b", s) and s.startswith("In weights removed once, the winner leads")
    assert litrev._viewer_text(rv, "C002 adds little.") == f"the {rv.tables['clusters']['C002']['name']} family adds little."
    assert c1


def test_check_script_refuses_record_ids_and_notes_narration_habits(tmp_path):
    rv, ps = merged_plan(tmp_path)
    say = [SENT] * 7 + ["X2 pits pruning once against pruning while training.", "Here's the catch.",
                        "It leads the board with the best paper.", "The reward stage adds just 4.32 points.",
                        "Two more limits are on screen.", "Our invented team agrees."]
    write_script(rv, "CH04", [scene("CH04-S01", 13, narration=say, claim_type="illustrative", sources=[], on_screen="C001 leads")])
    rep = litrev.check_script(rv, "CH04")
    assert any("record id X2" in e for e in rep["errors"]) and any("on_screen: record id C001" in e for e in rep["errors"])
    notes = " ".join(rep["wording"])
    for what in ("not 'catch'", "'board' blurs", "representative paper", "decision criterion", "do not point at others", "Illustrative badge"):
        assert what in notes, what
    road = {"kind": "roadmap", "refs": [], "reveal": [{"at": 0, "show": ["X1", "X2"]}]}
    assert litrev.REPORT_LISTS        # the fixture report has 2 experiments: 2 cards pass
    write_script(rv, "CH07", [scene("CH07-S01", 6, visual=road, claim_type="action", sources=["X1"])])
    assert not any("experiment cards" in e for e in litrev.check_script(rv, "CH07")["errors"])


def test_plan_warns_on_example_chapters_and_repeated_openings(tmp_path):
    rv, ps = pres_review(tmp_path)
    plan = good_plan(ps)
    plan["recurring_example"]["chapters"] = ["CH01", "CH02", "CH03", "CH04", "CH05", "CH06"]
    for c in plan["chapters"]:
        c["opening"] = "question"
    write_plan(rv, plan)
    w = " ".join(litrev.merge_presentation_plan(rv)["warnings"])
    assert "returns in 6 chapters" in w and "two chapters in a row open the same way" in w and "open with a question" in w
    plan["recurring_example"]["chapters"] = ["CH01", "CH05", "CH07"]
    for c, o in zip(plan["chapters"], ("example", "question", "result", "failure", "constraint", "contrast", "prediction")):
        c["opening"] = o
    write_plan(rv, plan)
    w = " ".join(litrev.merge_presentation_plan(rv)["warnings"])
    assert "returns in" not in w and "open the same way" not in w and "open with a question" not in w
    assert [litrev._example_here(load_plan(rv), c) for c in ("CH01", "CH02", "CH05")] == ["introduce", "none", "revisit"]


def load_plan(rv):
    return litrev.load_presentation(rv)["plan"]


def test_the_executive_summary_names_no_paper(tmp_path, monkeypatch):
    # the user, 2026-10-26: "make the executive summary more fluent (no need to compare and name papers)"
    assert litrev.paper_names_in(None, "Teacher training with CURE-Med beats D-Pruner.", {"CURE-Med", "D-Pruner", "ATP"}) == ["CURE-Med", "D-Pruner"]
    assert litrev.paper_names_in(None, "Pruning with domain text comes close.", {"CURE-Med", "ATP"}) == []
    rv, ps = merged_plan(tmp_path)
    monkeypatch.setattr(litrev, "paper_names", lambda rv: {"CURE-Med"})
    doc = report_doc(ps)
    doc["executive_summary"]["problem"]["text"] = "CURE-Med leads the medical comparison."
    doc["verdict"][0]["principle"] = "CURE-Med shows that worked solutions transfer."
    write_report(rv, doc)
    rep = litrev.merge_report(rv)
    assert rep["errors"] == [] and any(w.startswith("executive_summary.problem: names CURE-Med") for w in rep["warnings"])
    assert any(w.startswith("VD1: names CURE-Med") for w in rep["warnings"])
    write_script(rv, "CH01", [scene("CH01-S01", 4, visual={"kind": "executive-summary", "refs": [], "reveal": []}, claim_type="synthesis",
                                    sources=["TK1"], narration=[SENT] * 3 + ["CURE-Med leads the medical comparison."])])
    assert any("names no paper (CURE-Med)" in w for w in litrev.check_script(rv, "CH01")["wording"])


def test_the_video_calls_a_paper_by_its_method_name_when_its_title_is_a_sentence(tmp_path):
    rv, ps = merged_plan(tmp_path)
    ctx = litrev._PresCtx(rv)
    p = rv.tables["papers"][ps[0]["id"]]
    p["title"] = "Small language models learn enhanced reasoning skills from medical textbooks"
    rid = next(e["result_id"] for e in ctx.board("K001") if e["paper_id"] == p["id"])
    for mv, want in (("Meerkat-8B (ours, 8B; base Llama-3)", "Meerkat"), ("SparseGPT + MixCal (alpha=0.5)", "MixCal"),
                     ("Gemma 1 7B, 50% Pruned (FT)", "Small language models learn enhanced reasoning ski...")):
        rv.tables["results"][rid]["method_variant"] = mv
        assert litrev._PresCtx(rv).name(p["id"]) == want, mv
    p["title"] = "CURE-Med: Curriculum-Informed Reinforcement Learning"
    assert litrev._PresCtx(rv).name(p["id"]) == "CURE-Med"


def test_a_count_followed_by_a_clause_names_a_subset():
    cx = {"c": {"families": 5, "ranked": 12, "unranked_no_board_result": 3, "family_members": 12, "per_family": {}}, "members": {}}
    assert litrev.count_issues(cx, [("x", "Only the two families that remove weights give a smaller model.")]) == []
    assert litrev.count_issues(cx, [("x", "All four families report scores.")])          # the whole set, wrong count
    assert litrev.count_issues(cx, [("x", "Six families that remove weights exist.")])   # more than the whole set


def test_viewer_text_says_comparison_not_board(tmp_path):
    rv, ps = merged_plan(tmp_path)
    assert litrev._viewer_text(rv, "The two boards disagree; the comparison board has 3 papers. Board K001 is coarse.") == \
        f"The two comparisons disagree; the comparison has 3 papers. Comparison {litrev._kpi_short(rv, rv.meta['kpis'][0])} is coarse."
    assert litrev._viewer_text(rv, "a public leaderboard") == "a public leaderboard"


# ---------- the top-down verdict and saying each thing once (the user, 2026-10-28) ----------

def test_merge_report_stores_the_verdict_and_checks_its_rows(tmp_path):
    rv, ps = pres_review(tmp_path)
    r = rv.meta["report"]
    assert [x["id"] for x in r["verdict"]] == ["VD1", "VD2"] and r["verdict"][0]["cluster_ids"] == ["C001"]
    assert set(r["executive_summary"]) == {"problem", "uncertainty", "next_step"}
    assert litrev._pres_ref_ok(rv, "VD2") and not litrev._pres_ref_ok(rv, "VD3")
    assert litrev._pres_record(rv, "VD1", litrev._PresCtx(rv))["choice"] == "A diffusion prior"
    doc = report_doc(ps)
    doc["verdict"] = [dict(doc["verdict"][0], cluster_ids=["C009"]), dict(doc["verdict"][1], support="strong"),
                      {"goal": "Speed", "choice": "x", "principle": "y", "condition": "", "support": "few", "refs": ["C001"]},
                      dict(doc["verdict"][1], refs=["P999"])]
    write_report(rv, doc)
    rep = litrev.merge_report(rv)
    e = " | ".join(rep["errors"])
    for needle in ("verdict[0]: cluster_ids not in this review: C009", "verdict[1]: support 'strong'",
                   "verdict[2] needs goal, choice, principle and condition", "verdict[3]: cite the records"):
        assert needle in e, needle
    doc = report_doc(ps)                     # a family in no row, and a principle too long for the table: warnings
    doc["verdict"][1]["cluster_ids"] = []
    doc["verdict"][0]["principle"] = " ".join(["word"] * 26)
    write_report(rv, doc)
    rep = litrev.merge_report(rv)
    w = " | ".join(rep["warnings"])
    assert rep["errors"] == [] and "family C002" in w and "VD1.principle: 26 words" in w


def test_a_report_without_a_verdict_is_stale_for_the_video_only(tmp_path):
    rv, ps = pres_review(tmp_path)
    rv.meta["report"].pop("verdict")
    assert any("has no verdict" in w for w in litrev.report_warnings(rv))
    assert not any("verdict" in w for w in litrev.validate_review(rv)[1])        # an older report stays whole
    with pytest.raises(SystemExit, match="has no verdict"):
        litrev.presentation_input(rv)
    rv.meta["report"]["executive_summary"].update({"families": {"text": "Two families.", "refs": []}})
    ctx = litrev._PresCtx(rv)
    assert litrev._visual_check(rv, {"kind": "executive-summary", "refs": []}, ctx, "x")[1] == ["problem", "families", "uncertainty", "next_step"]
    assert not litrev._pres_kind_needed(ctx, "verdict")


def test_the_plan_shows_the_verdict_and_gives_each_claim_to_one_chapter(tmp_path):
    rv, ps = merged_plan(tmp_path)
    ctx = litrev._PresCtx(rv)
    assert litrev._visual_check(rv, {"kind": "verdict", "refs": []}, ctx, "x") == ([], ["VD1", "VD2"])
    p = good_plan(ps)
    p["chapters"][0]["summaries"] = ["executive-summary"]
    write_plan(rv, p)
    assert any("verdict data scene of section 1" in e for e in litrev.merge_presentation_plan(rv)["errors"])
    p = good_plan(ps)
    p["chapters"][0]["claims"] = ["VD1", "VD2", "L1"]
    p["chapters"][5]["claims"] = ["L1", "CN1", "G9"]
    write_plan(rv, p)
    rep = litrev.merge_presentation_plan(rv)
    assert any(e.startswith("L1 is a claim of CH01, CH06") for e in rep["errors"]) and any("G9" in w for w in rep["warnings"])
    p["chapters"][5]["claims"] = ["CN1", "CN2"]
    p["chapters"][4]["takeaway"] = p["chapters"][3]["takeaway"] = "Diffusion priors give the sharpest indoor depth maps."
    write_plan(rv, p)
    rep = litrev.merge_presentation_plan(rv)
    assert rep["errors"] == [] and any("CH04 and CH05 have the same takeaway" in w for w in rep["warnings"])
    assert litrev.load_presentation(rv)["plan"]["chapters"][0]["claims"] == ["VD1", "VD2", "L1"]
    pk = litrev.script_input(rv, "CH06")
    assert [c["id"] for c in pk["claims"]] == ["CN1", "CN2"] and pk["claims"][0]["text"] == "The diffusion prior leads the comparison."
    assert pk["claims_elsewhere"][0]["chapter"] == "CH01" and pk["claims_elsewhere"][0]["claims"][0]["text"].startswith("Accuracy indoors: A diffusion prior, because")
    assert litrev.script_input(rv, "CH01")["review"]["report"]["verdict"][0]["id"] == "VD1"
    p["chapters"][0]["claims"] = p["chapters"][5]["claims"] = []
    for c in p["chapters"]:
        c.pop("claims", None)
    write_plan(rv, p)
    assert any("no chapter lists `claims`" in w for w in litrev.merge_presentation_plan(rv)["warnings"])


def test_says_again_finds_restated_claims_not_shared_formulas():
    cw = litrev._content_words
    assert litrev._says_again(cw("Each gain is measured against baselines trained on other data."),
                              cw("Headline gains are measured against baselines with other training data."))
    assert litrev._says_again(cw("Copying stops at the teacher's text."), cw("Copying stops at the teacher's text."))
    assert not litrev._says_again(cw("Here ATP reports the highest score."), cw("Long domain pre-training reports the highest score: 88.4%."))
    assert not litrev._says_again(cw("The loss is plain next-word prediction."), cw("It trains on 15,000 examples per domain."))


def test_script_checks_note_sentences_said_before_and_claims_of_other_chapters(tmp_path):
    rv, ps = merged_plan(tmp_path)
    p = good_plan(ps)
    p["chapters"][0]["claims"], p["chapters"][4]["claims"] = ["VD1", "VD2"], ["L1", "CN1"]
    write_plan(rv, p)
    assert litrev.merge_presentation_plan(rv)["errors"] == []
    said = "Each gain is measured against baselines trained on other data."
    other = ["The generator already knows indoor shapes from its pretraining.", "So its depth maps keep sharp edges.",
             "The second family regresses depth in one pass.", "It needs no sampling steps at all.",
             "Training takes one loss and one network."]
    write_script(rv, "CH05", [scene("CH05-S01", 6, narration=[said] + other + [SENT] * 7, visual={"kind": "kpi-chart", "refs": ["K001"], "reveal": []},
                                    claim_type="synthesis", sources=["L1"])])
    assert litrev.merge_script(rv, "CH05")["merged"] == ["CH05"]
    new = ["Headline gains are measured against baselines with other training data.",
           "The diffusion family samples many steps per image.", "Each step refines the depth map a little.",
           "Its prior comes from billions of photos.", "That prior explains its sharp edges.", "Its prior comes from billions of photos!"]
    write_script(rv, "CH04", [scene("CH04-S01", 6, narration=new, sources=[ps[0]["id"], "L1"])])
    rep = litrev.check_script(rv, "CH04")
    notes = " | ".join(rep["repetition"])
    assert "CH04-S01 sentence 0 says again what CH05 said" in notes
    assert "CH04-S01 sentence 5 says again what CH04-S01 sentence 3 said" in notes
    assert "cites L1 (CH05), the claim of another chapter" in notes
    pk = litrev.script_input(rv, "CH04")
    assert pk["said_elsewhere"]["CH05"][0] == said and "CH04" not in pk["said_elsewhere"]
    # the executive summary points to the claims it summarises: no claim note there, but no repeated sentence either
    write_script(rv, "CH01", [scene("CH01-S01", 3, narration=[said, "The diffusion family wins indoors.", "Speed favours regression."],
                                    visual={"kind": "verdict", "refs": [], "reveal": [{"at": 1, "show": ["VD1"]}]},
                                    claim_type="synthesis", sources=["L1", "VD1"])])
    notes = " | ".join(litrev.check_script(rv, "CH01")["repetition"])
    assert "says again what CH05 said" in notes and "claim of another chapter" not in notes


def test_merge_script_skips_the_files_of_an_earlier_plan(tmp_path):
    rv, ps = merged_plan(tmp_path)
    write_script(rv, "CH09", [scene("CH09-S01", 3)])          # the plan has CH01-CH07
    rep = litrev.merge_script(rv)
    assert "CH09" not in rep["rejected"] and any("CH09 is not in the plan" in w and "skipped" in w for w in rep["warnings"])
    assert "not in the plan" in litrev.merge_script(rv, "CH09")["rejected"]["CH09"][0]       # named, it is an error


def test_a_question_asked_at_the_seam_of_two_chapters_is_noted_with_three_shared_words(tmp_path):
    rv, ps = merged_plan(tmp_path)
    end = "Which ones would settle the choices between routes?"
    write_script(rv, "CH04", [scene("CH04-S01", 12, narration=[SENT] * 11 + [end])])
    assert litrev.merge_script(rv, "CH04")["merged"] == ["CH04"]
    again = "Which experiments would settle the route choices?"
    assert not litrev._says_again(litrev._content_words(end), litrev._content_words(again))      # 3 shared words
    write_script(rv, "CH05", [scene("CH05-S01", 3, narration=[again, "We propose three.", "None was run."],
                                    visual={"kind": "kpi-chart", "refs": ["K001"], "reveal": []})])
    notes = " | ".join(litrev.check_script(rv, "CH05")["repetition"])
    assert "CH05-S01 sentence 0 says again what CH04 said next to it" in notes
    write_script(rv, "CH06", [scene("CH06-S01", 3, narration=[again, "We propose three.", "None was run."],
                                    visual={"kind": "matrix", "refs": [], "reveal": []})])
    assert not litrev.check_script(rv, "CH06")["repetition"]           # CH06 does not follow CH04
