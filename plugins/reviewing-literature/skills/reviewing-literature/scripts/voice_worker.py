"""Voice worker for step 8 (audio). Runs inside the TTS venv (kokoro 0.9.4, misaki 0.9.4, soundfile, mlx-whisper),
never inside litrev.py, which stays stdlib-only. litrev.py writes a job file and runs:

    <venv>/bin/python voice_worker.py tts <job.json> <result.json>
    <venv>/bin/python voice_worker.py asr <job.json> <result.json>

tts job: {"voice": "af_heart", "speed": 1.0, "lang": "a", "items": [{"id", "text", "out"}]}; each item is ONE
sentence (Kokoro reads at most 510 phonemes per chunk, and its truncation warning must stay visible: kokoro#367).
result: {"items": [{"id", "out", "dur", "tokens": [{"text", "start", "end"}], "trimmed_s", "chunks"}], "engine": ...}.
asr job: {"model": "mlx-community/whisper-small.en-mlx", "items": [{"id", "wav"}]};
result: {"items": [{"id", "text", "words": [{"word", "start", "end"}]}]}.
"""
from __future__ import annotations

import json
import sys
import time

SR = 24000          # Kokoro's sample rate
TAIL_S = 0.12       # kept after the last word's end: a final punctuation token can carry voiced junk (kokoro#365)


def tts(job: dict) -> dict:
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    import kokoro
    t0 = time.time()
    pipe = KPipeline(lang_code=job.get("lang", "a"), repo_id="hexgrad/Kokoro-82M")
    out = {"engine": {"name": "kokoro", "version": getattr(kokoro, "__version__", None), "voice": job.get("voice", "af_heart"),
                      "speed": job.get("speed", 1.0), "load_s": round(time.time() - t0, 1)}, "items": []}
    for it in job["items"]:
        t1 = time.time()
        # speed stays 1.0 by default: speed < 1 stretches the boundary tokens into a phantom vowel (kokoro#344)
        res = list(pipe(it["text"], voice=job.get("voice", "af_heart"), speed=job.get("speed", 1.0), split_pattern=None))
        if not res:
            out["items"].append({"id": it["id"], "out": it["out"], "error": "no audio"})
            continue
        parts, toks, off = [], [], 0.0
        for r in res:
            a = r.audio.numpy() if hasattr(r.audio, "numpy") else np.asarray(r.audio)
            for t in r.tokens or []:
                if t.start_ts is not None and t.end_ts is not None:
                    toks.append({"text": t.text, "start": round(off + t.start_ts, 3), "end": round(off + t.end_ts, 3)})
            parts.append(a)
            off += len(a) / SR
        audio = np.concatenate(parts)
        trimmed = 0.0
        words = [t for t in toks if any(ch.isalnum() for ch in t["text"])]
        if words:
            cut = min(len(audio), int((words[-1]["end"] + TAIL_S) * SR))
            trimmed = round((len(audio) - cut) / SR, 3)
            audio = audio[:cut]
        sf.write(it["out"], audio, SR, subtype="PCM_16")
        out["items"].append({"id": it["id"], "out": it["out"], "dur": round(len(audio) / SR, 3), "tokens": toks,
                             "trimmed_s": trimmed, "chunks": len(res), "synth_s": round(time.time() - t1, 2)})
    return out


def asr(job: dict) -> dict:
    import mlx_whisper
    model = job.get("model", "mlx-community/whisper-small.en-mlx")
    out = {"engine": {"name": "mlx-whisper", "model": model}, "items": []}
    for it in job["items"]:
        r = mlx_whisper.transcribe(it["wav"], path_or_hf_repo=model, word_timestamps=True, language="en",
                                   condition_on_previous_text=False)
        out["items"].append({"id": it["id"], "text": r.get("text", "").strip(),
                             "words": [{"word": w["word"].strip(), "start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3)}
                                       for s in r.get("segments", []) for w in s.get("words", [])]})
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("tts", "asr"):
        print(__doc__, file=sys.stderr)
        return 2
    job = json.loads(open(argv[2]).read())
    res = tts(job) if argv[1] == "tts" else asr(job)
    with open(argv[3], "w") as fh:
        json.dump(res, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
