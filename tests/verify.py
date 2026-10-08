"""tests/verify.py — SSK DRAMA app-এর বাস্তব পরীক্ষা (sandbox-এ চালানো হয়)।

যা পরীক্ষা করা হয়: config যাচাই, 81.25% ট্রিম ম্যাথ, সাবটাইটেল রাইটার,
FFmpeg দিয়ে **সত্যিকারের** দুটি রেন্ড (16:9 + 1:1), ফাইল মাপ/দৈর্ঘ্য,
আর secret না থাকলে Drive/Telegram সৎভাবে ব্যর্থ হয় কি না।
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = ROOT / "tests" / "render"
os.environ["APP_WORK"] = str(WORK)
os.environ["APP_DEADLINE"] = str(time.time() + 3600)
os.environ.pop("GOOGLE_REFRESH_TOKEN", None)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.chdir(WORK)

import core                      # noqa: E402
import editor                    # noqa: E402
import subtitle_generator        # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""), flush=True)


def make_source(path: Path, seconds: int = 14):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=420:sample_rate=48000", "-t", str(seconds),
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   capture_output=True, check=True)


def fake_transcript(folder: Path, seconds: float = 14.0):
    """Whisper ডাউনলোড এড়াতে হাতে বানানো word timing (রেন্ডার পথ পরীক্ষার জন্য)।"""
    words, t = [], 0.4
    words_list = ["This", "is", "a", "colourful", "SSK", "DRAMA", "studio", "test", "clip", "now"]
    index = 0
    while t < seconds - 1:
        words.append({"start": round(t, 3), "end": round(t + 0.5, 3),
                      "word": words_list[index % len(words_list)]})
        index += 1
        t += 0.75
    (folder / "transcript.json").write_text(json.dumps(
        {"segments": [{"start": 0.0, "end": seconds, "text": "test",
                       "words": [{"start": w["start"], "end": w["end"], "word": " " + w["word"]}
                                 for w in words]}], "language": "en"}), encoding="utf-8")


def main() -> int:
    cfg_source = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    check("config.json যাচাই", not core.validate_config(cfg_source), str(core.validate_config(cfg_source)))

    bad = dict(cfg_source)
    bad["facebook_times_bd"] = ["02:00", "15:00", "20:00"]
    check("ভুল সময় ধরা পড়ে (01:00–06:00 নিষিদ্ধ)", bool(core.validate_config(bad)))

    # ---- 81.25% ট্রিম ম্যাথ ------------------------------------------------
    silences = [(12.0, 22.0), (40.0, 55.0), (70.0, 82.0)]
    units = editor.build_units(100.0, [22.0, 33.0, 55.0, 66.0], silences)
    intervals, report = editor.choose_intervals(100.0, units, cfg_source, approved=[])
    check("ট্রিম অনুপাত ০.৭৫–০.৮৭৫ ভেতরে",
          0.75 <= report["actual_ratio"] <= 0.875,
          f"ratio={report['actual_ratio']} edited={report['edited_seconds']}s "
          f"removed_parts={report['removed_parts']}/{report['units']} silence_cuts={report.get('silence_cuts')}")
    check("টার্গেট মেট ফ্ল্যাগ", report["target_met"] is True, str(report.get("note", "")))

    # ---- সাবটাইটেল রাইটার -----------------------------------------------
    ass = Work = WORK / "ass-check.ass"
    words = [{"text": "Hello", "start": 0.2, "end": 0.7}, {"text": "colourful", "start": 0.8, "end": 1.4},
             {"text": "studio", "start": 1.5, "end": 2.1}]
    subtitle_generator.write_ass(words, str(ass), 1280, 720)
    text = ass.read_text(encoding="utf-8")
    check("ASS-এ রঙিন karaoke ট্যাগ", "\\kf" in text and "\\1c&H" in text and "PlayResX: 1280" in text)

    # ---- সত্যিকারের রেন্ড (দুই প্ল্যাটফর্ম) -----------------------------
    test_cfg = json.loads(json.dumps(cfg_source))
    test_cfg.update({"min_duration_seconds": 5, "max_duration_minutes": 80,
                     "size_cap_mb": {"youtube": 24, "facebook": 24},
                     "music_enabled": False, "ai_scene_review": False})
    test_cfg["edits"]["bgm_ducking"] = False
    test_cfg["edits"]["scene_trim"] = True

    for platform, expect in (("youtube", (1920, 1080)), ("facebook", (1080, 1080))):
        src = WORK / f"{platform}-source.mp4"
        make_source(src, 14)
        folder = WORK / platform
        folder.mkdir(parents=True, exist_ok=True)
        fake_transcript(folder)
        result = editor.edit_video(str(src), platform, test_cfg, folder)
        info = editor.probe(result["file"])
        check(f"{platform}: রেজোলিউশন {expect[0]}x{expect[1]}",
              (info["width"], info["height"]) == expect, f"{info['width']}x{info['height']}")
        check(f"{platform}: অডিও আছে", info.get("has_audio") is True)
        check(f"{platform}: দৈর্ঘ্য ঠিক আছে",
              abs(info["duration"] - result["duration"]) < 0.2,
              f"{info['duration']:.2f}s")
        check(f"{platform}: সাইজ সীমার ভেতরে",
              info["size"] <= 24 * 1024 * 1024, f"{info['size'] / 1048576:.1f} MB")
        check(f"{platform}: এডিট সংখ্যা >= ১৫", result["edits_count"] >= 15,
              f"{result['edits_count']} → {', '.join(result['edits_applied'][:6])}…")
        check(f"{platform}: সাবটাইটেল বসেছে", bool(result["subtitles"]), str(result["subtitles"]))
        check(f"{platform}: ট্রিম রিপোর্ট আছে", "actual_ratio" in result["report"],
              f"ratio={result['report'].get('actual_ratio')}, pieces={result['pieces']}")
        if platform == "facebook":
            note = " ".join(result["notes"])
            check("facebook: blurred-background + ০.৮১ height-fit লেআউট",
                  "blurred-background" in note and "0.81" in note, note)

    # ---- secret ছাড়া সৎভাবে ব্যর্থ হওয়া -------------------------------
    try:
        core.drive_token()
        check("Drive: refresh token ছাড়া ব্যর্থ হয়", False, "লুকানো সাফল্য!")
    except core.CoreError as exc:
        check("Drive: refresh token ছাড়া পরিষ্কার বার্তা",
              "GOOGLE_REFRESH_TOKEN" in str(exc), str(exc)[:80])
    check("Telegram: token ছাড়া ক্র্যাশ করে না", core.telegram("পরীক্ষা") is None)
    check("Gemini: key ছাড়া সৎ ব্যর্থতা", _check_gemini())
    check("Drive লিংক থেকে file id পড়া যায়",
          __import__("main").drive_id_from_url(
              "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view") == "1AbCdEfGhIjKlMnOpQrStUvWx")

    passed = sum(1 for r in RESULTS if r)
    print(f"\n{passed}/{len(RESULTS)} পরীক্ষা পাস", flush=True)
    print(f"work dir: {WORK}", flush=True)
    return 0 if passed == len(RESULTS) else 1


def _check_gemini() -> bool:
    key = os.environ.pop("GEMINI_API_KEY", None)
    try:
        core.gemini("test")
        return False
    except core.CoreError:
        return True
    finally:
        if key:
            os.environ["GEMINI_API_KEY"] = key


if __name__ == "__main__":
    sys.exit(main())
