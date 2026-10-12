"""SSK DRAMA — লোকাল টেস্ট: সিন্থেটিক সোর্স দিয়ে পূর্ণ এডিট পাইপলাইন + নিয়ম যাচাই।"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_WORK", "/tmp/ssk/proj/work")

import core  # noqa: E402
import media  # noqa: E402

RENDER = Path("/tmp/ssk/proj/tests/render")
PASS, FAIL = [], []


def check(name, ok, info=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + " | " + name + " | " + str(info)[:300], flush=True)


def synth_source(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    parts = []
    patterns = ["testsrc2=size=640x360:rate=25", "smptebars=size=640x360:rate=25",
                "rgbtestsrc=size=640x360:rate=25", "testsrc=size=640x360:rate=25",
                "mandelbrot=size=640x360:rate=25", "life=size=640x360:rate=25",
                "cellauto=size=640x360:rate=25", "gradients=size=640x360:rate=25"]
    for i, pat in enumerate(patterns):
        p = folder / f"seg-{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", pat,
                        "-f", "lavfi", "-i", "sine=frequency=%d:duration=5" % (300 + i * 60),
                        "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(p)], check=True, capture_output=True)
        parts.append(p)
    lst = folder / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    src = folder / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-c", "copy", str(src)], cwd=str(folder), check=True, capture_output=True)
    return src


def main():
    if RENDER.exists():
        shutil.rmtree(RENDER, ignore_errors=True)
    RENDER.mkdir(parents=True)
    cfg = json.loads(Path("/tmp/ssk/proj/config.json").read_text(encoding="utf-8"))

    # ১. ৬ ঘণ্টার নিয়ম
    ok, note = core.lead_check(cfg, core.bd_hm())
    check("৬ ঘণ্টার নিয়ম: এখনই দিলে আটকায়", not ok, note)
    later = (core.bd_now().replace(minute=0, second=0, microsecond=0))
    far = (later.hour + 8) % 24
    ok2, note2 = core.lead_check(cfg, f"{far:02d}:00")
    check("৬ ঘণ্টার নিয়ম: ৮ ঘণ্টা পরের সময় গ্রহণ", ok2, note2)
    ok3, note3 = core.lead_check(cfg, "25:00")
    check("ভুল সময়ের ফরম্যাট ধরা পড়ে", not ok3, note3)

    # ২. এডিট উইন্ডো
    win = cfg["edit_window_bd"]
    check("এডিট উইন্ডো ০১:০০–০৬:০০", win == ["01:00", "06:00"], win)
    check("উইন্ডোর বাইরের সময় বাদ পড়ে",
          not core.in_window(cfg, core.bd_now().replace(hour=12, minute=0)),
          "12:00 BD = উইন্ডোর বাইরে")

    # ৩. সোর্স তৈরি
    src = synth_source(RENDER / "src")
    info = media.probe(src)
    check("সিন্থেটিক সোর্স তৈরি", info["duration"] > 39 and info["has_audio"],
          f"{info['width']}x{info['height']} {info['duration']:.1f}s audio={info['has_audio']}")

    # ৪. scene trim
    t0 = time.time()
    cuts = media.scene_cuts(src)
    keeps = media.choose_keeps(info["duration"], cuts, cfg["keep_ratio"])
    joined = media.trim_video(src, keeps, RENDER)
    after = media.probe(joined)
    ratio = after["duration"] / info["duration"]
    check("scene কাট সনাক্ত", len(cuts) >= 3, f"cuts={cuts[:6]}")
    check("৮১.২৫% অনুপাতে ট্রিম", abs(ratio - cfg["keep_ratio"]) < 0.16,
          f"ratio={ratio:.3f} pieces={len(keeps)} {info['duration']:.1f}s→{after['duration']:.1f}s ({time.time() - t0:.0f}s)")

    # ৫. সাবটাইটেল (Whisper বাদে প্রি-কম্পিউটেড ট্রান্সক্রিপ্ট)
    words = []
    t = 1.0
    for i in range(40):
        words.append({"start": round(t, 2), "end": round(t + 0.45, 2), "text": ["SSK", "DRAMA", "রঙিন", "সাবটাইটেল", "পরীক্ষা"][i % 5]})
        t += 0.55
    groups = media.write_ass(words, RENDER / "captions.ass", 1920, 1080)
    ass_text = (RENDER / "captions.ass").read_text(encoding="utf-8")
    check("ASS সাবটাইটেল লেখা", groups >= 5 and "\\kf" in ass_text and "\\1c&H" in ass_text,
          f"groups={groups} karaoke={ass_text.count(chr(92) + 'kf')} colors={ass_text.count(chr(92) + '1c')}")
    check("উল্লম্ব ভিডিওর জন্য আলাদা ফন্ট সাইজ", media.write_ass(words, RENDER / "vertical.ass", 1080, 1080) >= 5
          and "Fontsize,48" not in (RENDER / "vertical.ass").read_text(encoding="utf-8"),
          "1080x1080 লেখা হয়েছে")

    # ৬. YouTube রেন্ডার
    out = RENDER / "youtube-ready.mp4"
    rep = media.render(joined, out, "youtube", cfg, words)
    check("YouTube রেন্ডার 1920x1080", (rep["width"], rep["height"]) == (1920, 1080),
          f"{rep['width']}x{rep['height']} {rep['duration']}s {rep['size_bytes'] / 1048576:.1f}MB")
    check("রেন্ডারে এডিট প্রয়োগ", len(rep["edits"]) >= 16,
          f"{len(rep['edits'])}টি এডিট, ভার্সন={rep['variant']}")
    pr = media.probe(out)
    check("আউটপুটে অডিও আছে", pr["has_audio"], f"audio={pr['has_audio']} dur={pr['duration']:.2f}")

    # ৭. Facebook রেন্ডার (১:১, ব্লার/ব্ল্যাক ফিল)
    out2 = RENDER / "facebook-ready.mp4"
    rep2 = media.render(joined, out2, "facebook", cfg, words)
    check("Facebook রেন্ডার 1080x1080", (rep2["width"], rep2["height"]) == (1080, 1080),
          f"{rep2['width']}x{rep2['height']} {rep2['duration']}s {rep2['size_bytes'] / 1048576:.1f}MB")

    # ৮. প্রমাণ ফ্রেম
    for name, file in (("youtube", out), ("facebook", out2)):
        png = RENDER / f"{name}-proof.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "3", "-i", str(file),
                        "-frames:v", "1", str(png)], check=True, capture_output=True)
        check(f"{name} প্রমাণ ফ্রেম", png.exists() and png.stat().st_size > 20000,
              f"{png.stat().st_size // 1024} KB")

    # ৯. ক্যাপশন ক্লোজ-আপ
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "3", "-i", str(out),
                    "-vf", "crop=1100:200:410:780", "-frames:v", "1",
                    str(RENDER / "caption-zoom.png")], check=True, capture_output=True)
    check("ক্যাপশন ক্লোজ-আপ", (RENDER / "caption-zoom.png").stat().st_size > 8000)

    # ১০. karaoke সময় ঠিক আছে কি না
    first = [l for l in ass_text.splitlines() if l.startswith("Dialogue")][0]
    check("ASS Dialogue ফরম্যাট", first.count(",") >= 9 and "{\\fad" in first, first[:120])

    print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
    if FAIL:
        print("FAILED:", FAIL)
    return 0 if not FAIL else 3


if __name__ == "__main__":
    raise SystemExit(main())
