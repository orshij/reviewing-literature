"""Step 8 video: timeline, captions, Chrome stills over the DevTools protocol, ffmpeg assembly and the sync check.
Stdlib only (like litrev.py). litrev.py builds the scene data and calls `build_video`; nothing here knows the review.

The audio sets the clock: each scene lasts its measured audio, rounded up to a whole frame (the audio is padded by the
same amount), each reveal state starts at its sentence's measured start, and a fade takes its time from the scene
before it, so the video's length is the audio's length.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

FPS = 30
FADE_S = 0.4
W, H = 1920, 1080
CUE_CHARS, CUE_MAX_S = 84, 6.0           # captions: two lines of about 42 characters, at most 6 s each
LOUDNESS = {"I": -16.0, "TP": -1.5, "LRA": 11.0}   # spoken-word target (EBU R128 style), two-pass linear loudnorm


# --------------------------------------------------------------------------
# timeline (pure)
# --------------------------------------------------------------------------

def frames(s: float) -> int:
    return int(math.ceil(round(s * FPS, 6)))


def timeline(scenes: list[dict]) -> list[dict]:
    """Segments of the video, in order. `scenes`: [{id, dur, sentence_starts[], reveal[{at, show[]}], transition}].
    A segment is {scene, state, start_f, n_f, kind: still|fade, from_state?}; `state` k shows the reveal groups whose
    sentence has started. All times are whole frames, so nothing drifts."""
    segs, t = [], 0
    for i, sc in enumerate(scenes):
        n_scene = frames(sc["dur"])
        ats = sorted({r["at"] for r in sc.get("reveal") or [] if isinstance(r.get("at"), int) and r["at"] > 0})
        starts = sc.get("sentence_starts") or [0.0]
        cuts = [0] + [min(n_scene - 1, frames(starts[a])) for a in ats if a < len(starts)]
        cuts = sorted(set(cuts))
        for k, c in enumerate(cuts):
            end = cuts[k + 1] if k + 1 < len(cuts) else n_scene
            if end > c:
                segs.append({"scene": i, "state": k, "start_f": t + c, "n_f": end - c, "kind": "still"})
        nxt = scenes[i + 1] if i + 1 < len(scenes) else None
        if nxt is not None and (nxt.get("transition") == "fade" or sc.get("transition_out") == "fade"):
            nf = min(frames(FADE_S), segs[-1]["n_f"] - 1)
            if nf > 0:        # the fade replaces the end of this scene's last still: the total does not change
                segs[-1]["n_f"] -= nf
                segs.append({"scene": i + 1, "state": 0, "from_scene": i, "from_state": segs[-1]["state"],
                             "start_f": t + n_scene - nf, "n_f": nf, "kind": "fade"})
        t += n_scene
    return segs


def total_frames(scenes: list[dict]) -> int:
    return sum(frames(s["dur"]) for s in scenes)


def states_of(sc: dict) -> list[dict]:
    """Reveal state k of a scene: every element shown so far, and the ones that appeared at this step (`latest`)."""
    groups: dict[int, list] = {}
    for r in sc.get("reveal") or []:
        if isinstance(r.get("at"), int):
            groups.setdefault(r["at"], []).extend(str(x) for x in r.get("show") or [])
    ats = sorted(groups)
    first = [a for a in ats if a <= 0]
    shown = [x for a in first for x in groups[a]]
    out = [{"shown": list(dict.fromkeys(shown)), "latest": list(dict.fromkeys(shown))}]
    for a in [a for a in ats if a > 0]:
        shown = shown + groups[a]
        out.append({"shown": list(dict.fromkeys(shown)), "latest": list(dict.fromkeys(groups[a]))})
    return out


def show_unnamed(states: list[dict], n_lines: int) -> list[dict]:
    """The script prompt's rule for `claim` and `example` scenes: an on-screen line that no reveal names shows from the
    start. It joins every state's `shown`, never `latest` (it is not new at any step). Without it such a line stayed
    faint for the whole scene: 7 "Illustrative ..." labels in two videos, 1.29.0-1.32.0."""
    named = {x for st in states for x in st["shown"]}
    free = [str(i) for i in range(n_lines) if str(i) not in named]
    return [dict(st, shown=free + st["shown"]) for st in states] if free else states


# --------------------------------------------------------------------------
# captions (pure)
# --------------------------------------------------------------------------

def _chunks(text: str, limit: int = CUE_CHARS) -> list[str]:
    """Split a sentence into caption-sized pieces at a comma, colon or semicolon near the middle, else at a space."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    mid = len(text) // 2
    cands = [m.end() for m in re.finditer(r"[,;:]\s", text)] or [m.start() for m in re.finditer(r"\s", text)]
    cut = min(cands, key=lambda c: abs(c - mid))
    return _chunks(text[:cut], limit) + _chunks(text[cut:], limit)


