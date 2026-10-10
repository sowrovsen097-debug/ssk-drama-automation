# studio.py — ২০-ধাপের ভিডিও এডিটিং ইঞ্জিন
import os, io, glob, shutil, subprocess
from analyze import probe, plan, timeline_for
import subtitle as submod

def _ff(cmd):
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {' '.join(map(str, cmd))[:400]}\n{p.stderr.decode()[-900:]}")
    return p

def out_dims(cfg, platform):
    if platform == "facebook":
        return int(cfg.get("fb_width", 1080)), int(cfg.get("fb_height", 1080))
    return int(cfg.get("yt_width", 1920)), int(cfg.get("yt_height", 1080))

def reframe_filter(w, h, src_w, src_h, face_x=0.5):
    if not src_w or not src_h:
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    target = w / float(h); src = src_w / float(src_h)
    if abs(target - src) < 0.02: return f"scale={w}:{h},setsar=1"
    if src > target:
        cw = int(round(src_h * target)); cw -= cw % 2
        off = int(round((src_w - cw) * min(max(face_x, 0.15), 0.85)))
        off = max(0, min(off, src_w - cw))
        return f"crop={cw}:{src_h}:{off}:0,scale={w}:{h},setsar=1"
    ch = int(round(src_w / target)); ch -= ch % 2
    off = int(round((src_h - ch) * 0.42))
    off = max(0, min(off, src_h - ch))
    return f"crop={src_w}:{ch}:0:{off},scale={w}:{h},setsar=1"

def safe_zoom(w, h, margin=0.04):
    f = 1 - margin
    return f"crop=w=iw*{f:.4f}:h=ih*{f:.4f}:x=(iw-ow)/2:y=(ih-oh)/2,scale={w}:{h},setsar=1"

def cut_and_grade(src, segments, outdir, w, h, face_x, cfg, platform, log=print):
    info = probe(src)
    mirror = bool(cfg.get("mirror_facebook", True)) and platform == "facebook"
    crf, preset = str(cfg.get("quality_crf", 23)), cfg.get("quality_preset", "veryfast")
    fps = info["fps"] if 5 <= info["fps"] <= 60 else 30.0
    files = []
    for i, (s, e) in enumerate(segments):
        out = os.path.join(outdir, f"seg_{i:03d}.mp4")
        chain = [
            safe_zoom(w, h, 0.03),
            reframe_filter(w, h, info["width"], info["height"], face_x),
            "hqdn3d=1.2:1.2:4:4",
            "eq=brightness=0.03:contrast=1.09:saturation=1.16:gamma=1.02",
            "unsharp=5:5:0.55:5:5:0.0"
        ]
        if mirror: chain.append("hflip")
        chain.append(f"fps={fps},format=yuv420p")
        cmd = ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i", src, "-t", f"{max(e - s, 0.4):.3f}", "-vf", ",".join(chain)]
        if info["has_audio"]: cmd += ["-af", "afftdn=nf=-25,highpass=f=70,lowpass=f=12000"]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out]
        if not info["has_audio"]: cmd += ["-an"]
        _ff(cmd); files.append(out)
    log(f"segments cut: {len(files)}")
    return files, info

