"""Step 8 video: timeline, reveal states and captions (pure), the PowerPoint deck, plus one small render when Chrome and ffmpeg exist.
Run: python3 -m pytest tests -q"""
import json
import shutil
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import video_build as vb  # noqa: E402

FPS = vb.FPS


def sc(dur, starts=(0.0,), reveal=(), transition="cut", **kw):
    return dict({"id": kw.pop("id", "S"), "dur": dur, "sentence_starts": list(starts), "reveal": list(reveal),
                 "transition": transition}, **kw)


def test_frames_round_up_and_the_total_is_the_audio_rounded_per_scene():
    assert vb.frames(1.0) == 30 and vb.frames(1.01) == 31 and vb.frames(0.0333334) == 2
    scenes = [sc(2.51), sc(3.2), sc(1.7)]
    segs = vb.timeline(scenes)
    assert sum(s["n_f"] for s in segs) == vb.total_frames(scenes) == 76 + 96 + 51
    assert [s["start_f"] for s in segs] == [0, 76, 172]                 # contiguous: no gap, no overlap


def test_reveal_states_start_at_their_sentence():
    s = sc(10.0, starts=(0.0, 3.0, 6.5), reveal=[{"at": 0, "show": ["a"]}, {"at": 1, "show": ["b"]}, {"at": 2, "show": ["c", "d"]}])
    segs = vb.timeline([s])
    assert [(x["state"], x["start_f"], x["n_f"]) for x in segs] == [(0, 0, 90), (1, 90, 105), (2, 195, 105)]
    st = vb.states_of(s)
    assert st[0] == {"shown": ["a"], "latest": ["a"]} and st[2] == {"shown": ["a", "b", "c", "d"], "latest": ["c", "d"]}
    assert vb.states_of(sc(2.0)) == [{"shown": [], "latest": []}]   # no reveal: the frame only


def test_a_fade_takes_its_time_from_the_scene_before_it():
    scenes = [sc(2.0), sc(2.0, transition="fade"), sc(2.0)]
    segs = vb.timeline(scenes)
    fade = [s for s in segs if s["kind"] == "fade"]
    assert len(fade) == 1 and fade[0]["n_f"] == vb.frames(vb.FADE_S) and fade[0]["start_f"] == 60 - fade[0]["n_f"]
    assert fade[0]["from_scene"] == 0 and fade[0]["scene"] == 1
    assert sum(s["n_f"] for s in segs) == 180                          # the video is still exactly the audio long
    ends = [s["start_f"] + s["n_f"] for s in segs]
    assert all(a == b["start_f"] for a, b in zip(ends, segs[1:]))


def test_cues_follow_the_spoken_words_and_split_long_sentences():
    long = ("This sentence is long enough that one caption cannot hold it, so the builder splits it at the comma "
            "that sits nearest the middle of the text.")
    s = {"narration": ["Short one.", long], "sentence_starts": [0.0, 1.5],
         "words": [{"start": 0.1, "end": 0.9, "sentence": 0}] + [{"start": 1.6 + 0.3 * i, "end": 1.85 + 0.3 * i, "sentence": 1} for i in range(28)]}
    cs = vb.cues([s], [60])                                            # the scene starts at frame 60 = 2.0 s
    assert cs[0] == {"start": 2.1, "end": 2.9, "text": "Short one."}
    assert len(cs) == 3 and cs[1]["start"] == pytest.approx(3.6) and cs[-1]["end"] == pytest.approx(2.0 + 1.85 + 0.3 * 27)
    assert all(len(c["text"]) <= vb.CUE_CHARS for c in cs) and cs[1]["end"] <= cs[2]["start"]
    out = vb.srt(cs)
    assert out.startswith("1\n00:00:02,100 --> 00:00:02,900\nShort one.\n\n2\n") and vb.vtt(cs).startswith("WEBVTT\n\n00:00:02.100")
    assert all(len(line) <= 42 or " " not in line for line in out.splitlines())


def _tone_wav(p: Path, secs: float):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(vb.SR)
        import math
        w.writeframes(b"".join(int(8000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) for i in range(int(secs * vb.SR))))