def cues(scenes: list[dict], offsets_f: list[int]) -> list[dict]:
    """Caption cues in the written form. Each sentence starts at its measured start and ends at its last spoken word;
    a long sentence is split, with times shared by its spoken words. `scenes`: [{narration[], sentence_starts[],
    words[{start, end, sentence}]}]; `offsets_f`: each scene's first frame."""
    out = []
    for sc, off in zip(scenes, offsets_f):
        base = off / FPS
        for n, sent in enumerate(sc["narration"]):
            ws = [w for w in sc.get("words") or [] if w.get("sentence") == n]
            s0 = sc["sentence_starts"][n] if n < len(sc["sentence_starts"]) else 0.0
            s1 = ws[-1]["end"] if ws else s0 + 2.0
            if ws:
                s0 = ws[0]["start"]            # the caption appears with the first spoken word
            parts = _chunks(sent)
            total = sum(len(p) for p in parts)
            t, acc = s0, 0
            for p in parts:
                acc += len(p)
                end = s0 + (s1 - s0) * acc / total
                if ws and len(parts) > 1:      # snap the split to the nearest spoken word end
                    end = min((w["end"] for w in ws), key=lambda e: abs(e - end))
                if p is parts[-1]:
                    end = s1
                out.append({"start": round(base + t, 3), "end": round(base + max(end, t + 0.3), 3), "text": p})
                t = end
    for a, b in zip(out, out[1:]):           # no overlap, no flicker: a cue ends where the next starts if they touch
        if b["start"] < a["end"]:
            a["end"] = b["start"]
    return out


def _ts(s: float, sep: str) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def _wrap(text: str, width: int = 42) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width and len(lines) < 1:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return "\n".join(lines + [cur])


def srt(cs: list[dict]) -> str:
    return "".join(f"{i}\n{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}\n{_wrap(c['text'])}\n\n" for i, c in enumerate(cs, 1))


def vtt(cs: list[dict]) -> str:
    return "WEBVTT\n\n" + "".join(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}\n{_wrap(c['text'])}\n\n" for c in cs)


# --------------------------------------------------------------------------
# Chrome over the DevTools protocol (a minimal WebSocket client, stdlib only)
# --------------------------------------------------------------------------

def chrome_path() -> str | None:
    for p in (os.environ.get("LITREV_CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome")):
        if p and Path(p).exists():
            return p
    return None


class _WS:
    def __init__(self, url: str):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        self.sock = socket.create_connection((m.group(1), int(m.group(2))), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {m.group(3)} HTTP/1.1\r\nHost: {m.group(1)}:{m.group(2)}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"DevTools refused the WebSocket: {head[:200]!r}")
        self.buf = b""

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise RuntimeError("DevTools closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        hdr = bytes([0x81])
        n = len(data)
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> dict:
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if b0 & 0x0F in (0x1, 0x0):
                msg += payload
                if b0 & 0x80:
                    return json.loads(msg)
            elif b0 & 0x0F == 0x9:               # ping
                continue


class Chrome:
    """One headless Chrome with one page; `shot(js)` runs a script that draws a state, then captures the page."""

    def __init__(self):
        exe = chrome_path()
        if not exe:
            raise SystemExit("no Chrome found: install Google Chrome or set LITREV_CHROME")
        self.dir = tempfile.mkdtemp(prefix="litrev-chrome-")
        self.proc = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                                      "--no-default-browser-check", "--mute-audio", "--remote-debugging-port=0",
                                      f"--user-data-dir={self.dir}", f"--window-size={W},{H}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_f = Path(self.dir) / "DevToolsActivePort"
        for _ in range(200):
            if port_f.exists() and port_f.read_text().strip():
                break
            time.sleep(0.05)
        port = port_f.read_text().split()[0]
        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10).read())
        page = next(t for t in targets if t.get("type") == "page")
        self.ws, self.n = _WS(page["webSocketDebuggerUrl"]), 0
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)

    def call(self, method: str, **params) -> dict:
        self.n += 1
        self.ws.send({"id": self.n, "method": method, "params": params})
        while True:
            m = self.ws.recv()
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result") or {}

    def open(self, url: str) -> None:
        self.call("Page.navigate", url=url)
        for _ in range(400):
            r = self.call("Runtime.evaluate", expression="document.readyState === 'complete' && !!window.renderState", returnByValue=True)
            if (r.get("result") or {}).get("value"):
                return
            time.sleep(0.05)
        raise RuntimeError("the scene page did not load")

    def shot(self, js: str, out: Path) -> None:
        expr = ("(async()=>{" + js + "; await document.fonts.ready; "
                "await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); return window.__err||'ok';})()")
        r = self.call("Runtime.evaluate", expression=expr,
                      awaitPromise=True, returnByValue=True)
        v = (r.get("result") or {}).get("value")
        if v != "ok":
            raise RuntimeError(f"scene page error: {v or r}")
        warn = (self.call("Runtime.evaluate", expression="JSON.stringify(window.__warn||[])", returnByValue=True).get("result") or {}).get("value")
        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False,
                         clip={"x": 0, "y": 0, "width": W, "height": H, "scale": 1})["data"]
        out.write_bytes(base64.b64decode(data))
        return json.loads(warn or "[]")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------------
