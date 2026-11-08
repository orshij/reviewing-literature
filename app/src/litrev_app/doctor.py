"""Doctor feature (M1b's server loads it from server.FEATURES): GET /api/doctor, POST /api/doctor/<id>/fix, and
`litrev-app doctor` for install.sh. The checks that run programs are cached for a minute; the archived-leaderboards
check is recomputed on every request because its build reports progress, and the OpenAlex-key check because Settings
changes it. The video check is recomputed too (the voice install's progress); its voice probe keeps its own result
per venv state, and Check again (`fresh`) probes once more."""
from __future__ import annotations

import json
import threading
import time

from . import doctor_fix
from .doctor_checks import (VOICE_PROBES, VoiceProbes, check_openalex, check_pwc, check_video, default_inputs,
                            format_table, inputs_from_cfg, run_checks)

CACHE_S = 60.0
_cache: dict = {"at": 0.0, "checks": None}
_lock = threading.Lock()


def invalidate() -> None:
    with _lock:
        _cache.update(at=0.0, checks=None)


def checks_for(app, fresh: bool = False, clock=time.monotonic) -> list[dict]:
    inp = inputs_from_cfg(app.cfg)
    jobs = doctor_fix.JOBS.snapshot()
    if fresh:
        VOICE_PROBES.forget_done()
    with _lock:
        hit = (_cache["checks"] if not fresh and _cache["checks"] is not None and clock() - _cache["at"] < CACHE_S
               else None)
    if hit is None:
        hit = [c.to_dict() for c in run_checks(inp, jobs=jobs)]
        with _lock:
            _cache.update(at=clock(), checks=hit)
    fresh_rows = {"pwc": check_pwc(inp, jobs.get("pwc")).to_dict(),
                  "openalex": check_openalex(inp).to_dict(),   # a key saved in Settings shows at once
                  "video": check_video(inp, job=jobs.get("video")).to_dict()}   # the voice install's progress
    return [fresh_rows.get(c["id"], c) for c in hit]


def _flag(req, name: str) -> bool:
    v = (getattr(req, "query", None) or {}).get(name)
    if isinstance(v, list):
        v = v[0] if v else None
    return v not in (None, "", "0", "false")


def get_doctor(app, req):
    checks = checks_for(app, fresh=_flag(req, "fresh"))
    return 200, {"checks": checks, "ready": all(c["ok"] for c in checks)}, {}


def post_fix(app, req, check_id):
    try:
        res = doctor_fix.fix(check_id, inputs_from_cfg(app.cfg))
    except KeyError:
        return 400, {"error": f"There is no automatic fix for '{check_id}'."}, {}
    except Exception as e:                      # e.g. Terminal could not be opened
        return 500, {"error": f"The fix could not start: {e}"}, {}
    invalidate()
    return (202 if res.get("started") else 200), res, {}


def register(router) -> None:
    router.add("GET", r"^/api/doctor$", get_doctor)
    router.add("POST", r"^/api/doctor/(?P<check_id>[a-z]+)/fix$", post_fix)


def cli_main(argv: list[str]) -> int:
    """`litrev-app doctor [--json]`: exit 0 when nothing fails, 1 otherwise (install.sh and update.sh read it)."""
    checks = run_checks(default_inputs(), jobs=doctor_fix.JOBS.snapshot(),
                        probes=VoiceProbes(spawn=lambda f: f()))     # no page to poll: wait for the voice probe
    print(json.dumps([c.to_dict() for c in checks], indent=1) if "--json" in argv else format_table(checks))
    return 0 if all(c.ok for c in checks) else 1