@pytest.mark.skipif(not (vb.chrome_path() and shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs Chrome and ffmpeg")
def test_small_render_is_frame_exact_and_its_picture_changes_where_planned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # relative work and output paths, as `video --review .` gives them
    for i, d in enumerate((1.6, 1.3)):
        _tone_wav(tmp_path / f"s{i}.wav", d)
    scenes = [sc(1.6, starts=(0.0, 0.8), reveal=[{"at": 0, "show": ["0"]}, {"at": 1, "show": ["1"]}], wav=str(tmp_path / "s0.wav"),
                 narration=["One.", "Two."], words=[{"start": 0.0, "end": 0.5, "sentence": 0}, {"start": 0.8, "end": 1.3, "sentence": 1}]),
              sc(1.3, transition="fade", wav=str(tmp_path / "s1.wav"), narration=["Three."], words=[{"start": 0.0, "end": 0.9, "sentence": 0}])]
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "A chapter", "question": "Q?"}], "families": [], "obj": {},
            "scenes": [{"id": "CH01-S01", "ch": 0, "kind": "claim", "refs": [], "states": vb.states_of(scenes[0]), "reveals": True,
                        "lines": ["First line", "Second line"], "claim": "synthesis", "sources": "", "illustrative": False},
                       {"id": "CH01-S02", "ch": 0, "kind": "chapter", "refs": [], "states": vb.states_of(scenes[1]), "reveals": False,
                        "lines": [], "claim": "synthesis", "sources": "", "illustrative": False}]}
    tpl = Path(__file__).parent.parent / "scripts" / "video_template.html"
    rep = vb.build_video(data, scenes, Path("work"), Path("out.mp4"), tpl, workers=2)
    assert rep["probe"]["frames"] == rep["frames_expected"] == 48 + 39
    assert abs(rep["probe"]["audio_s"] - rep["probe"]["video_s"]) < 0.1 and rep["probe"]["subtitles"]
    assert rep["picture_changes"]["missing"] == [] and rep["picture_changes"]["max_offset_frames"] <= 1.5
    assert Path(rep["srt"]).read_text().count("-->") == 3 and not rep["layout_warnings"]


def kpi_page(tmp_path, reveal, n=40, template=None):
    """The scene page of one kpi-chart scene over an n-paper board."""
    rows = [{"paper_id": f"P{i:03d}", "label": f"Method {i}", "rank": i, "sub": "(2025)", "value": round(2 + i / 10, 6),
             "num": (2 + i / 10) / 100, "cluster": None} for i in range(1, n + 1)]
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "A chapter", "question": "Q?"}], "families": [],
            "obj": {"K1": {"label": "NYUv2 AbsRel", "rows": rows, "units": "%", "note": f"{n} papers, best row per paper",
                           "direction": "lower is better"}},
            "scenes": [{"id": "CH01-S01", "ch": 0, "kind": "kpi-chart", "refs": ["K1"], "states": vb.states_of(sc(1.0, reveal=reveal)),
                        "reveals": True, "lines": [], "claim": "reported", "sources": "", "illustrative": False}]}
    tpl = template or Path(__file__).parent.parent / "scripts" / "video_template.html"
    page = tmp_path / "scene.html"
    page.write_text(tpl.read_text().replace("/*__DATA__*/", json.dumps(data).replace("<", "\\u003c")))
    return page


ROWS_JS = ("JSON.stringify({rows: [].map.call(document.querySelectorAll('#vis .kr'), function(e){ return [e.querySelector('.lb').textContent,"
           " e.querySelector('.val').textContent, e.className]; }), sub: document.querySelector('#vis .sub').textContent})")


@pytest.mark.skipif(not vb.chrome_path(), reason="needs Chrome")
def test_kpi_chart_draws_only_the_revealed_papers_without_rank_badges(tmp_path):
    # MDE: a hidden row kept its height, so a 45-paper board with 3 papers shown ran 900 px past the frame
    page = kpi_page(tmp_path, [{"at": 0, "show": ["P001", "P002"]}, {"at": 1, "show": ["P030"]}])
    chrome = vb.Chrome()
    try:
        chrome.open(page.as_uri())
        warns = [chrome.shot(f"renderState(0,{k},null)", tmp_path / f"{k}.png") for k in (0, 1)]
        got = json.loads(chrome.call("Runtime.evaluate", expression=ROWS_JS, returnByValue=True)["result"]["value"])
        empty = kpi_page(tmp_path, [])                       # a prediction scene: the frame only
        chrome.open(empty.as_uri())
        chrome.shot("renderState(0,0,null)", tmp_path / "e.png")
        none = json.loads(chrome.call("Runtime.evaluate", expression=ROWS_JS, returnByValue=True)["result"]["value"])
    finally:
        chrome.close()
    assert warns == [[], []]
    # the bars keep the order; a '#30' badge read as a ranking across settings the papers do not share (outside review)
    assert len(got["rows"]) == 3 and not any("#" in r[0] for r in got["rows"]) and got["rows"][2][1] == "5%" and "latest" in got["rows"][2][2]
    assert "3 of 40 papers shown" in got["sub"] and "reported by each paper" in got["sub"]
    assert none["rows"] == [] and "40 papers, best row per paper" in none["sub"]