# ffmpeg assembly
# --------------------------------------------------------------------------

VF = ("scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
      "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv")
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-tune", "stillimage", "-g", str(FPS * 10), "-r", str(FPS)]
SR = 24000


def ffmpeg_path() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SystemExit("no ffmpeg on PATH: `brew install ffmpeg`")
    return ff


def _run(args: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} failed: {p.stderr[-1200:]}")
    return p


def encode_still(png: Path, n_f: int, out: Path) -> None:
    _run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
          "-frames:v", str(n_f), "-vf", VF, *X264, str(out)])


def encode_fade(a: Path, b: Path, n_f: int, out: Path) -> None:
    d = n_f / FPS
    _run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(a),
          "-loop", "1", "-framerate", str(FPS), "-i", str(b), "-filter_complex",
          f"[0]settb=AVTB,fps={FPS}[a];[1]settb=AVTB,fps={FPS}[b];[a][b]xfade=transition=fade:duration={d:.4f}:offset=0,{VF}",
          "-frames:v", str(n_f), *X264, str(out)])


def _wav_frames(p: Path) -> bytes:
    with wave.open(str(p), "rb") as w:
        return w.readframes(w.getnframes())


def narration(scene_wavs: list[Path], n_frames: list[int], out_wav: Path) -> float:
    """Every scene's audio padded (or, by under a frame, cut) to its whole number of frames, then joined."""
    pcm = b""
    for p, nf in zip(scene_wavs, n_frames):
        a, want = _wav_frames(p), int(round(nf / FPS * SR)) * 2
        pcm += a[:want] + b"\x00" * max(0, want - len(a))
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm)
    return len(pcm) / 2 / SR


def loudnorm(wav: Path, out_m4a: Path) -> dict:
    """Two-pass linear loudnorm to the spoken-word target, 48 kHz AAC (one pass would resample to 192 kHz)."""
    ff, t = ffmpeg_path(), LOUDNESS
    p = subprocess.run([ff, "-hide_banner", "-nostats", "-i", str(wav), "-af",
                        f"loudnorm=I={t['I']}:TP={t['TP']}:LRA={t['LRA']}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = json.loads(p.stderr[p.stderr.rindex("{"):p.stderr.rindex("}") + 1])
    _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-af",
          f"loudnorm=I={t['I']}:TP={t['TP']}:LRA={t['LRA']}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true",
          "-ar", "48000", "-c:a", "aac", "-b:a", "160k", str(out_m4a)])
    return {"input_i": float(m["input_i"]), "input_tp": float(m["input_tp"]), "target_i": t["I"]}


