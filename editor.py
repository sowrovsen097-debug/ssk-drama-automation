"""editor.py — SSK DRAMA auto-editing engine (একটাই জায়গায় সব এডিট রুল).

Pass 1  : scene-aware trim → 81.25% keep ratio, mirror, micro speed   -> joined.mp4
Whisper : word timings TRIMMED timeline-এর উপরে মাপা হয়                -> transcript.json
Pass 2  : ২০টি স্টাইল এডিট + রঙিন karaoke ASS + watermark + audio mix -> <platform>-ready.mp4

CLI:  python -m editor transcribe AUDIO.wav OUT.json MODEL
"""
from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import cv2
import requests

import core
from core import CoreError, env
from subtitle_generator import write_ass

FFMPEG = env("FFMPEG_BIN", "ffmpeg")
FFPROBE = env("FFPROBE_BIN", "ffprobe")
WORK = Path(os.environ.get("APP_WORK", "work"))

EDIT_KEYS = ["aspect_fit", "crop_edges", "mirror_segments", "global_flip", "colour_grading",
             "colour_curves", "tone_warmth", "vignette", "zoom_punch", "film_grain",
             "unsharp_clarity", "video_fade", "audio_fade", "denoise", "loudness_normalise",
             "pitch_shift", "micro_speed", "bgm_ducking", "scene_trim", "karaoke_subtitles"]

_FONT = None


# --------------------------------------------------------------------- plumbing
def run(args, timeout: int = 7200, check: bool = True, cwd=None) -> str:
    limit = float(env("APP_DEADLINE", "0") or 0)
    if limit and time.time() > limit - 90:
        raise TimeoutError("রাতের এডিটিং উইন্ডো শেষ — কাজ নিরাপদে থামানো হলো")
    proc = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)
    if check and proc.returncode != 0:
        raise CoreError(f"{Path(str(args[0])).name} ব্যর্থ: {(proc.stderr or proc.stdout or '')[-500:]}")
    return proc.stdout


def font_file() -> str:
    global _FONT
    if _FONT is not None:
        return _FONT
    # watermark লেখা ইংরেজিতে (SSK DRAMA) — তাই আগে পূর্ণ ল্যাটিন ফন্ট, পরে বাংলা
    for name in ("DejaVu Sans:style=Bold", "Noto Sans:style=Bold",
                 "Noto Sans Bengali:style=Bold", "DejaVu Sans"):
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", name],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            if out and Path(out).exists():
                _FONT = out
                return out
        except Exception:
            continue
    for guess in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(guess).exists():
            _FONT = guess
            return guess
    _FONT = ""        # কিছুই নেই → fontconfig-কে নিজে বেছে নিতে দিন
    return _FONT


def probe(path) -> dict:
    raw = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
               str(path)], timeout=300)
    data = json.loads(raw or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {"duration": float(data.get("format", {}).get("duration") or 0),
            "size": int(data.get("format", {}).get("size") or 0),
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "has_audio": bool(audio), "audio_sr": int(audio.get("sample_rate") or 0)}


# ------------------------------------------------------------------ analysis
def scenes(path: str, threshold: int = 28, min_gap: float = 8.0) -> list:
    """OpenCV frame-difference cut detection — ২ সেকেন্ডে ১ ফ্রেম পড়ে (হালকা)।"""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(fps * 2))
    previous, index, cuts, last = None, 0, [], -99.0
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
        if previous is not None and index / fps - last >= min_gap:
            if float(cv2.absdiff(small, previous).mean()) > threshold:
                cuts.append(round(index / fps, 3))
                last = index / fps
        previous = small
        index += step
    cap.release()
    return cuts


def silence_ranges(path: str, noise: str = "-35dB", dur: float = 1.2) -> list:
    proc = subprocess.run([FFMPEG, "-i", str(path), "-af",
                           f"silencedetect=noise={noise}:d={dur}", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=3600)
    starts, ranges = [], []
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].split()[0]))
            except (IndexError, ValueError):
                pass
        elif "silence_end:" in line and starts:
            try:
                ranges.append((starts.pop(), float(line.split("silence_end:")[1].split("|")[0].strip())))
            except (IndexError, ValueError):
                pass
    for left in starts:
        ranges.append((left, left + 2.0))
    return ranges