@pytest.mark.skipif(not vb.chrome_path(), reason="needs Chrome")
def test_results_table_draws_the_rows_its_scene_names(tmp_path):
    # llm-to-domain-slm: QuIP#'s table had 20 rows; the page drew the first 12 (13B, 70B), so the 7B rows the scene
    # revealed were never drawn (2 picture changes missing) and the long labels overflowed by 72 px
    rows = [{"label": f"Llama 2 {s} QuIP# {b}-bit variant {v} (RHT + RVQ with E8P codebook, weight-only PTQ of FP16)",
             "own": True, "cells": ["5.%d" % v, "7.%d" % v], "best": [False, False]}
            for s in ("70B", "13B", "7B") for b in (3, 4) for v in range(1, 4)] + [{"label": "Llama 2 7B FP16 reference", "own": False, "cells": ["5.12", "6.63"]}]
    want = [rows[12]["label"], rows[13]["label"], rows[-1]["label"]]
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "A chapter", "question": "Q?"}], "families": [],
            "obj": {"C001": {"winner": "QuIP#", "table": {"caption": "Table 3", "columns": ["Wiki2", "C4"], "rows": rows}}},
            "scenes": [{"id": "CH01-S01", "ch": 0, "kind": "results-table", "refs": ["C001"],
                        "states": vb.states_of(sc(1.0, reveal=[{"at": 0, "show": want[:2]}, {"at": 1, "show": want[2:]}])),
                        "reveals": True, "lines": [], "claim": "reported", "sources": "", "illustrative": False},
                       # a prediction scene: an empty reveal draws the frame only (it drew 12 hidden rows, 529 px too tall)
                       {"id": "CH01-S02", "ch": 0, "kind": "results-table", "refs": ["C001"], "states": vb.states_of(sc(1.0)),
                        "reveals": False, "lines": ["One strip line"], "claim": "reported", "sources": "", "illustrative": False}]}
    page = tmp_path / "scene.html"
    tpl = Path(__file__).parent.parent / "scripts" / "video_template.html"
    page.write_text(tpl.read_text().replace("/*__DATA__*/", json.dumps(data).replace("<", "\\u003c")))
    rows_js = ("JSON.stringify({rows: [].map.call(document.querySelectorAll('#vis tbody tr'), function(e){return [e.getAttribute('data-rid'), e.firstChild.textContent];}),"
               " note: document.querySelector('#vis .note').textContent})")
    chrome = vb.Chrome()
    try:
        chrome.open(page.as_uri())
        warns = [chrome.shot(f"renderState(0,{k},null)", tmp_path / f"{k}.png") for k in (0, 1)]
        got = json.loads(chrome.call("Runtime.evaluate", returnByValue=True, expression=rows_js)["result"]["value"])
        blank = chrome.shot("renderState(1,0,null)", tmp_path / "blank.png")
        none = json.loads(chrome.call("Runtime.evaluate", returnByValue=True, expression=rows_js)["result"]["value"])
    finally:
        chrome.close()
    assert warns == [[], []] and [r[0] for r in got["rows"]] == want and "3 of 19 rows shown" in got["note"]
    # the frame shows each method's name, the part before its first bracket; the reveal id stays the whole label
    assert [r[1] for r in got["rows"]] == ["Llama 2 7B QuIP# 3-bit variant 1", "Llama 2 7B QuIP# 3-bit variant 2", "Llama 2 7B FP16 reference"]
    assert blank == [] and none["rows"] == [] and "0 of 19 rows shown" in none["note"]