def probe(p: Path) -> dict:
    fp = shutil.which("ffprobe")
    r = json.loads(_run([fp, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)]).stdout)
    v = next((s for s in r["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in r["streams"] if s["codec_type"] == "audio"), {})
    return {"video_s": float(v.get("duration") or 0), "audio_s": float(a.get("duration") or 0),
            "frames": int(v.get("nb_frames") or 0), "subtitles": any(s["codec_type"] == "subtitle" for s in r["streams"]),
            "size_mb": round(int(r["format"].get("size") or 0) / 1e6, 1)}


def scene_changes(mp4: Path, threshold: float = 0.0008) -> list[float]:
    """Times (s) where the finished video's picture changes, read back from the MP4 itself."""
    p = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostats", "-i", str(mp4), "-an", "-vf",
                        f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], capture_output=True, text=True)
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", p.stderr)]


def render_stills(data: dict, keys: list[tuple[int, int]], work: Path, template: Path) -> dict:
    """One PNG per (scene, reveal state) in work/stills/SSS-KK.png, drawn by one headless Chrome. Returns the layout
    warnings per state."""
    work = work.resolve()
    (work / "stills").mkdir(parents=True, exist_ok=True)
    page = work / "scene.html"
    blob = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    page.write_text(template.read_text().replace("/*__DATA__*/", blob))
    warns = {}
    chrome = Chrome()
    try:
        chrome.open(page.as_uri())
        for si, k in keys:
            w = chrome.shot(f"renderState({si},{k},null)", work / "stills" / f"{si:03d}-{k:02d}.png")
            if w:
                warns[f"{data['scenes'][si]['id']} state {k}"] = w
    finally:
        chrome.close()
    return warns


def build_video(data: dict, scenes: list[dict], work: Path, out_mp4: Path, template: Path, workers: int = 4,
                log=lambda *a: None) -> dict:
    """`data`: the scene page's data (scenes in order, one entry per entry of `scenes`). `scenes`: [{id, dur,
    sentence_starts, reveal, transition, wav, narration, words}]. Writes out_mp4 and its .srt/.vtt beside it."""
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    work.mkdir(parents=True, exist_ok=True)
    (work / "stills").mkdir(exist_ok=True)
    (work / "segs").mkdir(exist_ok=True)
    work, out_mp4 = work.resolve(), out_mp4.resolve()   # `--review .` gave a relative page Chrome cannot open as a file URL
    segs = timeline(scenes)
    keys = sorted({(s["scene"], s["state"]) for s in segs} | {(s["from_scene"], s["from_state"]) for s in segs if s["kind"] == "fade"})
    t0 = time.time()
    warns = render_stills(data, keys, work, template)
    t_stills = time.time() - t0
    still = lambda si, k: work / "stills" / f"{si:03d}-{k:02d}.png"

    def enc(i_s):
        i, s = i_s
        out = work / "segs" / f"{i:04d}.mp4"
        if s["kind"] == "fade":
            encode_fade(still(s["from_scene"], s["from_state"]), still(s["scene"], s["state"]), s["n_f"], out)
        else:
            encode_still(still(s["scene"], s["state"]), s["n_f"], out)
        return out
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        outs = list(ex.map(enc, enumerate(segs)))
    (work / "segs.txt").write_text("".join(f"file '{o}'\n" for o in outs))
    video = work / "video.mp4"
    _run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "segs.txt"),
          "-c", "copy", str(video)])
    t_enc = time.time() - t1
    nfs = [frames(s["dur"]) for s in scenes]
    nar_s = narration([Path(s["wav"]) for s in scenes], nfs, work / "narration.wav")
    loud = loudnorm(work / "narration.wav", work / "narration.m4a")
    offs = [sum(nfs[:i]) for i in range(len(nfs))]
    cs = cues(scenes, offs)
    srt_p, vtt_p = out_mp4.with_suffix(".srt"), out_mp4.with_suffix(".vtt")
    srt_p.write_text(srt(cs))
    vtt_p.write_text(vtt(cs))
    _run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(work / "narration.m4a"),
          "-i", str(srt_p), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
          "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", str(out_mp4)])
    pr = probe(out_mp4)
    # where the picture should change: each still whose image differs from the one before it (fades: at their start)
    h = {k: hashlib.sha1(still(*k).read_bytes()).hexdigest() for k in keys}
    expect, prev = [], None
    for s in segs:
        cur = (s["scene"], s["state"])
        if prev is not None and h[cur] != h[prev]:
            expect.append({"t": s["start_f"] / FPS, "scene": data["scenes"][s["scene"]]["id"], "state": s["state"], "kind": s["kind"]})
        prev = cur
    got = scene_changes(out_mp4)
    tol = 1.5 / FPS
    miss = [e for e in expect if not any(abs(g - e["t"]) <= tol for g in got) and e["kind"] == "still"]
    off = [min((abs(g - e["t"]) for g in got), default=None) for e in expect if e["kind"] == "still"]
    off = [x for x in off if x is not None and x <= tol]
    return {"mp4": str(out_mp4), "srt": str(srt_p), "vtt": str(vtt_p), "stills": len(keys), "segments": len(segs),
            "frames_expected": total_frames(scenes), "probe": pr, "narration_s": round(nar_s, 3), "loudness": loud,
            "cues": len(cs), "render_s": {"stills": round(t_stills, 1), "encode": round(t_enc, 1)},
            "picture_changes": {"expected": len(expect), "found_in_mp4": len(got), "missing": [f"{m['scene']} state {m['state']} at {m['t']:.2f} s" for m in miss][:20],
                                "max_offset_frames": round(max(off) * FPS, 2) if off else None},
            "layout_warnings": warns, "segs": segs}


