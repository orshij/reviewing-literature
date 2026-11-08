"""The questions a step session can ask (app-mode.md), and how the app records each answer through litrev."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

KINDS = ("scope", "boundaries", "included_off_topic", "cap", "kpi", "refresh", "free")
# The user (2026-10-12): as few questions as possible. A question stops the run until the person answers, so a step
# session asks only the step-0 scope, and the refresh brief its own form; the other kinds stay answerable (a question
# parked before this change) but ask_user answers them with DECIDE and the session records its own call (app-mode.md).
ASKED = ("scope", "refresh")
DECIDE = ("DECIDE: the app does not ask the person about {kind}: decide it yourself as app-mode.md's table says, "
          "record it with --by agent, add a 'Claude decided (DEC0xx): ...' line to notes_for_user and continue the "
          "step. Do not ask again.")
APP = "Litrev App"
PID, BID, DID, MID, KID = (re.compile(rf"^{c}\d+$") for c in "PBDMK")


def _strs(x) -> bool:
    return isinstance(x, list) and all(isinstance(i, str) and i.strip() for i in x)


def validate(kind: str, p) -> str | None:
    """None when the proposal fits the kind's shape (app-mode.md), else what is wrong."""
    if kind not in KINDS:
        return f"unknown kind {kind}"
    if not isinstance(p, dict):
        return "proposal must be an object"
    if kind == "scope":
        if not _strs(p.get("in")) or not p.get("in"):
            return "scope needs a non-empty 'in' list of strings"
        if not _strs(p.get("out", [])) or not _strs(p.get("protocol", [])):
            return "'out' and 'protocol' must be lists of strings"
    elif kind == "boundaries":
        bs = p.get("boundaries")
        if not isinstance(bs, list) or not bs:
            return "boundaries needs a non-empty 'boundaries' list"
        for b in bs:
            if not isinstance(b, dict) or not str(b.get("area", "")).strip():
                return "each boundary needs an 'area'"
            if b.get("recommend") not in ("in", "out"):
                return f"boundary {b.get('area')}: 'recommend' must be 'in' or 'out'"
            if not all(PID.match(str(x)) for x in b.get("papers") or []):
                return f"boundary {b.get('area')}: 'papers' must be P ids"
    elif kind == "included_off_topic":
        ps = p.get("papers")
        if not isinstance(ps, list) or not ps:
            return "included_off_topic needs a non-empty 'papers' list"
        for x in ps:
            if not isinstance(x, dict) or not PID.match(str(x.get("id", ""))):
                return "each paper needs an 'id' like P012"
            if x.get("recommend") not in ("keep", "drop"):
                return f"{x.get('id')}: 'recommend' must be 'keep' or 'drop'"
    elif kind == "cap":
        if not isinstance(p.get("proposed"), int) or p["proposed"] <= 0:
            return "cap needs a positive integer 'proposed'"
    elif kind == "kpi":
        ks = p.get("kpis")
        if not isinstance(ks, list) or not ks:
            return "kpi needs a non-empty 'kpis' list"
        for k in ks:
            if not isinstance(k, dict) or not MID.match(str(k.get("metric", ""))):
                return "each KPI needs a 'metric' like M003"
            if bool(k.get("benchmark")) == bool(k.get("dataset")):
                return "each KPI needs exactly one of 'benchmark' (B..) or 'dataset' (D..)"
            if k.get("benchmark") and not BID.match(str(k["benchmark"])) or k.get("dataset") and not DID.match(str(k["dataset"])):
                return "benchmark/dataset ids look like B001 / D001"
            if not isinstance(k.get("splits", []), list):
                return "'splits' must be a list"
    elif kind == "refresh":
        ps = p.get("papers")
        if not isinstance(ps, list) or not ps:
            return "refresh needs a non-empty 'papers' list"
        for x in ps:
            if not isinstance(x, dict) or not PID.match(str(x.get("id", ""))):
                return "each paper needs an 'id' like P130"
            if x.get("recommend") not in ("include", "exclude"):
                return f"{x.get('id')}: 'recommend' must be 'include' or 'exclude'"
    elif kind == "free":
        if not isinstance(p.get("options", []), list):
            return "'options' must be a list"
    return None