@pytest.mark.skipif(not vb.chrome_path(), reason="needs Chrome")
def test_matrix_heads_its_columns_with_family_names_and_fits(tmp_path):
    # the video showed "C003 C007" over the columns. The hardest allowed case must fit: 5 families (method-clusters
    # allows 2-5) with 12-word names, 8 problems (LIMIT_ROWS) with 10-word titles, 2 strip lines (3+ warn)
    fams = [{"id": f"C00{i}", "name": f"Family {i}: ground features projected to a top-down view and compared everywhere"}
            for i in range(1, 6)]
    rows = [{"id": f"L{j}", "title": "Accuracy and metric scale drop on unfamiliar cameras and scenes",
             "cells": {f["id"]: "affected:inferred" for f in fams}} for j in range(1, 9)]
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "A chapter", "question": "Q?"}], "families": fams,
            "matrix": {"families": [f["id"] for f in fams], "rows": rows}, "obj": {},
            "scenes": [{"id": "CH01-S01", "ch": 0, "kind": "matrix", "refs": [],
                        "states": vb.states_of(sc(1.0, reveal=[{"at": 0, "show": ["L1"]}])), "reveals": True,
                        "lines": ["Design settings tuned on test sets (review's reading)",
                                  "ScanNet seen in training, authors state"], "claim": "synthesis",
                        "sources": "", "illustrative": False}]}
    page = tmp_path / "scene.html"
    tpl = Path(__file__).parent.parent / "scripts" / "video_template.html"
    page.write_text(tpl.read_text().replace("/*__DATA__*/", json.dumps(data).replace("<", "\\u003c")))
    chrome = vb.Chrome()
    try:
        chrome.open(page.as_uri())
        warn = chrome.shot("renderState(0,0,null)", tmp_path / "m.png")
        heads = json.loads(chrome.call("Runtime.evaluate", returnByValue=True, expression=(
            "JSON.stringify([].map.call(document.querySelectorAll('#vis .mx th.fh'), function(e){ return e.textContent; }))"))["result"]["value"])
    finally:
        chrome.close()
    assert heads == [f["name"] for f in fams] and warn == []


@pytest.mark.skipif(not vb.chrome_path(), reason="needs Chrome")
def test_summary_tables_fit_with_long_rows_and_highlight_the_named_ones(tmp_path):
    # the hardest case the summaries produce: 8 rows, a two-line name and a 100-character definition in each, 3 strip lines
    long_def = "Turning a large, resource-intensive language model into a compact model for devices with little memory."
    rows = {"tasks": [{"id": f"T{n:03d}", "name": "Knowledge distillation from a large language model teacher", "papers": 60 - n,
                       "definition": long_def, "test_sets": ["TREC DL 2019+2020 (pooled)", "WikiText-2"]} for n in range(1, 9)],
            "benchmarks": [{"id": f"D{n:03d}", "name": "TREC DL 2019+2020 (pooled, 97 queries)", "papers": 60 - n, "kpi": n % 3 == 0,
                            "task": "Fine-grained (metric) cross-view localization", "metric": "Localization accuracy within distance threshold"}
                           for n in range(1, 9)],
            "metrics": [{"id": f"M{n:03d}", "name": "Number of trainable parameters", "papers": 60 - n, "kpi": n == 2,
                         "definition": long_def, "direction": "lower is better"} for n in range(1, 9)]}
    sm = {k: {"rows": v, "note": f"8 of 40 {k} the included papers report, by number of papers"} for k, v in rows.items()}
    ids = {k: [r["id"] for r in v] for k, v in rows.items()}
    scenes = [{"id": f"CH01-S0{j}", "ch": 0, "kind": k, "refs": [], "reveals": True, "claim": "reported", "sources": "", "illustrative": False,
               "states": [{"shown": ids[k], "latest": ids[k][:1]}, {"shown": ids[k], "latest": ids[k][1:3]}],
               "lines": ["Most papers test general skills", "Domain sets are few", "KPI: a ranking board"]}
              for j, k in enumerate(("tasks", "benchmarks", "metrics"), 1)]
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "What the papers are tested on", "question": "Q?"}], "families": [],
            "obj": {}, "summaries": sm, "scenes": scenes}
    page = tmp_path / "scene.html"
    tpl = Path(__file__).parent.parent / "scripts" / "video_template.html"
    page.write_text(tpl.read_text().replace("/*__DATA__*/", json.dumps(data).replace("<", "\\u003c")))
    chrome = vb.Chrome()
    got = {}
    try:
        chrome.open(page.as_uri())
        for j, k in enumerate(("tasks", "benchmarks", "metrics")):
            warns = [chrome.shot(f"renderState({j},{st},null)", tmp_path / f"{k}{st}.png") for st in (0, 1)]
            got[k] = (warns, json.loads(chrome.call("Runtime.evaluate", returnByValue=True, expression=(
                "JSON.stringify({rows: [].map.call(document.querySelectorAll('#vis tbody tr'), function(e){ return [e.getAttribute('data-rid'), e.className]; }),"
                " tags: document.querySelectorAll('#vis .tag').length, note: document.querySelector('#vis .note').textContent})"))["result"]["value"]))
    finally:
        chrome.close()
    for k, (warns, g) in got.items():
        assert warns == [[], []], (k, warns)
        assert [r[0] for r in g["rows"]] == ids[k] and [r[0] for r in g["rows"] if "latest" in r[1]] == ids[k][1:3]
        assert not any("dim" in r[1] for r in g["rows"]), k            # the whole table shows; reveals only highlight
    assert got["benchmarks"][1]["tags"] == 2 and got["metrics"][1]["tags"] == 1 and got["tasks"][1]["tags"] == 0
    assert "KPI: a ranking board of this review uses it" in got["metrics"][1]["note"]


