# -*- coding: utf-8 -*-
"""SSK DRAMA — Whisper ট্রান্সক্রিপশন + রঙিন ক্যারাওকে ASS সাবটাইটেল"""
import os, re, subprocess

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def extract_audio(video_path, wav_path):
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
          "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav_path])
    return wav_path


def transcribe(wav_path, model_name="small", engine="auto", language=None):
    """faster-whisper আগে ট্রাই করে (৪x ফাস্ট), না পারলে openai-whisper।"""
    words = []
    if engine in ("auto", "faster"):
        try:
            from faster_whisper import WhisperModel
            root = os.path.expanduser("~/.cache/ssk-whisper")
            model = WhisperModel(model_name, device="cpu", compute_type="int8",
                                 download_root=root)
            segs, info = model.transcribe(wav_path, word_timestamps=True,
                                          vad_filter=True,
                                          language=(language or None))
            for s in segs:
                for w in (getattr(s, "words", None) or []):
                    t = (w.word or "").strip()
                    if t:
                        words.append({"start": float(w.start),
                                      "end": float(w.end), "text": t})
            if words:
                print(f"[whisper] faster-whisper OK ({info.language})")
                return words, f"faster-whisper/{model_name}/{info.language}"
        except Exception as e:
            print("[whisper] faster failed:", e)
            if engine == "faster":
                raise
    import whisper
    model = whisper.load_model(model_name)
    res = model.transcribe(wav_path, word_timestamps=True,
                           language=(language or None))
    for s in res.get("segments", []):
        for w in (s.get("words") or []):
            t = (w.get("word") or "").strip()
            if t:
                words.append({"start": float(w["start"]),
                              "end": float(w["end"]), "text": t})
    print("[whisper] openai-whisper OK")
    return words, f"openai-whisper/{model_name}"


def remap_words(words, ranges):
    """কাটার পর পুরনো টাইমস্ট্যাম্পকে নতুন টাইমলাইনে বসায় (এক ধাপেই এনকোড হয়)।"""
    out, off = [], 0.0
    for (rs, re_) in ranges:
        for w in words:
            ws, we = w["start"], w["end"]
            if we <= rs or ws >= re_:
                continue
            ns = max(ws, rs) - rs + off
            ne = min(we, re_) - rs + off
            if ne - ns > 0.02:
                out.append({"start": ns, "end": ne, "text": w["text"]})
        off += (re_ - rs)
    return out


def pick_font(text):
    if re.search(r"[\u0980-\u09FF]", text):
        return "Noto Sans Bengali"
    if re.search(r"[\u0900-\u097F]", text):
        return "Noto Sans Devanagari"
    if re.search(r"[\u0600-\u06FF]", text):
        return "Noto Naskh Arabic"
    return "DejaVu Sans"


def _esc(t):
    return (t.replace("\\", "").replace("{", "").replace("}", "")
             .replace("\r", " ").replace("\n", " ").strip())


def _ts(sec):
    if sec < 0:
        sec = 0
    h = int(sec // 3600); m = int((sec % 3600) // 60)
    s = int(sec % 60); cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        s += 1; cs = 0
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


# রঙিন পপ ক্যারাওকে স্টাইল (BGR: &HAABBGGRR)
# spoken = উজ্জ্বল হলুদ, unspoken = সাদা, আউটলাইন = কালো
STYLE_PRIMARY   = "&H0000FFFF"   # হলুদ (বলা শব্দ)
STYLE_SECONDARY = "&H00FFFFFF"   # সাদা (যে শব্দ এখনো বলা হয়নি)
STYLE_OUTLINE   = "&H00000000"
STYLE_BACK      = "&H80000000"


def build_ass(words, out_path, playres=(1920, 1080), mode="yt", cfg=None,
              max_chars=40, max_words=5, max_gap=0.9):
    cfg = cfg or {}
    W, H = playres
    text_all = " ".join(w["text"] for w in words[:400])
    font = pick_font(text_all)
    size = 58 if mode == "yt" else 46
    margin_v = 92 if mode == "yt" else 70
    if not words:
        open(out_path, "w", encoding="utf-8").write("")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: SSK,{font},{size},{STYLE_PRIMARY},{STYLE_SECONDARY},{STYLE_OUTLINE},{STYLE_BACK},-1,0,0,0,100,100,0,0,1,4,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    chunk, cur_len = [], 0

    def flush(ch):
        if not ch:
            return ""
        start = max(0.0, ch[0]["start"] - 0.08)
        end = ch[-1]["end"] + 0.30
        if end - start < 0.55:
            end = start + 0.55
        parts = []
        for i, w in enumerate(ch):
            nxt = ch[i + 1]["start"] if i + 1 < len(ch) else w["end"]
            k = int(round(max(0.06, nxt - w["start"]) * 100))
            parts.append(f"{{\\k{k}}}{_esc(w['text'])}")
        body = " ".join(parts)
        # পপ-ইন অ্যানিমেশন + ফেড
        body = "{\\fad(70,70)\\fscx86\\fscy86\\t(0,150,\\fscx100\\fscy100)}" + body
        return (f"Dialogue: 0,{_ts(start)},{_ts(end)},SSK,,0,0,0,,{body}\n")

    for w in words:
        gap = w["start"] - chunk[-1]["end"] if chunk else 0
        if chunk and (gap > max_gap or len(chunk) >= max_words
                      or cur_len + len(w["text"]) + 1 > max_chars):
            lines.append(flush(chunk))
            chunk, cur_len = [], 0
        chunk.append(w)
        cur_len += len(w["text"]) + 1
    lines.append(flush(chunk))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    print(f"[ass] {out_path} লেখা হলো ({len(words)} শব্দ)")
    return out_path