def speech_ratio(a: float, b: float, silences: list) -> float:
    total = max(0.01, b - a)
    quiet = sum(max(0.0, min(b, e) - max(a, s)) for s, e in silences)
    return max(0.0, 1.0 - quiet / total)


def build_units(duration: float, cuts: list, silences: list, piece: float = 30.0) -> list:
    """কাট পয়েন্ট অনুযায়ী ইউনিট; ৪৫ সেকেন্ডের বেশি হলে টুকরো করা হয়।"""
    edges = [0.0] + [c for c in cuts if 8 <= c <= duration - 8] + [duration]
    units, uid = [], 0
    for a, b in zip(edges, edges[1:]):
        span = b - a
        parts = max(1, int(round(span / piece))) if span > 45 else 1
        for step in range(parts):
            sa, sb = a + span * step / parts, a + span * (step + 1) / parts
            if sb - sa < 3:
                continue
            units.append({"id": uid, "start": round(sa, 3), "end": round(sb, 3),
                          "speech": round(speech_ratio(sa, sb, silences), 3),
                          "span": round(sb - sa, 3)})
            uid += 1
    return units


def sample_frames(path: str, at: list, folder: Path) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    blobs = []
    for position, moment in enumerate(at):
        target = folder / f"frame-{position}.jpg"
        subprocess.run([FFMPEG, "-y", "-ss", f"{max(0.0, moment):.3f}", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=480:-2", str(target)],
                       capture_output=True, timeout=300)
        if target.exists():
            blobs.append(base64.b64encode(target.read_bytes()).decode("ascii"))
    return blobs


def ai_review(path: str, units: list, cfg: dict) -> tuple:
    """Gemini-কে শুধু ফাঁকা/নির্দোষ অংশ কাটার অনুমতি — সন্দেহ হলে 'না'।"""
    count = int(cfg.get("ai_scene_frames", 6))
    fractions = [0.08, 0.25, 0.42, 0.58, 0.74, 0.86][:max(3, min(6, count))]
    approved, notes = [], []
    for unit in units[:6]:
        moments = [unit["start"] + (unit["end"] - unit["start"]) * f for f in fractions]
        blobs = sample_frames(path, moments, WORK / "frames")
        if len(blobs) < 3:
            notes.append("AI review skipped: frames unreadable")
            break
        prompt = ("These frames come from ONE continuous ~%d second segment of a Hindi crime "
                  "drama episode. Answer strict JSON only: "
                  '{"removable": true} only if the WHOLE segment is clearly inessential filler '
                  "(black screen, title card, credits, silent empty room, freeze frame) and "
                  "removing it loses no story; otherwise {\"removable\": false}. "
                  "If unsure answer false." % int(unit["end"] - unit["start"]))
        try:
            reply = core.json_from(core.gemini(prompt, images=blobs, model=cfg.get("gemini_model"),
                                               max_tokens=60), default={})
        except CoreError as exc:
            notes.append(f"AI review unavailable: {type(exc).__name__}")
            break
        if str(reply.get("removable", "")).lower() in ("true", "1", "yes"):
            approved.append(unit["id"])
    return approved, notes


def merge_ranges(ranges: list) -> list:
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 0.05:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def complement(drops: list, duration: float) -> list:
    keep, cursor = [], 0.0
    for a, b in merge_ranges(drops):
        if a - cursor > 0.5:
            keep.append((round(cursor, 3), round(a, 3)))
        cursor = max(cursor, b)
    if duration - cursor > 0.5:
        keep.append((round(cursor, 3), round(duration, 3)))
    return keep or [(0.0, round(duration, 3))]


def choose_intervals(duration: float, units: list, cfg: dict, approved=(), silences=()) -> tuple:
    """৮১.২৫% রাখা — আগে নীরব অংশ (গল্প থাকে), পরে প্রয়োজনে নীরব-প্রধান ইউনিট।"""
    keep_ratio = float(cfg.get("keep_ratio", 0.8125))
    budget = max(0.0, duration * (1.0 - keep_ratio))
    margin = min(20.0, duration * 0.10)      # শুরু ও শেষ সুরক্ষিত
    approved = set(approved)

    candidates = []
    for a, b in (silences or []):
        a2, b2 = a + 0.35, b - 0.35
        if b2 - a2 >= 1.0 and a2 >= margin and b2 <= duration - margin:
            candidates.append((a2, b2))
    for unit in (units[1:-1] if len(units) > 2 else []):
        if unit["speech"] < 0.45 or unit["id"] in approved:
            candidates.append((unit["start"] + 0.2, unit["end"] - 0.2))
    candidates = [(a, b) for a, b in candidates if b - a >= 0.8]
    candidates.sort(key=lambda c: c[1] - c[0], reverse=True)

    drops, spent = [], 0.0
    for a, b in candidates:
        if spent >= budget - 0.5:
            break
        take = min(b - a, budget - spent)
        middle = (a + b) / 2
        drops.append((max(0.0, middle - take / 2), min(duration, middle + take / 2)))
        spent += take
    intervals = complement(drops, duration)
    report = {"original_seconds": round(duration, 2),
              "edited_seconds": round(sum(b - a for a, b in intervals), 2),
              "removed_parts": len(merge_ranges(drops)), "units": len(units),
              "budget_seconds": round(budget, 2), "keep_ratio": keep_ratio,
              "silence_cuts": len(merge_ranges([c for c in candidates if c[1] - c[0] >= 1.0]))}
    report["actual_ratio"] = round(report["edited_seconds"] / max(1.0, duration), 4)
    report["target_met"] = 0.75 <= report["actual_ratio"] <= 0.875
    if not report["target_met"]:
        report["note"] = "এই ভিডিওতে কাটার মতো নীরব অংশ কম ছিল — কিছুটা বেশি রাখা হলো"
    return intervals, report


# --------------------------------------------------------------------- music
def music(cfg: dict, folder: Path) -> str | None:
    """Freesound থেকে CC0 (পাবলিক ডোমেইন) ট্র্যাক — FREESOUND_API_KEY দিয়ে।"""
    if not cfg.get("music_enabled", True):
        return None
    key = env("FREESOUND_API_KEY")
    if not key:
        return None
    res = requests.get("https://freesound.org/apiv2/search/text/", params={
        "query": cfg.get("music_query") or "cinematic ambient instrumental",
        "filter": 'license:"Creative Commons 0" duration:[20 TO 600]',
        "fields": "id,name,previews,duration", "page_size": 30, "sort": "score",
        "token": key}, timeout=60)
    if res.status_code != 200:
        raise CoreError(f"Freesound {res.status_code}: {res.text[:160]}")
    results = res.json().get("results") or []
    if not results:
        return None
    url = (random.choice(results[:10]).get("previews") or {}).get("preview-hq-mp3")
    if not url:
        return None
    target = folder / "music.mp3"
    target.write_bytes(requests.get(url, timeout=180).content)
    return str(target)


# -------------------------------------------------------------------- whisper
def transcribe_cli(audio: str, out: str, model_name: str) -> str:
    import whisper
    model = whisper.load_model(model_name)
    result = model.transcribe(audio, word_timestamps=True, fp16=False, verbose=False)
    segments = [{"start": s.get("start"), "end": s.get("end"), "text": s.get("text"),
                 "words": [{"word": w.get("word"), "start": w.get("start"), "end": w.get("end")}
                           for w in (s.get("words") or [])]}
                for s in result.get("segments", [])]
    Path(out).write_text(json.dumps({"segments": segments, "language": result.get("language")},
                                    ensure_ascii=False), encoding="utf-8")
    return out


def is_transcribe_cli() -> bool:
    return len(sys.argv) >= 5 and sys.argv[1] == "transcribe"


# --------------------------------------------------------------------- render
def _audio_chain(cfg: dict, duration: float, edits: dict) -> list:
    bits = []
    if edits.get("denoise"):
        bits.append("afftdn=nf=-25")
    if edits.get("pitch_shift"):
        ratio = float(cfg.get("pitch_ratio", 1.022))
        bits += [f"asetrate={int(48000 * ratio)}", "aresample=48000", f"atempo={1 / ratio:.6f}"]
    if edits.get("loudness_normalise"):
        bits.append("loudnorm=I=-15:TP=-1.5:LRA=11")
    if edits.get("audio_fade"):
        bits += ["afade=t=in:st=0:d=1.0", f"afade=t=out:st={max(0.0, duration - 1.6):.2f}:d=1.6"]
    return bits or ["anull"]


def render(joined: str, folder: Path, platform: str, cfg: dict, info: dict) -> dict:
    """Pass 2 — ২০টি এডিট, সাবটাইটেল, watermark, অডিও মিক্স, সাইজ ক্যাপ।"""
    square = platform == "facebook"
    width, height = (1080, 1080) if square else (1920, 1080)
    duration = float(info["duration"])
    if not info.get("has_audio"):
        raise CoreError("সোর্স ভিডিওতে অডিও নেই — সাবটাইটেল বসানো যাবে না")
    edits = cfg["edits"]
    applied = [k for k in EDIT_KEYS if edits.get(k)]
    notes = []

    # ---- ১) রঙিন word-by-word সাবটাইটেল (ট্রিম করা টাইমলাইনের উপরে) ----
    sub_note = {}
    if edits.get("karaoke_subtitles"):
        audio = folder / "audio.wav"
        run([FFMPEG, "-y", "-i", joined, "-vn", "-ac", "1", "-ar", "16000", str(audio)], timeout=3600)
        words_json = folder / "transcript.json"
        if not words_json.exists():
            run([sys.executable, "-m", "editor", "transcribe", str(audio), str(words_json),
                 str(cfg.get("whisper_model", "base"))], timeout=7200)
        data = json.loads(words_json.read_text(encoding="utf-8"))
        words = []
        for segment in data.get("segments", []):
            for word in segment.get("words") or []:
                text = str(word.get("word") or "").strip()
                if text:
                    words.append({"text": text, "start": float(word.get("start") or 0.0),
                                  "end": float(word.get("end") or 0.0)})
        if len(words) < 3:
            raise CoreError("Whisper কোনো কথা ধরতে পারেনি — সাবটাইটেল ফাঁকা হতো")
        write_ass(words, str(folder / "captions.ass"), width, height)
        sub_note = {"words": len(words), "language": data.get("language")}

    # ---- ২) ভিডিও ফিল্টার চেইন ----
    f, cur, step = [], "base", 0

    def chain(expr: str):
        nonlocal cur, step
        step += 1
        f.append(f"[{cur}]{expr}[v{step}]")
        cur = f"v{step}"

    if square and edits.get("aspect_fit", True):
        fit = int(height * float(cfg.get("foreground_height_ratio", 0.8125))) // 2 * 2
        f.append("[0:v]split=2[bg][fg]")
        f.append(f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{height},boxblur=22:2,eq=brightness=-0.07[bgv]")
        f.append(f"[fg]scale={width}:{fit}:force_original_aspect_ratio=decrease[fgv]")
        f.append("[bgv][fgv]overlay=(W-w)/2:(H-h)/2[base]")
        notes.append(f"১:১ blurred-background, মূল ছবি height {fit}px ({fit / height:.4f})")
    else:
        f.append(f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{height},setsar=1,fps=25[base]")

    if edits.get("crop_edges"):
        chain("crop=iw*0.96:ih*0.96")
    # মিরর (hflip) পাস-১ এ সম্পন্ন — এখানে আর করা হয় না, নইলে উল্টো হয়ে যেত
    if edits.get("zoom_punch"):
        chain("crop=iw*0.98:ih*0.98:x='(iw-ow)/2+sin(t/6)*iw*0.012':"
              "y='(ih-oh)/2+cos(t/9)*ih*0.012'")
    if edits.get("colour_grading"):
        chain("eq=contrast=1.06:saturation=1.10:brightness=0.01")
    if edits.get("colour_curves"):
        chain("curves=preset=medium_contrast")
    if edits.get("tone_warmth"):
        chain("colorbalance=rs=.03:gs=.01:bs=-.03")
    if edits.get("unsharp_clarity"):
        chain("unsharp=5:5:0.8:3:3:0.4")
    if edits.get("vignette"):
        chain("vignette=PI/5")
    if edits.get("film_grain"):
        chain("noise=alls=5:allf=t")
    if edits.get("video_fade"):
        chain(f"fade=t=in:st=0:d=1.0,fade=t=out:st={max(0.0, duration - 1.6):.2f}:d=1.6")
    if edits.get("karaoke_subtitles"):
        chain("subtitles=captions.ass")
    if edits.get("watermark"):
        size = max(20, int(height * 0.028))
        face = font_file()
        prefix = f"fontfile={face}:" if face else ""
        chain("drawtext=" + prefix + "text='SSK DRAMA':fontcolor=white@0.85:fontsize=%d"
              ":x=w-tw-28:y=28:box=1:boxcolor=black@0.25:boxborderw=10" % size)
    # সব কাট/জুমের পরে ঠিক মাপে ফেরানো — আউটপুট সবসময় অভিন্ন
    f.append(f"[{cur}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,format=yuv420p[outv]")

    # ---- ৩) অডিও ----
    f.append("[0:a]" + ",".join(_audio_chain(cfg, duration, edits)) + "[voice]")
    bgm = music(cfg, folder) if edits.get("bgm_ducking") else None
    if bgm:
        f.append("[voice]asplit=2[vmain][vkey]")
        f.append(f"[1:a]atrim=0:{duration:.3f},volume=0.12[bgm]")
        f.append("[bgm][vkey]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[mduck]")
        f.append("[vmain][mduck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[outa]")
    else:
        f.append("[voice]alimiter=limit=0.95[outa]")

    cap_mb = float((cfg.get("size_cap_mb") or {}).get(platform, 1800))
    cap_kbps = max(420.0, min(7000.0, cap_mb * 1024 * 1024 * 8 * 0.92 / duration / 1000 - 165))
    output = folder / f"{platform}-ready.mp4"
    args = [FFMPEG, "-y", "-i", joined]
    if bgm:
        args += ["-stream_loop", "-1", "-i", bgm]
    args += ["-filter_complex", ";".join(f), "-map", "[outv]", "-map", "[outa]",
             "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{int(cap_kbps)}k",
             "-maxrate", f"{int(cap_kbps * 1.3)}k", "-bufsize", f"{int(cap_kbps * 2)}k",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
             "-movflags", "+faststart", output.name]
    run(args, timeout=10800, cwd=str(folder))

    out = probe(output)
    if (out["width"], out["height"]) != (width, height):
        raise CoreError(f"আউটপুট রেজোলিউশন ভুল: {out['width']}x{out['height']}")
    if abs(out["duration"] - duration) > max(3.0, duration * 0.02):
        raise CoreError(f"দৈর্ঘ্য মিলছে না: {out['duration']:.1f}s বনাম {duration:.1f}s")
    if out["size"] > cap_mb * 1024 * 1024 * 1.05:
        raise CoreError(f"ফাইল বেশি বড়: {out['size'] / 1048576:.0f} MB (সীমা {cap_mb:.0f} MB)")
    try:
        Path(joined).unlink(missing_ok=True)      # ফাইনাল ফাইল তৈরি — মাঝের ফাইল মুছে ফেলা
    except OSError:
        pass
    return {"file": str(output), "width": out["width"], "height": out["height"],
            "size_bytes": out["size"], "duration": round(out["duration"], 2),
            "bitrate_kbps": int(cap_kbps), "edits_applied": applied,
            "edits_count": len(applied), "subtitles": sub_note, "notes": notes}


# ------------------------------------------------------------------- pipeline
def edit_video(source: str, platform: str, cfg: dict, folder: Path) -> dict:
    """সম্পূর্ণ এডিটিং পাইপলাইন — ইনপুট সোর্স, আউটপুট প্ল্যাটফর্ম-রেডি ফাইল।"""
    folder.mkdir(parents=True, exist_ok=True)
    info = probe(source)
    cap_seconds = float(cfg.get("max_duration_minutes", 80)) * 60
    if info["duration"] < float(cfg.get("min_duration_seconds", 60)):
        raise CoreError("সোর্স ভিডিও খুব ছোট (< min_duration_seconds)")
    if info["duration"] > cap_seconds + 5:
        raise CoreError(f"সোর্স ভিডিও {cap_seconds / 60:.0f} মিনিটের বেশি — নেওয়া হবে না")
    edits = cfg.get("edits", {})
    units, report, intervals = [], {}, [(0.0, info["duration"])]
    if edits.get("scene_trim"):
        cuts = scenes(source)
        silences = silence_ranges(source)
        units = build_units(info["duration"], cuts, silences)
        approved, review_notes = ([], [])
        if cfg.get("ai_scene_review"):
            weakest = sorted([u for u in units[1:-1] if u["speech"] < 0.2],
                             key=lambda u: u["speech"])[:6]
            if weakest:
                approved, review_notes = ai_review(source, weakest, cfg)
        intervals, report = choose_intervals(info["duration"], units, cfg, approved, silences)
        report["ai_approved"] = len(approved)
        report["ai_notes"] = review_notes
        report["scene_cuts"] = len(cuts)
        report["silences"] = len(silences)
    else:
        report = {"original_seconds": round(info["duration"], 2),
                  "edited_seconds": round(info["duration"], 2), "actual_ratio": 1.0,
                  "target_met": False, "keep_ratio": 1.0, "removed_units": 0, "units": 0}

    speed = float(cfg.get("micro_speed", 1.03)) if edits.get("micro_speed") else 1.0
    flip = bool(edits.get("mirror_segments") or edits.get("global_flip"))
    # পুরো ভিডিও একবারে মিরর — মাঝপথে উল্টে যাওয়ার অস্বস্তি হয় না, গল্প বোঝাও যায়
    mirror_ids = set(range(len(intervals))) if flip else set()

    parts_v, parts_a, concat = [], [], []
    for index, (a, b) in enumerate(intervals):
        v = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        if index in mirror_ids:
            v += ",hflip"
        if speed != 1.0:
            v += f",setpts=PTS/{speed:.4f}"
        parts_v.append(v + f"[pv{index}]")
        acc = f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
        if speed != 1.0:
            acc += f",atempo={speed:.4f}"
        parts_a.append(acc + f"[pa{index}]")
        concat.append(f"[pv{index}][pa{index}]")
    joined = folder / "joined.mp4"
    graph = ";".join(parts_v + parts_a +
                     ["".join(concat) + f"concat=n={len(intervals)}:v=1:a=1[cv][ca]"])
    run([FFMPEG, "-y", "-i", source, "-filter_complex", graph, "-map", "[cv]", "-map", "[ca]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", joined.name], timeout=10800, cwd=str(folder))
    joined_info = probe(joined)
    # পাস-১ শেষ — মূল সোর্স আর দরকার নেই, ফেলে দিয়ে ডিস্ক বাঁচানো হয়
    try:
        if Path(source).resolve() != Path(joined).resolve():
            Path(source).unlink(missing_ok=True)
    except OSError as exc:
        core.log(f"সোর্স ফাইল মোছা যায়নি: {exc}", "warn")
    if speed != 1.0:
        report["edited_seconds"] = round(joined_info["duration"], 2)
        report["actual_ratio"] = round(joined_info["duration"] / max(1.0, info["duration"]), 4)
        report["target_met"] = 0.75 <= report["actual_ratio"] <= 0.875
    result = render(str(joined), folder, platform, cfg, joined_info)
    result["report"] = report
    result["pieces"] = len(intervals)
    result["mirrored_pieces"] = len(mirror_ids)
    return result


if __name__ == "__main__":
    if is_transcribe_cli():
        transcribe_cli(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print("usage: python -m editor transcribe AUDIO OUT MODEL")