@pytest.mark.skipif(not vb.chrome_path(), reason="needs Chrome")
def test_section_scenes_fit_with_long_texts(tmp_path):
    # the ten review sections' data scenes at their longest: _video_section_data cuts the summary parts to 200
    # characters, requirements to 160, list items to 150, dimension notes to 260 (the page cuts them to 150 for two papers
    # and 110 for three) and roadmap cells to 80; each scene
    # must draw without a page error, text >= 22 px, no overflow
    t = lambda n: ("Domain compression keeps most of the quality of the teacher on the medical boards but loses more on "
                   "the legal and code boards when the bit width goes below four bits. ") * 3
    cut = lambda s, n: s[:n]
    dims = {k: cut(t(0), 260) for k in ("data", "representation", "topology", "loss", "training_scheme")}
    labels = {"data": "Data", "representation": "Representation", "topology": "Architecture", "loss": "Losses and objectives",
              "training_scheme": "Training scheme"}
    exp = [{"id": f"X{n}", **{k: cut(t(n), 120) for k in ("experiment", "question", "decision")}} for n in (1, 2, 3, 4, 5)]
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "Sections", "question": "Q?"}],
            "families": [{"id": "C001", "name": "Quantization"}, {"id": "C002", "name": "Distillation"}],
            "obj": {p: {"short": "A long paper short title: with words", "year": 2025, "cluster": "C001", "dims": dims} for p in ("P001", "P002", "P003")},
            "exec": [{"id": k, "label": k.title(), "text": cut(t(0), 200)} for k in ("problem", "families", "findings", "promising", "uncertainty", "next_step")],
            # the verdict at its longest: 5 rows, goal and choice of 8 words, principle and condition cut to 150 and 80
            "verdict": [{"id": f"VD{n}", "goal": "Domain skill on exam questions with labelled data",
                         "choice": "Fine-tune on a large teacher's worked solutions", "families": ["C001", "C002"],
                         "principle": cut(t(n), 150), "condition": cut(t(n), 80), "support": "consistent across comparisons"} for n in range(1, 6)],
            "req": [{"id": k, "label": k.title(), "text": cut(t(0), 160)} for k in ("application_goal", "formal_task", "inputs_outputs", "operating_conditions", "constraints")]
                   + [{"id": "research_questions", "label": "Research questions", "list": [cut(t(0), 110)] * 4}],
            "search": {"first_search": "2026-10-02", "last_search": "2026-10-26", "found": 7019, "included": 137,
                       "sources": [{"name": n, "kinds": [{"kind": "keyword searches", "n": 83}, {"kind": "citing-paper walks", "n": 166}]}
                                   for n in ("arXiv", "OpenAlex", "Semantic Scholar")],
                       "terms": {"run": 83, "total": 120, "examples": ["knowledge distillation large language models survey"] * 6},
                       "walks": {"reference": 12, "citing": 203, "max_hop": 4, "converged": True, "coverage_check": True, "surveys": 4},
                       "criteria": {"in": 6, "out": 5, "protocol": 3, "year_window": 3, "paper_cap": 128},
                       "gaps": {"no_text": 12, "over_cap": 345, "never_screened": 0, "terms_not_run": 37}},
            "prov": [{"id": f"K00{n}", "label": "MedQA (USMLE, 4 options) test · Accuracy", "papers": 14, "reported": 14, "verified": 0, "reproduced": 0} for n in range(1, 7)],
            "weak": {"kinds": ["comparison", "evaluation", "cost", "claim", "generalisation", "data", "assumption", "reproducibility"],
                     "rows": [{"id": f"C00{n}", "name": "Family", "winner": "A long paper short title: with words", "kinds": {"cost": 2, "claim": 1}} for n in range(1, 6)]},
            "feas": [{"id": f"P00{n}", "short": "A long paper short title: with words", "year": 2025, "cluster": "C001", "code": "authors'",
                      "weights": "third-party only", "licence": "bigcode-openrail-m", "size": "15.9B",
                      "readiness": "weights only: find how to run them"} for n in range(1, 9)],
            "dimLabels": labels, "gapTypes": {"evidence-gap": "Evidence gap"},
            "unmeasured": [{"id": f"U{n}", "text": cut(t(n), 150)} for n in range(1, 6)],
            "conditions": [{"id": f"CN{n}", "kind": "unresolved" if n % 2 else "works", "text": cut(t(n), 150)} for n in range(1, 7)],
            "gaps": [{"id": f"G{n}", "type": "evidence-gap", "basis": "outside-knowledge" if n == 3 else "reviewed-papers", "text": cut(t(n), 150)} for n in range(1, 9)],
            "roadmap": {"baseline": {"short": "A long paper short title: with words", "reason": cut(t(0), 110)},
                        "shortlist": [{"short": "A long paper short title"}] * 4, "experiments": exp}}
    kinds = [("executive-summary", [], list(d["id"] for d in data["exec"])), ("verdict", [], [f"VD{n}" for n in range(1, 6)]),
             ("verdict", [], ["VD1", "VD2", "VD3"]), ("requirements", [], [d["id"] for d in data["req"]]),
             ("search", [], ["sources", "terms", "walks", "criteria", "gaps"]), ("dimensions", ["P001", "P002", "P003"], list(labels)),
             ("dimensions", ["P001", "P002"], list(labels)), ("dimensions", ["P001"], list(labels)),
             ("unmeasured", [], [f"U{n}" for n in range(1, 6)]), ("provenance", [], [f"K00{n}" for n in range(1, 7)]),
             ("weak-points", [], [f"C00{n}" for n in range(1, 6)]), ("conditions", [], [f"CN{n}" for n in range(1, 7)]),
             ("feasibility", [], [f"P00{n}" for n in range(1, 9)]), ("gaps", [], [f"G{n}" for n in range(1, 9)]),
             ("roadmap", [], ["baseline", "shortlist"] + [f"X{n}" for n in range(1, 4)]), ("roadmap", [], ["X4", "X5"]),
             ("standing", ["C001"], [f"K00{n}" for n in range(1, 7)]), ("standing", ["C002"], ["K001", "K002", "K003"])]
    # where a family ranks: six comparisons in two rows (5 rows each) and three in one row (7 rows), long names
    row = lambda r, me: {"paper_id": f"P{r:03d}", "name": "A long method name that runs on and on", "rank": r, "mine": me,
                         "cluster": "C001" if me else "C002", "value": "83.79%"}
    board = lambda k, rows, more: {"id": k, "label": "WikiTableQuestions denotation accuracy, test split", "of": 45,
                                   "best": rows[2]["rank"], "members": 9, "more": more, "rows": rows}
    data["standing"] = {"C001": {"cluster": "C001", "name": "Quantization", "members": 12, "on_comparisons": 9, "without_result": 3,
                                 "boards": [board(f"K00{n}", [row(1, False), row(2, False)] + [row(r, True) for r in (6, 9, 30)], 6) for n in range(1, 7)]},
                        "C002": {"cluster": "C002", "name": "Distillation", "members": 12, "on_comparisons": 9, "without_result": 3,
                                 "boards": [board(f"K00{n}", [row(1, False), row(2, False)] + [row(r, True) for r in (6, 9, 30, 31, 40)], 4) for n in range(1, 4)]}}
    data["scenes"] = [{"id": f"CH01-S{j:02d}", "ch": 0, "kind": k, "refs": refs, "reveals": True, "claim": "synthesis", "sources": "",
                       "illustrative": False, "states": [{"shown": ids, "latest": ids[-1:]}], "lines": ["One strip line", "Another strip line"]}
                      for j, (k, refs, ids) in enumerate(kinds, 1)]
    page = tmp_path / "scene.html"
    tpl = Path(__file__).parent.parent / "scripts" / "video_template.html"
    page.write_text(tpl.read_text().replace("/*__DATA__*/", json.dumps(data).replace("<", "\\u003c")))
    chrome = vb.Chrome()
    got = {}
    try:
        chrome.open(page.as_uri())
        for j, (k, _, ids) in enumerate(kinds):
            warns = chrome.shot(f"renderState({j},0,null)", tmp_path / f"{k}.png")
            n = json.loads(chrome.call("Runtime.evaluate", returnByValue=True, expression=(
                "JSON.stringify([].map.call(document.querySelectorAll('#vis [data-rid]'), function(e){ return e.getAttribute('data-rid'); }))"))["result"]["value"])
            got[f"{k}{j}"] = (warns, n)
    finally:
        chrome.close()
    bad = {f"{k}{j}": got[f"{k}{j}"][0] for j, (k, _, _) in enumerate(kinds) if got[f"{k}{j}"][0]}
    assert bad == {}, bad
    for j, (k, _, ids) in enumerate(kinds):
        want = ["baseline", "shortlist"] + ids if k == "roadmap" and "baseline" not in ids else ids   # those two lines always show
        assert got[f"{k}{j}"][1] == want, (k, got[f"{k}{j}"][1])     # every element its reveals may name is drawn, in order