# --------------------------------------------------------------------------
# PowerPoint deck of the video's slides (hand-written OOXML: stdlib only)
# --------------------------------------------------------------------------
# One slide per scene. Its reveal states are stacked full-slide pictures, state 0 at the bottom; each later state
# appears on a click, so a slide show replays the video's reveals, and the edit view, the slide sorter and a PDF
# show the scene's last state. The speaker notes hold the narration, with "[click]" before the sentence where the
# video shows the next state. The picture data is the video's own stills, so the deck and the MP4 match.

SLIDE_EMU = (12192000, 6858000)           # 16:9, 13.333 x 7.5 in
NOTES_EMU = (6858000, 9144000)
_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_CT = "application/vnd.openxmlformats-officedocument.presentationml."
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_GRP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
        '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')
_CLRMAP = ('bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
           'accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"')
_TITLE_XFRM = '<a:xfrm><a:off x="609600" y="365125"/><a:ext cx="10972800" cy="1325563"/></a:xfrm>'


def _x(s) -> str:
    """Text for XML: escaped, without the control characters XML 1.0 forbids."""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]", "", str(s if s is not None else ""))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _rels(rows: list[tuple[str, str, str]]) -> str:
    return (_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="{i}" Type="{t if t.startswith("http") else _REL + t}" Target="{g}"/>' for i, t, g in rows)
            + "</Relationships>")


def _theme(name: str) -> str:
    c = {"dk1": "1A1A1A", "lt1": "FFFFFF", "dk2": "2D3A4A", "lt2": "F4F4F1", "accent1": "2D5FA6", "accent2": "C2571A",
         "accent3": "3F8F5A", "accent4": "8A5BB0", "accent5": "B8902A", "accent6": "4F9FB8", "hlink": "2D5FA6", "folHlink": "6B4E9B"}
    font = '<a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/>'
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    return (_HEAD + f'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="{_x(name)}"><a:themeElements>'
            f'<a:clrScheme name="Litrev">' + "".join(f'<a:{k}><a:srgbClr val="{v}"/></a:{k}>' for k, v in c.items()) + '</a:clrScheme>'
            f'<a:fontScheme name="Litrev"><a:majorFont>{font}</a:majorFont><a:minorFont>{font}</a:minorFont></a:fontScheme>'
            f'<a:fmtScheme name="Litrev"><a:fillStyleLst>{fill * 3}</a:fillStyleLst><a:lnStyleLst>'
            + f'<a:ln w="6350">{fill}</a:ln>' * 3 + '</a:lnStyleLst><a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3
            + f'</a:effectStyleLst><a:bgFillStyleLst>{fill * 3}</a:bgFillStyleLst></a:fmtScheme></a:themeElements>'
            '<a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


def _title_sp(sid: int, text: str, ph: str = "", hidden: bool = False, xfrm: str = "") -> str:
    body = f'<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{_x(text)}</a:t></a:r></a:p>' if text else '<a:p><a:endParaRPr lang="en-US"/></a:p>'
    hid = ' hidden="1"' if hidden else ""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="Title {sid - 1}"{hid}/><p:cNvSpPr><a:spLocks noGrp="1"/>'
            f'</p:cNvSpPr><p:nvPr><p:ph type="title"{ph}/></p:nvPr></p:nvSpPr><p:spPr>{xfrm}</p:spPr><p:txBody><a:bodyPr/><a:lstStyle/>'
            f'{body}</p:txBody></p:sp>')


def _master() -> str:
    lvl = lambda sz: f'<a:lvl1pPr><a:defRPr sz="{sz}"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr></a:lvl1pPr>'
    return (_HEAD + f'<p:sldMaster {_NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>{_GRP}'
            + _title_sp(2, "", xfrm=_TITLE_XFRM) + f'</p:spTree></p:cSld><p:clrMap {_CLRMAP}/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
            f'<p:txStyles><p:titleStyle>{lvl(4000)}</p:titleStyle><p:bodyStyle>{lvl(2000)}</p:bodyStyle><p:otherStyle>{lvl(1800)}</p:otherStyle>'
            '</p:txStyles></p:sldMaster>')