def answer_commands(kind: str, a: dict, meta: dict) -> list[list[str]]:
    """litrev argv (no interpreter, script or --review) that record the person's answer, attributed to them."""
    if kind == "scope":
        if not a.get("in"):
            raise ValueError("scope needs at least one 'in' item")
        argv = ["scope"]
        for flag, key in (("--in", "in"), ("--out", "out"), ("--protocol", "protocol")):
            for x in a.get(key) or []:
                argv += [flag, str(x)]
        return [argv + ["--notes", a.get("notes") or f"Accepted Claude's proposal ({APP})", "--by", "user"]]
    if kind == "boundaries":
        cmds, widen = [], []
        norm = lambda x: " ".join(str(x).lower().split())          # litrev matches areas ignoring case and spacing
        stored = {norm(x.get("area", "")): x for x in meta.get("boundaries") or []}
        for b in a["boundaries"]:
            inside = b["decision"] == "in"
            names = [n for n in dict.fromkeys([b["area"], *(b.get("aliases") or [])]) if norm(n) in stored]
            open_names = [n for n in names if not stored[norm(n)].get("decision_id")]
            if inside:
                widen.append(b["area"])       # widened below unless scope.in already has it (also after a retry)
            if names and not open_names:
                continue                      # already decided (e.g. recorded before a retry): no new decision
            argv = ["decide", "--kind", "scope"]
            for n in open_names:              # litrev refuses a --boundary that is not a stored area (live run)
                argv += ["--boundary", n]
            cmds.append(argv + ["--decision", f"{b['area']}: {'in scope' if inside else 'out of scope'}",
                                "--reason", b.get("reason") or f"the user's call ({APP})", "--by", "user"])
        sc = meta.get("scope") or {}
        have = {norm(x) for x in sc.get("in") or []}
        widen = [w for w in dict.fromkeys(widen) if norm(w) not in have]
        if widen:
            argv = ["scope"]
            for x in list(sc.get("in") or []) + widen:
                argv += ["--in", x]
            for x in sc.get("out") or []:
                argv += ["--out", x]
            for x in sc.get("protocol") or []:
                argv += ["--protocol", x]
            cmds.append(argv + ["--notes", f"widened by the user: {', '.join(widen)} ({APP})", "--by", "user"])
        return cmds
    if kind == "included_off_topic":
        cmds = []
        for p in a["papers"]:
            keep = p["decision"] == "keep"
            cmds.append(["screen", "--id", p["id"], "--decision", "included" if keep else "excluded", "--reason",
                         f"user ({APP}): {p.get('reason') or ('kept by the user' if keep else 'off-topic')}"])
        ids = ",".join(p["id"] for p in a["papers"])
        cmds.append(["decide", "--kind", "screen", "--refs", ids, "--decision",
                     "included papers screened off-topic: " + "; ".join(f"{p['id']} {p['decision']}" for p in a["papers"]),
                     "--reason", f"the user's call ({APP})", "--by", "user"])
        return cmds
    if kind == "cap":
        caps = dict(meta.get("caps") or {})
        old, new = caps.get("max_papers"), int(a["proposed"])
        caps["max_papers"] = new
        return [["set-meta", "--field", "caps", "--json", json.dumps(caps)],
                ["decide", "--kind", "cap", "--decision", f"cap {old} -> {new}",
                 "--reason", a.get("reason") or f"the user's call ({APP})", "--by", "user"]]
    if kind == "kpi":
        have = meta.get("kpis") or []
        ids = {k.get("id") for k in have}
        def recorded(k):      # litrev refuses a duplicate add; a retry after a partial failure must skip it
            return any(o.get("benchmark_id") == (k.get("benchmark") or None) and o.get("dataset_id") == (k.get("dataset") or None)
                       and o.get("metric_id") == k["metric"] and o.get("metric_variant") == (k.get("metric_variant") or None)
                       and sorted(map(str, o.get("splits") or [])) == sorted(map(str, k.get("splits") or [])) for o in have)
        def replaces(k, o):   # same board (benchmark or dataset, metric, variant) on overlapping splits: an edit
            same = (o.get("benchmark_id") == (k.get("benchmark") or None) and o.get("dataset_id") == (k.get("dataset") or None)
                    and o.get("metric_id") == k["metric"] and o.get("metric_variant") == (k.get("metric_variant") or None))
            old, new = set(map(str, o.get("splits") or [])), set(map(str, k.get("splits") or []))
            return same and (bool(old & new) or not old or not new)
        keep = {o.get("id") for o in have for k in a["kpis"] if recorded(k) and replaces(k, o)
                and sorted(map(str, o.get("splits") or [])) == sorted(map(str, k.get("splits") or []))}
        drop = [k for k in a.get("remove") or [] if k in ids]
        drop += [o["id"] for o in have for k in a["kpis"]     # litrev has no edit: remove, then add (M2 live run)
                 if o.get("id") not in keep and replaces(k, o)]
        cmds = [["kpi", "--remove", k] for k in dict.fromkeys(drop)]      # removes first: never over litrev's 6
        for k in a["kpis"]:
            if recorded(k):
                continue
            argv = ["kpi", "--add"] + (["--benchmark", k["benchmark"]] if k.get("benchmark") else ["--dataset", k["dataset"]])
            argv += ["--metric", k["metric"]]
            if k.get("metric_variant"):
                argv += ["--metric-variant", k["metric_variant"]]
            for s in k.get("splits") or []:
                argv += ["--split", s]
            cmds.append(argv + ["--rationale", k.get("rationale") or f"chosen by the user ({APP})"])
        cmds.append(["decide", "--kind", "kpi", "--decision", f"{len(a['kpis'])} KPI(s) chosen by the user",
                     "--reason", a.get("reason") or f"the user's call ({APP})", "--by", "user"])
        return cmds
    if kind == "refresh":
        date = a.get("date") or (meta.get("last_refresh") or {}).get("date") or "the last refresh"
        approve, reject = list(a.get("approve") or []), list(a.get("reject") or [])
        if not approve and not reject:
            raise ValueError("refresh needs papers to approve or leave out")
        cmds = []
        if a.get("raise_cap_to"):
            caps = dict(meta.get("caps") or {})
            old, new = caps.get("max_papers"), int(a["raise_cap_to"])
            caps["max_papers"] = new
            cmds += [["set-meta", "--field", "caps", "--json", json.dumps(caps)],
                     ["decide", "--kind", "cap", "--decision", f"cap {old} -> {new}", "--reason",
                      f"room for the papers approved in the refresh of {date} ({APP})", "--by", "user"]]
        if approve:
            cmds.append(["screen", "--id", ",".join(approve), "--decision", "included", "--reason",
                         f"user ({APP}): approved in the refresh of {date}"])
        if reject:
            cmds.append(["screen", "--id", ",".join(reject), "--decision", "excluded", "--reason",
                         f"user ({APP}): not approved in the refresh of {date}"])
        cmds.append(["decide", "--kind", "screen", "--refs", ",".join(approve + reject), "--decision",
                     f"refresh {date}: added {', '.join(approve) or 'none'}; left out {len(reject)}",
                     "--reason", a.get("note") or f"the user's call ({APP})", "--by", "user"])
        return cmds
    if kind == "free":
        return []
    raise ValueError(f"unknown kind {kind}")