def test_deck_states_follow_the_reveals_the_video_shows():
    s = sc(3.0, starts=(0.0, 1.0, 2.0), reveal=[{"at": 0, "show": ["0"]}, {"at": 2, "show": ["1"]}, {"at": 2, "show": ["2"]},
                                               {"at": 5, "show": ["3"]}])
    assert vb.deck_states(s) == [(0, 0), (1, 2)]          # one state per sentence; a reveal past the last sentence never shows


def _png(path, rgb):
    import struct
    import zlib
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * 4 for _ in range(4))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_pptx_has_one_slide_per_scene_its_reveals_on_clicks_and_the_narration_in_the_notes(tmp_path):
    import zipfile
    from xml.dom import minidom
    scenes = [sc(3.0, starts=(0.0, 1.0, 2.0), reveal=[{"at": 0, "show": ["0"]}, {"at": 1, "show": ["1"]}, {"at": 2, "show": ["2"]}],
                 transition="fade", narration=["One.", "Two.", "Three."]),
              sc(1.0, transition="fade", narration=["Four & <five>."])]
    data = {"title": "T", "subtitle": "A test", "chapters": [{"id": "CH01", "title": "First"}, {"id": "CH02", "title": "Second"}],
            "scenes": [{"id": "CH01-S01", "ch": 0, "kind": "claim", "states": vb.states_of(scenes[0]), "lines": ["A line"]},
                       {"id": "CH02-S01", "ch": 1, "kind": "chapter", "states": vb.states_of(scenes[1]), "lines": []}]}
    for name, rgb in (("000-00", (255, 0, 0)), ("000-01", (0, 255, 0)), ("000-02", (0, 255, 0)), ("001-00", (255, 0, 0))):
        _png(tmp_path / f"{name}.png", rgb)    # state 2 looks like state 1; scene 2 looks like scene 1's first state
    rep = vb.build_pptx(data, scenes, lambda si, k: tmp_path / f"{si:03d}-{k:02d}.png", tmp_path / "deck.pptx", "2026-10-25T06:00:00Z")
    assert (rep["slides"], rep["pictures"], rep["clicks"], rep["media"]) == (2, 3, 1, 2)
    z = zipfile.ZipFile(tmp_path / "deck.pptx")
    names = z.namelist()
    assert names[0] == "[Content_Types].xml"
    xml = {n: minidom.parseString(z.read(n)) for n in names if n.endswith((".xml", ".rels"))}      # every part parses
    ct = z.read("[Content_Types].xml").decode()
    assert all(f'PartName="/{n}"' in ct for n in names if n.endswith(".xml") and n != "[Content_Types].xml")
    s1, s2 = z.read("ppt/slides/slide1.xml").decode(), z.read("ppt/slides/slide2.xml").decode()
    assert s1.count('nodeType="clickEffect"') == 1 and "<p:fade/>" not in s1     # no fade into the first slide
    assert "<p:timing>" not in s2 and "<p:fade/>" in s2
    notes = " ".join(t.firstChild.data for t in xml["ppt/notesSlides/notesSlide1.xml"].getElementsByTagName("a:t"))
    assert "One. [click] Two. Three." in notes and notes.startswith("CH01-S01 · video 00:00-00:03")
    assert "Four & <five>." in " ".join(t.firstChild.data for t in xml["ppt/notesSlides/notesSlide2.xml"].getElementsByTagName("a:t"))
    assert z.read("ppt/presentation.xml").decode().count("<p14:section ") == 2
    try:
        import pptx
    except ImportError:
        return
    deck = pptx.Presentation(str(tmp_path / "deck.pptx"))
    assert len(deck.slides) == 2 and deck.slides[0].notes_slide.notes_text_frame.text.endswith("Three.")