def _layout() -> str:
    return (_HEAD + f'<p:sldLayout {_NS} type="titleOnly" preserve="1"><p:cSld name="Video still"><p:spTree>{_GRP}'
            + _title_sp(2, "") + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')


def _notes_master() -> str:
    return (_HEAD + f'<p:notesMaster {_NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>{_GRP}'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/>'
            '</p:cNvSpPr><p:nvPr><p:ph type="sldImg" idx="2"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="381000" y="685800"/>'
            '<a:ext cx="6096000" cy="3429000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln w="12700">'
            '<a:solidFill><a:prstClr val="black"/></a:solidFill></a:ln></p:spPr></p:sp>'
            '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>'
            '<p:ph type="body" sz="quarter" idx="3"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="685800" y="4343400"/>'
            '<a:ext cx="5486400" cy="4114800"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr><p:txBody><a:bodyPr/>'
            '<a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>'
            f'</p:spTree></p:cSld><p:clrMap {_CLRMAP}/><p:notesStyle><a:lvl1pPr marL="0" algn="l" rtl="0"><a:defRPr sz="1200">'
            '<a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr></a:lvl1pPr></p:notesStyle>'
            '</p:notesMaster>')


def _notes(paras: list[str]) -> str:
    ps = "".join(f'<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{_x(t)}</a:t></a:r></a:p>' if t else '<a:p><a:endParaRPr lang="en-US"/></a:p>'
                 for t in paras) or '<a:p><a:endParaRPr lang="en-US"/></a:p>'
    return (_HEAD + f'<p:notes {_NS}><p:cSld><p:spTree>{_GRP}'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/>'
            '</p:cNvSpPr><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
            '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>'
            f'<p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>{ps}</p:txBody></p:sp>'
            '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>')


def _timing(spids: list[int]) -> str:
    """A click sequence: each shape in `spids` appears (entrance effect "Appear") on its own click, in order."""
    if not spids:
        return ""
    clicks, n = [], 3
    for sp in spids:
        clicks.append(
            f'<p:par><p:cTn id="{n}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>'
            f'<p:par><p:cTn id="{n + 1}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
            f'<p:par><p:cTn id="{n + 2}" presetID="1" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" nodeType="clickEffect">'
            '<p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst><p:set><p:cBhvr>'
            f'<p:cTn id="{n + 3}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
            f'<p:tgtEl><p:spTgt spid="{sp}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>'
            '</p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set></p:childTnLst></p:cTn></p:par>'
            '</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>')
        n += 4
    return ('<p:timing><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
            '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>' + "".join(clicks) +
            '</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
            '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq>'
            '</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>')


def _pic(sid: int, rid: str, name: str, descr: str) -> str:
    cx, cy = SLIDE_EMU
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="{sid}" name="{_x(name)}" descr="{_x(descr)}"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/>'
            f'</p:cNvPicPr><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
            f'<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '</p:spPr></p:pic>')


def deck_states(sc: dict) -> list[tuple[int, int]]:
    """(reveal state, sentence that starts it) for each state the video shows: state 0 with sentence 0, state k with the
    k-th distinct reveal `at` above 0 (numbered as states_of numbers them), when that sentence exists."""
    n = len(sc.get("sentence_starts") or sc.get("narration") or [0])
    ats = sorted({r["at"] for r in sc.get("reveal") or [] if isinstance(r.get("at"), int) and r["at"] > 0})
    return [(0, 0)] + [(k, a) for k, a in enumerate(ats, 1) if a < n]


def build_pptx(data: dict, scenes: list[dict], still, out: Path, built_at: str = "") -> dict:
    """Write the video's slides as a PowerPoint deck. `data`/`scenes` as build_video takes them; `still(si, k)` is the
    PNG of scene si in reveal state k. Identical consecutive states share one picture, so they need no click."""
    import hashlib
    import uuid
    import zipfile
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"\W+", "-", str(data.get("title") or "deck")).strip("-").lower() or "deck"
    when = (built_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))[:19] + "Z"
    media: dict[str, str] = {}            # sha1 -> part name
    parts: list[tuple[str, bytes]] = []
    sld_ids, sections, t_f, clicks, pics = [], {}, 0, 0, 0
    for si, (d, sc) in enumerate(zip(data["scenes"], scenes)):
        n = si + 1
        ch = data["chapters"][d["ch"]]
        rels, shapes, prev, starts = [("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")], [], None, {}
        kept = [(k, a) for k, a in deck_states(sc) if Path(still(si, k)).exists()]
        for k, a in kept:
            png = Path(still(si, k)).read_bytes()
            h = hashlib.sha1(png).hexdigest()
            if h == prev:
                continue                  # the same picture as the state before: no click
            prev = h
            if h not in media:
                media[h] = f"ppt/media/image{len(media) + 1}.png"
                parts.append((media[h], png))
            rid = f"rId{len(rels) + 1}"
            rels.append((rid, "image", "../media/" + media[h].rsplit("/", 1)[1]))
            sid = 3 + len(shapes)
            lines = "; ".join(d.get("lines") or [])
            descr = f"Video slide {d['id']}, step {len(shapes) + 1}: {ch.get('title') or ''}." + (f" {lines}" if lines else "")
            shapes.append((sid, _pic(sid, rid, f"Step {len(shapes) + 1}", descr)))
            if shapes[1:]:
                starts[a] = True
        if not shapes:
            raise RuntimeError(f"no still for {d['id']}")
        pics += len(shapes)
        clicks += len(shapes) - 1
        title = f"Chapter {d['ch'] + 1}: {ch.get('title') or ''}" if d.get("kind") != "title" else str(data.get("title") or "")
        fade = si > 0 and (sc.get("transition") == "fade" or scenes[si - 1].get("transition_out") == "fade")   # as timeline()
        slide = (_HEAD + f'<p:sld {_NS}><p:cSld name="{_x(d["id"])}"><p:spTree>{_GRP}'
                 + _title_sp(2, title, hidden=True, xfrm=_TITLE_XFRM) + "".join(x for _, x in shapes)
                 + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
                 + ('<p:transition spd="fast"><p:fade/></p:transition>' if fade else "")
                 + _timing([s for s, _ in shapes[1:]]) + "</p:sld>")
        nf = frames(sc["dur"]) if sc.get("dur") else 0
        span = f"video {int(t_f / FPS) // 60:02d}:{int(t_f / FPS) % 60:02d}" + (f"-{int((t_f + nf) / FPS) // 60:02d}:{int((t_f + nf) / FPS) % 60:02d}" if nf else "")
        t_f += nf
        say = [("[click] " if i in starts else "") + str(s) for i, s in enumerate(sc.get("narration") or [])]
        rels.append((f"rId{len(rels) + 1}", "notesSlide", f"../notesSlides/notesSlide{n}.xml"))
        parts += [(f"ppt/slides/slide{n}.xml", slide.encode()), (f"ppt/slides/_rels/slide{n}.xml.rels", _rels(rels).encode()),
                  (f"ppt/notesSlides/notesSlide{n}.xml", _notes([f"{d['id']} · {span}", ""] + say).encode()),
                  (f"ppt/notesSlides/_rels/notesSlide{n}.xml.rels",
                   _rels([("rId1", "notesMaster", "../notesMasters/notesMaster1.xml"), ("rId2", "slide", f"../slides/slide{n}.xml")]).encode())]
        sld_ids.append(255 + n)
        sections.setdefault(d["ch"], []).append(255 + n)
    ns = len(sld_ids)
    pres_rels = [("rId1", "slideMaster", "slideMasters/slideMaster1.xml"), ("rId2", "notesMaster", "notesMasters/notesMaster1.xml"),
                 ("rId3", "theme", "theme/theme1.xml"), ("rId4", "presProps", "presProps.xml"), ("rId5", "viewProps", "viewProps.xml"),
                 ("rId6", "tableStyles", "tableStyles.xml")] + [(f"rId{6 + i}", "slide", f"slides/slide{i}.xml") for i in range(1, ns + 1)]
    sec = "".join(f'<p14:section name="{_x(data["chapters"][c]["id"] + " " + str(data["chapters"][c].get("title") or ""))}" '
                  f'id="{{{str(uuid.uuid5(uuid.NAMESPACE_URL, slug + "/" + data["chapters"][c]["id"])).upper()}}}"><p14:sldIdLst>'
                  + "".join(f'<p14:sldId id="{i}"/>' for i in ids) + "</p14:sldIdLst></p14:section>" for c, ids in sections.items())
    pres = (_HEAD + f'<p:presentation {_NS} saveSubsetFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
            '</p:sldMasterIdLst><p:notesMasterIdLst><p:notesMasterId r:id="rId2"/></p:notesMasterIdLst><p:sldIdLst>'
            + "".join(f'<p:sldId id="{i}" r:id="rId{6 + j}"/>' for j, i in enumerate(sld_ids, 1))
            + f'</p:sldIdLst><p:sldSz cx="{SLIDE_EMU[0]}" cy="{SLIDE_EMU[1]}"/><p:notesSz cx="{NOTES_EMU[0]}" cy="{NOTES_EMU[1]}"/>'
            '<p:extLst><p:ext uri="{521415D9-36F7-43E2-AB2F-B90AF26B5E84}">'
            f'<p14:sectionLst xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">{sec}</p14:sectionLst></p:ext></p:extLst>'
            "</p:presentation>")
    ov = ([("/ppt/presentation.xml", _CT + "presentation.main+xml"), ("/ppt/slideMasters/slideMaster1.xml", _CT + "slideMaster+xml"),
           ("/ppt/slideLayouts/slideLayout1.xml", _CT + "slideLayout+xml"), ("/ppt/notesMasters/notesMaster1.xml", _CT + "notesMaster+xml"),
           ("/ppt/theme/theme1.xml", "application/vnd.openxmlformats-officedocument.theme+xml"),
           ("/ppt/theme/theme2.xml", "application/vnd.openxmlformats-officedocument.theme+xml"),
           ("/ppt/presProps.xml", _CT + "presProps+xml"), ("/ppt/viewProps.xml", _CT + "viewProps+xml"),
           ("/ppt/tableStyles.xml", _CT + "tableStyles+xml"),
           ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
           ("/docProps/app.xml", "application/vnd.openxmlformats-officedocument.extended-properties+xml")]
          + [(f"/ppt/slides/slide{i}.xml", _CT + "slide+xml") for i in range(1, ns + 1)]
          + [(f"/ppt/notesSlides/notesSlide{i}.xml", _CT + "notesSlide+xml") for i in range(1, ns + 1)])
    ctypes = (_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
              + "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in ov) + "</Types>")
    core = (_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{_x(data.get("title"))}</dc:title><dc:subject>{_x(data.get("subtitle"))}</dc:subject><dc:creator>litrev</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{when}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{when}</dcterms:modified>'
            "</cp:coreProperties>")
    app = (_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           f"<Application>litrev</Application><PresentationFormat>Widescreen</PresentationFormat><Slides>{ns}</Slides><Notes>{ns}</Notes>"
           "</Properties>")
    fixed = [("[Content_Types].xml", ctypes),
             ("_rels/.rels", _rels([("rId1", "officeDocument", "ppt/presentation.xml"),
                                    ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
                                    ("rId3", "extended-properties", "docProps/app.xml")])),
             ("docProps/core.xml", core), ("docProps/app.xml", app),
             ("ppt/presentation.xml", pres), ("ppt/_rels/presentation.xml.rels", _rels(pres_rels)),
             ("ppt/presProps.xml", _HEAD + f"<p:presentationPr {_NS}/>"), ("ppt/viewProps.xml", _HEAD + f"<p:viewPr {_NS}/>"),
             ("ppt/tableStyles.xml", _HEAD + '<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                                     'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>'),
             ("ppt/slideMasters/slideMaster1.xml", _master()),
             ("ppt/slideMasters/_rels/slideMaster1.xml.rels", _rels([("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
                                                                    ("rId2", "theme", "../theme/theme1.xml")])),
             ("ppt/slideLayouts/slideLayout1.xml", _layout()),
             ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _rels([("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")])),
             ("ppt/notesMasters/notesMaster1.xml", _notes_master()),
             ("ppt/notesMasters/_rels/notesMaster1.xml.rels", _rels([("rId1", "theme", "../theme/theme2.xml")])),
             ("ppt/theme/theme1.xml", _theme("Litrev")), ("ppt/theme/theme2.xml", _theme("Litrev notes"))]
    stamp = tuple(int(x) for x in re.findall(r"\d+", when)[:6])
    tmp = out.with_suffix(".pptx.part")
    with zipfile.ZipFile(tmp, "w") as z:
        for name, body in [(n_, b.encode()) for n_, b in fixed] + parts:
            zi = zipfile.ZipInfo(name, date_time=stamp)
            zi.compress_type = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            z.writestr(zi, body)
    tmp.replace(out)
    return {"pptx": str(out), "slides": ns, "pictures": pics, "clicks": clicks, "media": len(media),
            "size_mb": round(out.stat().st_size / 1e6, 1)}
