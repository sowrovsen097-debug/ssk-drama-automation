# -*- coding: utf-8 -*-
"""SSK DRAMA — OpenCV সিন-ডিটেকশন, স্মার্ট কাট, 16:9 ও 1:1 (ব্লার-প্যাড) এনকোড"""
import datetime, json, os, random, re, subprocess
import pytz

BD = pytz.timezone("Asia/Dhaka")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def bd_now():
    return datetime.datetime.now(BD)


def hhmm_to_dt(day_dt, hhmm):
    h, m = [int(x) for x in hhmm.split(":")]
    return BD.localize(datetime.datetime(day_dt.year, day_dt.month, day_dt.day, h, m))


def probe(path):
    r = _run(["ffprobe", "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", path])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr[:300]}")
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    fps = 25.0
    if v and "/" in str(v.get("r_frame_rate", "")):
        try:
            n, dn = v["r_frame_rate"].split("/")
            fps = float(n) / float(dn) if float(dn) else 25.0
        except Exception:
            pass
    return {"duration": float(d["format"].get("duration") or 0),
            "width": int(v["width"]) if v else 0,
            "height": int(v["height"]) if v else 0,
            "fps": fps, "has_audio": a is not None,
            "size": int(d["format"].get("size") or 0)}


def find_font(name="Noto Sans Bengali"):
    try:
        r = _run(["fc-match", "-f", "%{file}", name])
        p = (r.stdout or "").strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    for p in ["/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
              "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if os.path.exists(p):
            return p
    return None


# ----------------------------------------------------------------------------
# ডাউনলোড + সোর্স ভিডিও বাছাই
# ----------------------------------------------------------------------------
def chat0(url):
    """?si= আসল ডাউনলোডের দরকার নেই, পরিষ্কার করা হয়।"""
    return url.split("?")[0].strip()


def ydl_base():
    opts = {"quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "extractor_retries": 3, "retries": 5, "socket_timeout": 60}
    return opts


def pick_source_video(channel_url, history, cfg):
    """চ্যানেলের সবচেয়ে পুরনো, আগে প্রসেস না-করা ভিডিওটা বাছাই করে।"""
    import yt_dlp
    url = chat0(channel_url)
    if not url:
        return None
    try:
        with yt_dlp.YoutubeDL({**ydl_base(), "extract_flat": True,
                               "playlistend": cfg.get("scan_depth", 40)}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print("[pick] channel error", e)
        return None
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    now = datetime.datetime.now(datetime.timezone.utc)
    cands = []
    for e in entries:
        vid = e["id"]
        if vid in history:
            continue
        try:
            with yt_dlp.YoutubeDL(ydl_base()) as ydl:
                v = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                     download=False)
        except Exception:
            continue
        up = v.get("upload_date")
        if not up:
            continue
        try:
            upd = datetime.datetime.strptime(up, "%Y%m%d").replace(
                tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        days = (now - upd).days
        if days < cfg.get("min_age_days", 60):
            continue
        maxd = cfg.get("max_age_days", 0)
        if maxd and days > maxd:
            continue
        cands.append({"id": vid, "title": v.get("title") or "",
                      "description": v.get("description") or "",
                      "duration": v.get("duration") or 0,
                      "upload_date": up, "days_old": days,
                      "url": f"https://www.youtube.com/watch?v={vid}"})
    if not cands:
        return None
    cands.sort(key=lambda x: x["days_old"], reverse=True)  # পুরনো আগে
    return cands[0]


def download(url, workdir, cfg):
    import yt_dlp
    os.makedirs(workdir, exist_ok=True)
    tmpl = os.path.join(workdir, "src_%(id)s.%(ext)s")
    opts = {**ydl_base(),
            "format": ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                       "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
            "outtmpl": tmpl, "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    for f in sorted(os.listdir(workdir)):
        if f.startswith("src_") and f.lower().endswith((".mp4", ".mkv", ".webm")):
            return os.path.join(workdir, f)
    return None


# ----------------------------------------------------------------------------
# সাইলেন্স + সিন অ্যানালাইসিস
# ----------------------------------------------------------------------------
def detect_silences(path, noise_db=-32, min_dur=0.7):
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
              f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    sil, start = [], None
    for line in (r.stderr or "").splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            start = float(m.group(1)); continue
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and start is not None:
            sil.append((start, float(m.group(1)))); start = None
    if start is not None:
        sil.append((start, 1e9))
    return sil


def scene_analyze(path, sample_every=0.5, threshold=0.32, max_samples=4200):
    """OpenCV দিয়ে দৃশ্য-পরিবর্তন + প্রতি সিনের মুভমেন্ট স্কোর।"""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(fps * max(0.2, sample_every)))
    times, diffs, prev, idx, taken = [], [], None, 0, 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok and frame is not None:
                small = cv2.resize(frame, (240, 136))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                d = 999.0 if prev is None else float(np.mean(cv2.absdiff(gray, prev)))
                prev = gray
                times.append(idx / fps)
                diffs.append(d)
                taken += 1
                if taken >= max_samples:
                    break
        idx += 1
    cap.release()

    if len(times) < 3:
        return [(0.0, times[-1] if times else 0.0)], [1.0]

    arr = np.array(diffs[1:], dtype="float32")
    cut = max(12.0, float(arr.mean() + 2.5 * arr.std()))
    scenes, motion, start = [], [], 0.0
    for i in range(1, len(times)):
        if diffs[i] >= cut:
            scenes.append((start, times[i]))
            motion.append(float(np.mean(diffs[max(1, i - 4):i])) if i > 1 else 1.0)
            start = times[i]
    scenes.append((start, times[-1]))
    motion.append(float(np.mean(diffs[-4:])) if len(diffs) > 4 else 1.0)
    # খুব ছোট সিনগুলো আগেরটার সাথে জোড়া
    merged, mmerged = [], []
    for (s, e), m in zip(scenes, motion):
        if merged and (e - s) < 1.5:
            ps, _ = merged[-1]
            merged[-1] = (ps, e)
            mmerged[-1] = (mmerged[-1] + m) / 2
        else:
            merged.append((s, e)); mmerged.append(m)
    print(f"[scene] {len(merged)} দৃশ্য, cut থ্রেশহোল্ড {cut:.1f}")
    return merged, mmerged


# ----------------------------------------------------------------------------
# কাট-প্ল্যান (৪০ মিনিট → ৩০-৩৫ মিনিট, স্টোরি ক্রম অটুট)
# ----------------------------------------------------------------------------
def _merge(ranges):
    ranges = sorted([(max(0.0, s), e) for s, e in ranges if e - s > 0.3])
    out = []
    for s, e in ranges:
        if out and s - out[-1][1] < 0.06:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _fine_trim(ranges, mx, mn):
    ranges = [list(r) for r in ranges]
    guard = 0
    while sum(e - s for s, e in ranges) > mx and guard < 400:
        guard += 1
        idxs = list(range(1, len(ranges) - 1)) if len(ranges) > 2 else [0]
        i = max(idxs, key=lambda k: ranges[k][1] - ranges[k][0])
        over = sum(e - s for s, e in ranges) - mx
        cut = min(over, 30.0)
        if ranges[i][1] - ranges[i][0] - cut < 20:
            cut = max(0.0, ranges[i][1] - ranges[i][0] - 20)
        if cut <= 0.1:
            break
        ranges[i][1] -= cut
    return _merge([(s, e) for s, e in ranges])


def _silence_trim(ranges, silences, keep=0.25, min_chunk=2.0):
    out = []
    for s, e in ranges:
        cur = s
        for ss, se in silences:
            if se <= s or ss >= e:
                continue
            cut_s, cut_e = max(ss, s), min(se, e)
            if cut_e - cut_s < 0.8:
                continue
            if cut_s - cur >= min_chunk:
                out.append((cur, cut_s))
                cur = min(cut_e, cut_s + keep)
        if e - cur >= min_chunk:
            out.append((cur, e))
    return out or list(ranges)


def plan_segments(duration, scenes, motion, silences, cfg):
    mn = cfg.get("target_min_minutes", 30) * 60
    mx = cfg.get("target_max_minutes", 35) * 60
    feat = cfg.get("features", {})

    if duration <= mx:
        ranges = [(0.0, duration)]
        if feat.get("silence_trim", True):
            t = _silence_trim(ranges, silences)
            if len(t) <= cfg.get("max_segments", 90):
                ranges = t
        return _merge(ranges)

    n = len(scenes)
    protect = {0, n - 1}
    if n > 2:
        protect.add(1)
    dropped, total = set(), duration
    order = sorted([i for i in range(n) if i not in protect],
                   key=lambda i: motion[i])
    for i in order:
        if total <= mx + 60:
            break
        d = scenes[i][1] - scenes[i][0]
        if d < 5:
            continue
        dropped.add(i); total -= d
    ranges = _merge([scenes[i] for i in range(n) if i not in dropped])
    ranges = _fine_trim(ranges, mx, mn)

    if feat.get("silence_trim", True):
        t = _silence_trim(ranges, silences)
        if len(t) <= cfg.get("max_segments", 90) and \
           sum(e - s for s, e in t) <= mx:
            ranges = t

    tot = sum(e - s for s, e in ranges)
    if tot > mx:
        ranges = _fine_trim(ranges, mx, mn)
    if tot < mn and duration > mn:
        print(f"[plan] সতর্কতা: ফলাফল {tot/60:.1f} মিনিট (টার্গেট {mn/60:.0f}-{mx/60:.0f})")
    print(f"[plan] চূড়ান্ত দৈর্ঘ্য {sum(e-s for s,e in ranges)/60:.2f} মিনিট, "
          f"{len(ranges)} সেগমেন্ট")
    return ranges


# ----------------------------------------------------------------------------
# রেন্ডার (এক পাসেই কাট + ক্রপ/ব্লার-প্যাড + সাবটাইটেল + মিউজিক + অডিও ক্লিন)
# ----------------------------------------------------------------------------
def render(input_path, out_path, platform, W, H, ranges, ass_path, music_path,
           cfg, total_out, brand="SSK DRAMA"):
    feat = cfg.get("features", {})
    has_ranges = len(ranges) > 0
    if not has_ranges:
        raise RuntimeError("no ranges")

    f = []
    n = len(ranges)
    for i, (s, e) in enumerate(ranges):
        f.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        f.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(n))
    f.append(f"{concat_in}concat=n={n}:v=1:a=1[vc][ac]")

    # ---- aspect ----
    if platform == "fb":
        # 1:1 — উপরে-নিচে ব্লার ব্যাকগ্রাউন্ড, মূল ফ্রেম অক্ষত (কিছু কাটা যায় না)
        f.append("[vc]split=2[bg][fg]")
        f.append(f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},gblur=sigma=26,eq=brightness=-0.10:saturation=1.05[bgb]")
        f.append(f"[fg]scale={W}:-2:flags=lanczos[fgs]")
        f.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vasp]")
    else:
        # 16:9 — পুরো ফ্রেম সমানুপাতে ফিট, পাশে কালো বার (কিছু কাটা যায় না)
        f.append(f"[vc]scale={W}:{H}:force_original_aspect_ratio=decrease:"
                 f"flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black[vasp]")

    vcur = "[vasp]"
    if feat.get("color_grade", True):
        f.append(f"{vcur}eq=brightness=0.03:contrast=1.08:saturation=1.12:gamma=1.02[vcg]")
        vcur = "[vcg]"
    if feat.get("mirror_frame", False):
        f.append(f"{vcur}hflip[vm]"); vcur = "[vm]"
    if feat.get("zoom_pan", False):
        f.append(f"{vcur}zoompan=z='min(zoom+0.00035,1.07)':d=1:"
                 f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30[vz]")
        vcur = "[vz]"

    font_path = find_font() if feat.get("brand_overlay", True) else None
    if font_path and brand:
        safe_brand = re.sub(r"[:'\\]", "", brand)
        fs = 92 if platform == "yt" else 66
        ypos = "h*0.08" if platform == "yt" else "h*0.06"
        f.append(f"{vcur}drawtext=fontfile={font_path}:text='{safe_brand}':"
                 f"fontcolor=white:fontsize={fs}:x=(w-text_w)/2:y={ypos}:"
                 f"enable='between(t,0.2,4.2)'[vb1]")
        f.append(f"[vb1]drawtext=fontfile={font_path}:text='{safe_brand}':"
                 f"fontcolor=white:fontsize={fs}:x=(w-text_w)/2:y={ypos}:"
                 f"enable='between(t,{max(5.0,total_out-4.5):.2f},{max(5.1,total_out-0.4):.2f})'[vb2]")
        vcur = "[vb2]"

    f.append(f"{vcur}fade=in:st=0:d=0.4,fade=out:st={max(0.5,total_out-0.6):.2f}:d=0.5[vi]")
    vcur = "[vi]"

    if ass_path and os.path.exists(ass_path) and os.path.getsize(ass_path) > 0:
        f.append(f"{vcur}subtitles=filename={ass_path}:fontsdir=/usr/share/fonts[vsub]")
        vcur = "[vsub]"

    f.append(f"{vcur}format=yuv420p[vout]")

    # ---- audio ----
    acur = "[ac]"
    if feat.get("denoise", True):
        f.append(f"{acur}afftdn=nr=10:nf=-25,highpass=f=70[ad]"); acur = "[ad]"
    if feat.get("pitch_touch", True):
        f.append(f"{acur}asetrate=44100*1.015,aresample=44100,atempo=0.9852[ap]")
        acur = "[ap]"
    if feat.get("loudnorm", True):
        f.append(f"{acur}loudnorm=I=-16:TP=-1.5:LRA=11[al]"); acur = "[al]"
    f.append(f"{acur}aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[av]")

    if music_path and os.path.exists(music_path) and feat.get("music", True):
        vol = float(cfg.get("features", {}).get("music_volume", 0.1))
        f.append(f"[1:a]volume={vol},aloop=loop=-1:size=2147483647,"
                 f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[amg]")
        f.append("[amg][av]sidechaincompress=threshold=0.03:ratio=12:attack=15:"
                 "release=350[amduck]")
        f.append("[av][amduck]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        f.append("[av]anull[aout]")

    fc = ";".join(f)

    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-stats",
           "-i", input_path]
    if music_path and os.path.exists(music_path) and feat.get("music", True):
        cmd += ["-stream_loop", "-1", "-i", music_path]
    cmd += ["-filter_complex", fc,
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total_out:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf",
            ("23" if platform == "yt" else "24"),
            "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
            "-r", "30", "-g", "60", "-threads", "0"]
    if platform == "fb":
        cmd += ["-maxrate", "4M", "-bufsize", "8M"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", out_path]

    print("[ffmpeg] এনকোড শুরু হচ্ছে…")
    r = _run(cmd)
    if r.returncode != 0:
        print(r.stderr[-4000:])
        raise RuntimeError("ffmpeg render failed")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 100000:
        raise RuntimeError("output file missing/too small")
    return out_path