def test_claim_and_example_lines_that_no_reveal_names_show_from_the_start():
    s = sc(3.0, starts=(0.0, 1.0, 2.0), reveal=[{"at": 1, "show": ["1"]}, {"at": 2, "show": ["2"]}])
    st = vb.show_unnamed(vb.states_of(s), 4)                # line 0 (a label) and line 3 are named by no reveal
    assert [x["shown"] for x in st] == [["0", "3"], ["0", "3", "1"], ["0", "3", "1", "2"]]
    assert [x["latest"] for x in st] == [[], ["1"], ["2"]]  # never highlighted as new
    named = [{"at": 0, "show": ["0"]}, {"at": 1, "show": ["1"]}]
    assert vb.show_unnamed(vb.states_of(sc(2.0, starts=(0.0, 1.0), reveal=named)), 2) == vb.states_of(sc(2.0, starts=(0.0, 1.0), reveal=named))


@pytest.mark.skipif(not vb.chrome_path(), reason="needs Chrome")
def test_results_table_draws_only_the_columns_a_reveal_names(tmp_path):
    # D-Pruner's eleven columns were on screen while the voice compared two MedNLI values (outside review, 2026-10-26)
    cols = [f"Set {i} Accuracy" for i in range(1, 12)]
    rows = [{"label": f"Method {r} (variant)", "own": r == 1, "cells": [f"{50 + r + i}.0" for i in range(11)], "best": [False] * 11}
            for r in (1, 2)]
    rows[0]["cells"][1] = "54.35 ± 0.50"
    data = {"title": "T", "chapters": [{"id": "CH01", "title": "A chapter", "question": "Q?"}], "families": [],
            "obj": {"C001": {"winner": "Paper", "table": {"caption": "its table", "columns": cols, "col_ids": [f"col{i}" for i in range(1, 12)], "rows": rows}}},
            "scenes": [{"id": f"CH01-S0{j}", "ch": 0, "kind": "results-table", "refs": ["C001"], "reveals": True, "lines": [], "claim": "reported",
                        "sources": "", "illustrative": False, "states": [{"shown": sh, "latest": []}]}
                       for j, sh in enumerate([["Method 1 (variant)", "Method 2 (variant)", "col2"], ["Method 1 (variant)"]])]}
    page = tmp_path / "scene.html"
    page.write_text((Path(__file__).parent.parent / "scripts" / "video_template.html").read_text().replace("/*__DATA__*/", json.dumps(data)))
    js = "JSON.stringify([[].map.call(document.querySelectorAll('#vis th'), function(e){return e.textContent;}), document.querySelector('#vis tbody td.num').textContent])"
    chrome = vb.Chrome()
    try:
        chrome.open(page.as_uri())
        w0 = chrome.shot("renderState(0,0,null)", tmp_path / "a.png")
        named = json.loads(chrome.call("Runtime.evaluate", expression=js, returnByValue=True)["result"]["value"])
        chrome.shot("renderState(1,0,null)", tmp_path / "b.png")
        alln = json.loads(chrome.call("Runtime.evaluate", expression=js, returnByValue=True)["result"]["value"])
    finally:
        chrome.close()
    assert w0 == [] and named == [["Method", "Set 2 Accuracy"], "54.35 ± 0.50"]
    assert len(alln[0]) == 12                       # no column named: every column, as before