def summarize(kind: str, a: dict) -> str:
    if kind == "scope":
        return f"in: {'; '.join(a.get('in') or [])} | out: {'; '.join(a.get('out') or [])} | protocol: {'; '.join(a.get('protocol') or [])}"
    if kind == "boundaries":
        return "; ".join(f"{b['area']} {b['decision']}" for b in a.get("boundaries") or [])
    if kind == "included_off_topic":
        return "; ".join(f"{p['id']} {p['decision']}" for p in a.get("papers") or [])
    if kind == "cap":
        return f"cap {a.get('proposed')}"
    if kind == "kpi":
        return "; ".join(f"{k.get('benchmark') or k.get('dataset')} {k['metric']} {k.get('metric_variant') or ''} {','.join(k.get('splits') or [])}".strip()
                         for k in a.get("kpis") or [])
    if kind == "refresh":
        s = f"approved {', '.join(a.get('approve') or []) or 'none'}; left out {len(a.get('reject') or [])}"
        return s + (f"; cap raised to {a['raise_cap_to']}" if a.get("raise_cap_to") else "")
    return str(a.get("text", ""))[:300]


@dataclass
class RecordResult:
    ok: bool
    commands: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    error: str = ""
    reply: str = ""
    summary: str = ""


def record(run, kind: str, answer: dict, meta: dict) -> RecordResult:
    """Run the commands in order; stop at the first failure (nothing goes back to Claude until all succeed)."""
    try:
        cmds = answer_commands(kind, answer, meta)
    except (KeyError, ValueError, TypeError) as e:
        return RecordResult(False, [], [], f"invalid answer: {e}")
    outs = []
    for argv in cmds:
        p = run(argv)
        outs.append({"argv": argv, "exit": p.returncode, "stdout": (p.stdout or "")[-500:], "stderr": (p.stderr or "")[-500:]})
        if p.returncode != 0:
            return RecordResult(False, cmds, outs, f"`{argv[0]}` failed: {((p.stderr or '') + (p.stdout or '')).strip()[-300:]}")
    summary = summarize(kind, answer)
    done = "; ".join(" ".join(c[:2]) for c in cmds) or "nothing to record"
    reply = (f"User answered; already recorded with: {done}. Answer: {json.dumps(answer, ensure_ascii=False)}. "
             f"Do not record it again. Continue the step.")
    return RecordResult(True, cmds, outs, reply=reply, summary=summary)