def xfade_concat(files, out, w, h, fps=30.0, transition="fade", dur=0.5, log=print):
    if len(files) == 1:
        shutil.copy(files[0], out); return out
    n = len(files); dur_list = [probe(f)["duration"] for f in files]
    cmd = ["ffmpeg", "-y"]
    for f in files: cmd += ["-i", f]
    fc, vprev, aprev, offset = [], "0:v", "0:a", 0.0
    for i in range(1, n):
        offset += max(dur_list[i-1] - dur, 0.1)
        vout, aout = f"v{i}", f"a{i}"
        fc.append(f"[{vprev}][{i}:v]xfade=transition={transition}:duration={dur:.2f}:offset={offset:.3f},format=yuv420p[{vout}]")
        fc.append(f"[{aprev}][{i}:a]acrossfade=d={dur:.2f}:c1=tri:c2=tri[{aout}]")
        vprev, aprev = vout, aout
    cmd += ["-filter_complex", ";".join(fc), "-map", f"[{vprev}]", "-map", f"[{aprev}]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    _ff(cmd); log("transitions applied")
    return out

def brand_card(out, w, h, text, seconds, fps=30.0, bg="0x101014"):
    fs = int(min(w, h) * 0.075)
    font = submod.find_font() or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    txt = text.replace(":", "\\:").replace("'", "")
    vf = f"color=c={bg}:s={w}x{h}:r={fps}:d={seconds:.2f},format=yuv420p,drawtext=fontfile={font}:text='{txt}':fontcolor=0xFFC300:fontsize={fs}:x=(w-text_w)/2:y=(h-text_h)/2:alpha='if(lt(t,0.6),t/0.6,1)'"
    _ff(["ffmpeg", "-y", "-f", "lavfi", "-i", vf, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-t", f"{seconds:.2f}", "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", out])
    return out

def join_simple(files, out, w, h, fps=30.0):
    lst = out + ".txt"
    with io.open(lst, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{os.path.abspath(p).replace("'", "'\\''")}'\n")
    _ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-vf", f"scale={w}:{h},setsar=1,fps={fps},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out])
    os.remove(lst); return out

def ensure_assets(assets_dir):
    os.makedirs(os.path.join(assets_dir, "music"), exist_ok=True)
    os.makedirs(os.path.join(assets_dir, "sfx"), exist_ok=True)
    whoosh = os.path.join(assets_dir, "sfx", "whoosh.mp3")
    if not os.path.exists(whoosh):
        try:
            _ff(["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:a=0.6:r=44100", "-af", "highpass=f=700,lowpass=f=6000,afade=t=in:st=0:d=0.06,afade=t=out:st=0.18:d=0.27,volume=0.55", whoosh])
        except Exception:
            whoosh = ""
    songs = (glob.glob(os.path.join(assets_dir, "music", "*.mp3")) + glob.glob(os.path.join(assets_dir, "music", "*.m4a")) + glob.glob(os.path.join(assets_dir, "music", "*.wav")))
    if not songs:
        pad = os.path.join(assets_dir, "music", "ssk_pad.mp3")
        try:
            _ff(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=196:duration=90", "-f", "lavfi", "-i", "sine=frequency=294:duration=90", "-f", "lavfi", "-i", "sine=frequency=147:duration=90", "-filter_complex", "[0][1][2]amix=inputs=3:weights=0.5 0.3 0.5,tremolo=f=0.15:d=0.4,lowpass=f=1200,afade=t=in:st=0:d=3,afade=t=out:st=84:d=6,volume=0.5", "-c:a", "libmp3lame", "-b:a", "128k", pad])
            songs = [pad]
        except Exception:
            songs = []
    return (songs[0] if songs else ""), whoosh

def build_audio(muxed_video, out, cfg, cut_times, total_dur, assets_dir, log=print):
    music, whoosh = ensure_assets(assets_dir)
    vol = float(cfg.get("music_volume", 0.12)); pitch = 1.035
    cmd = ["ffmpeg", "-y", "-i", muxed_video]
    fc = [f"[0:a]afftdn=nf=-25,highpass=f=70,lowpass=f=12000,asetrate={int(48000 * pitch)},aresample=48000,atempo={1/pitch:.4f},dynaudnorm=f=250:g=8:p=0.9[voice]"]
    last = "[voice]"
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        fc.append(f"[1:a]volume={vol:.3f},highpass=f=80,lowpass=f=9000[mus]")
        fc.append("[voice][mus]amix=inputs=2:duration=first:normalize=0[amixed]")
        last = "[amixed]"
    if whoosh and cut_times:
        widx = 1 + (1 if music else 0)
        cmd += ["-i", whoosh]
        src_stream = "[mus]" if music else "[voice]"
        for k, t in enumerate(cut_times[:8]):
            fc.append(f"[{widx}:a]volume=0.40,adelay={int(t*1000)}|{int(t*1000)}[w{k}]")
            fc.append(f"{src_stream}[w{k}]amix=inputs=2:duration=longest:normalize=0[s{k}]")
            src_stream = f"[s{k}]"
        if music:
            fc.append(f"[voice]{src_stream}amix=inputs=2:duration=first:normalize=0[amixed]")
            last = "[amixed]"
        else:
            last = src_stream
    fc.append(f"{last}loudnorm=I=-14:TP=-1.5:LRA=11[final]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[final]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    try:
        _ff(cmd); log("audio: voice+pitch + music + whoosh + loudnorm ✓")
    except Exception as e:
        log(f"audio mix fallback: {str(e)[:140]}")
        _ff(["ffmpeg", "-y", "-i", muxed_video, "-af", "afftdn=nf=-25,loudnorm=I=-14:TP=-1.5:LRA=11", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    return out

def burn_subs(video, ass, out, w, cfg, total_dur, log=print):
    crf, preset = str(cfg.get("quality_crf", 23)), cfg.get("quality_preset", "veryfast")
    fc = []
    if cfg.get("subtitles", True) and ass and os.path.exists(ass):
        fc.append(f"ass={ass.replace('\\', '/')}:fontsdir=/usr/share/fonts:shaping=complex")
    if cfg.get("progress_bar", True) and total_dur > 0:
        fc.append("drawbox=x=0:y=h-8:w=iw:h=8:color=black@0.35:t=fill")
        fc.append(f"drawbox=x=0:y=h-8:w='{int(w*0.30)}*(t/{total_dur:.3f})':h=8:color=0xFFC300@0.95:t=fill")
    _ff(["ffmpeg", "-y", "-i", video, "-vf", (",".join(fc) if fc else "null"), "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out])
    return out

def cap_size(path, cfg, log=print):
    limit = float(cfg.get("max_output_mb", 1700)) * 1024 * 1024
    if os.path.getsize(path) <= limit: return path
    dur = max(probe(path)["duration"], 1)
    target_kbps = int((limit * 8 / dur) / 1000 * 0.92)
    tmp = path.replace(".mp4", "_c.mp4")
    _ff(["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{max(target_kbps, 600)}k", "-maxrate", f"{max(target_kbps, 600)}k", "-bufsize", f"{max(target_kbps * 2, 1200)}k", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp])
    os.replace(tmp, path); log(f"size capped → {os.path.getsize(path)/1048576:.0f} MB")
    return path

def make_edited(src, outpath, cfg, platform, workdir, log=print):
    os.makedirs(workdir, exist_ok=True)
    w, h = out_dims(cfg, platform)
    import analyze as A
    a = A.analyze(src, cfg, log=log)
    p = plan(a, keep_ratio=float(cfg.get("keep_ratio", 0.8125)))
    log(f"plan: {len(p['segments'])} cuts · kept {p['kept']/max(p['total'],1)*100:.1f}% · hook first · duration {p['kept']/60.0:.1f} min")
    segs, info = cut_and_grade(src, p["segments"], workdir, w, h, p["face_x"], cfg, platform, log)
    fps = info["fps"] if 5 <= info["fps"] <= 60 else 30.0
    joined = os.path.join(workdir, "joined.mp4")
    xfade_concat(segs, joined, w, h, fps=fps, dur=0.5, log=log)
    tl = timeline_for(p["segments"], p["order"], 1.0, lead=0.0)
    cut_marks = [tl[i]["out_in"] for i in range(1, len(tl))]
    intro = brand_card(os.path.join(workdir, "intro.mp4"), w, h, f"{cfg.get('brand_name', 'SSK DRAMA')} PRESENTS", 3.0, fps)
    outro = brand_card(os.path.join(workdir, "outro.mp4"), w, h, f"{cfg.get('brand_name', 'SSK DRAMA')} • SUBSCRIBE & SHARE", 5.0, fps)
    staged = join_simple([intro, joined, outro], os.path.join(workdir, "staged.mp4"), w, h, fps)
    lead = 3.0
    tl_intro = timeline_for(p["segments"], p["order"], 1.0, lead=lead)
    cut_marks = [m + lead for m in cut_marks]
    staged_mux = os.path.join(workdir, "staged_mux.mp4")
    if info["has_audio"]:
        staged = build_audio(staged, staged_mux, cfg, cut_marks, p["kept"] + 8, workdir, log)
    else:
        staged_mux = staged
    ass = ""
    if cfg.get("subtitles", True):
        try:
            tr = submod.transcribe(src, cfg.get("whisper_model", "base"), cfg.get("whisper_language", "auto"), log=log)
            words = A.remap_words(tl_intro, tr["words"])
            lines = submod.group_lines(words, max_words=4, max_chars=30)
            if lines:
                ass, cnt = submod.build_ass(lines, os.path.join(workdir, "subs.ass"), w=w, h=h, colorful=bool(cfg.get("subtitle_colorful", True)), brand=cfg.get("brand_name", ""), intro_dur=3.0)
                log(f"subtitle lines: {cnt} ({tr.get('engine')})")
        except Exception as e:
            log(f"subtitle skipped: {str(e)[:160]}")
    finalsrc = os.path.join(workdir, "final_nosub.mp4")
    shutil.move(staged_mux, finalsrc)
    total = probe(finalsrc)["duration"]
    burn_subs(finalsrc, ass, outpath, w, cfg, total, log)
    cap_size(outpath, cfg, log)
    if not (os.path.exists(outpath) and os.path.getsize(outpath) > 100000):
        raise RuntimeError("output video invalid")
    meta = {
        "duration": probe(outpath)["duration"],
        "size_mb": round(os.path.getsize(outpath)/1048576, 1),
        "kept_ratio": round(p["kept"] / max(p["total"], 1), 3),
        "cuts": len(p["segments"]),
        "platform": platform,
        "dims": f"{w}x{h}",
        "subtitle": bool(ass)
    }
    log(f"edited → {meta['duration']/60:.1f} min · {meta['size_mb']} MB · {meta['dims']}")
    return outpath, meta
