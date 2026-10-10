# analyze.py — ভিডিও অ্যানালাইসিস ও কাট করার প্ল্যান তৈরি (OpenCV + NumPy)
import os, json, math, subprocess
import numpy as np

try:
    import cv2
    HAVE_CV = True
except Exception:
    HAVE_CV = False

def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    j = json.loads(out.decode("utf-8", "ignore") or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        num, den = v.get("avg_frame_rate", "30/1").split("/")
        fps = float(num) / float(den or 1)
    except Exception:
        fps = 30.0
    dur = 0.0
    for cand in (v.get("duration"), j.get("format", {}).get("duration")):
        try:
            dur = float(cand); break
        except Exception:
            continue
    return {"duration": dur, "width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "fps": fps or 30.0, "has_audio": bool(a)}

def audio_energy(path, sr=8000, step=1.0):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    raw = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if raw.size == 0: return np.zeros(1, dtype=np.float32)
    n = max(int(sr * step), 1)
    usable = (raw.size // n) * n
    if usable == 0: return np.array([float(np.sqrt(np.mean(raw ** 2) + 1e-9))], dtype=np.float32)
    return np.sqrt(np.mean(raw[:usable].reshape(-1, n) ** 2, axis=1) + 1e-9)

def read_gray_frames(path, fps=1.0, width=256, probe_info=None):
    info = probe_info or probe(path)
    h = int(info["height"]) or 9; w = int(info["width"]) or 16
    gh = max(int(round(width * h / max(w, 1))), 2); gh -= gh % 2
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={width}:{gh}", "-pix_fmt", "gray", "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    n = buf.size // (width * gh)
    if n <= 0: return np.zeros((0, gh, width), dtype=np.uint8)
    return buf[:n * width * gh].reshape(n, gh, width)

def _cascade():
    if not HAVE_CV: return None
    try:
        c = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        return c if not c.empty() else None
    except Exception:
        return None

def frame_features(frames):
    n = frames.shape[0]
    face = np.zeros(n, dtype=np.float32); fx = np.full(n, 0.5, dtype=np.float32)
    motion = np.zeros(n, dtype=np.float32); bright = np.zeros(n, dtype=np.float32)
    if n == 0: return {"face": face, "face_x": fx, "motion": motion, "bright": bright}
    cas = _cascade()
    for i in range(n):
        f = frames[i]
        bright[i] = float(f.mean()) / 255.0
        if i: motion[i] = float(np.mean(np.abs(f.astype(np.int16) - frames[i-1].astype(np.int16)))) / 255.0
        if cas is not None and i % 2 == 0:
            try:
                r = cas.detectMultiScale(f, 1.15, 5, minSize=(24, 24))
                if len(r):
                    face[i] = 1.0
                    fx[i] = float(np.mean([(x + w / 2.0) / frames.shape[2] for (x, y, w, h) in r]))
            except Exception:
                pass
    return {"face": face, "face_x": fx, "motion": motion, "bright": bright}

def _smooth(a, k=3):
    if a.size == 0 or k <= 1: return a
    return np.convolve(a, np.ones(k, dtype=np.float32) / k, mode="same")

def _norm(a):
    a = np.asarray(a, dtype=np.float32)
    lo, hi = float(a.min()), float(a.max())
    return (a - lo) / (hi - lo + 1e-6)

def analyze(path, cfg=None, fps=1.0, width=256, log=print):
    cfg = cfg or {}
    info = probe(path)
    dur = info["duration"]
    log(f"analysing {dur / 60.0:.1f} min video")
    frames = read_gray_frames(path, fps=fps, width=width, probe_info=info)
    ff = frame_features(frames)
    energy = audio_energy(path)
    n = int(max(len(energy), len(ff["face"]), 1))
    
    def fit(a):
        a = np.asarray(a, dtype=np.float32)
        if a.size == n: return a
        idx = np.linspace(0, max(a.size - 1, 0), n)
        return np.interp(idx, np.arange(a.size), a) if a.size else np.zeros(n, dtype=np.float32)
        
    motion = _smooth(fit(ff["motion"]), 3)
    face = _smooth(fit(ff["face"]), 3)
    bright = fit(ff["bright"])
    en = _norm(_smooth(energy, 3))
    face_x = float(np.median(ff["face_x"][ff["face_x"] > 0])) if np.any(ff["face_x"] > 0) else 0.5
    scene = _norm(_smooth(np.abs(np.diff(motion, prepend=motion[:1])), 3))
    expo_ok = np.clip(1.0 - np.abs(bright - 0.45) / 0.45, 0.0, 1.0)
    score = _norm(_smooth(1.15 * _norm(face) + 0.95 * _norm(motion) + 0.55 * scene + 1.05 * en + 0.45 * expo_ok, 3))
    return {"info": info, "n": n, "score": score, "face_x": face_x}

def plan(analysis, keep_ratio=0.8125, win=4.0, protect_edges=2.5):
    dur = analysis["info"]["duration"]
    n = analysis["n"]
    if dur <= 0 or n <= 0:
        return {"segments": [[0.0, dur]], "order": [0], "hook_index": 0, "kept": dur, "total": dur, "face_x": 0.5}
    total = float(dur)
    nwin = max(int(math.ceil(total / win)), 1)
    scores, bounds = [], []
    for i in range(nwin):
        s = i * win
        e = min(total, s + win)
        if e - s < 1.0: continue
        a, b = int(s), max(int(e), int(s) + 1)
        val = float(np.mean(analysis["score"][a:min(b, n)]))
        if e >= total - protect_edges: val *= 0.35
        scores.append(val); bounds.append([s, e])
    if not scores:
        return {"segments": [[0.0, total]], "order": [0], "hook_index": 0, "kept": total, "total": total, "face_x": analysis["face_x"], "cuts": 1}
    budget = total * float(keep_ratio)
    chosen, used = [], 0.0
    for idx in list(np.argsort(scores)[::-1]):
        s, e = bounds[idx]
        d = e - s
        if used + d <= budget + 0.001 or not chosen:
            chosen.append(idx); used += d
        if used >= budget: break
    chosen.sort()
    segments = [bounds[i] for i in chosen]
    h = int(np.argmax([scores[i] for i in chosen])) if chosen else 0
    order_final = [h] + [i for i in range(len(segments)) if i != h]
    kept = sum(se[1] - se[0] for se in segments)
    return {"segments": segments, "order": order_final, "hook_index": 0, "kept": kept, "total": total, "face_x": analysis["face_x"], "cuts": len(segments)}

def timeline_for(segments, order, speed=1.0, lead=0.0):
    tl, t = [], float(lead)
    for i in order:
        s, e = segments[i]
        d = (e - s) / float(speed)
        tl.append({"src_in": float(s), "src_out": float(e), "out_in": t, "out_out": t + d})
        t += d
    return tl

def remap_time(tl, t_src):
    for seg in tl:
        if seg["src_in"] <= t_src <= seg["src_out"]:
            return seg["out_in"] + (t_src - seg["src_in"])
    return None

def remap_words(tl, words):
    out = []
    for w in words or []:
        try:
            st, en, txt = float(w["start"]), float(w["end"]), str(w.get("word", w.get("text", "")))
        except Exception:
            continue
        mid = (st + en) / 2.0
        if remap_time(tl, mid) is None: continue
        ns = remap_time(tl, st); ne = remap_time(tl, en)
        if ns is None: ns = remap_time(tl, mid)
        if ne is None: ne = ns + 0.24
        out.append({"start": ns, "end": max(ne, ns + 0.08), "word": txt})
    return out
