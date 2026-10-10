# subtitle.py — Whisper সাবটাইটেল এবং .ass জেনারেটর
import os, io, glob

PALETTE = ["&H0000FFFF", "&H0000FF00", "&H00FF7BFF", "&H00FFC800", "&H00FFFFFF", "&H00A5FF7F"]
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
]

def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p): return p
    hit = glob.glob("/usr/share/fonts/**/*Bold*.ttf", recursive=True)
    return hit[0] if hit else ""

def _ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def transcribe(path, model_name="base", language="auto", log=print):
    try:
        from faster_whisper import WhisperModel
        log(f"whisper: faster-whisper {model_name} (int8, cpu)")
        m = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=max(os.cpu_count() or 2, 2))
        kw = {"beam_size": 1, "vad_filter": True, "word_timestamps": True}
        if language and language != "auto": kw["language"] = language
        segs, info = m.transcribe(path, **kw)
        words, text = [], []
        for s in segs:
            text.append(s.text.strip())
            for w in (s.words or []):
                words.append({"start": float(w.start), "end": float(w.end), "word": w.word})
        return {"words": words, "text": " ".join(text), "engine": "faster-whisper", "language": getattr(info, "language", language or "auto")}
    except Exception as e:
        log(f"faster-whisper ব্যর্থ ({str(e)[:120]}) → openai-whisper fallback")
        import whisper
        log(f"whisper: openai-whisper {model_name}")
        r = whisper.load_model(model_name).transcribe(path, word_timestamps=True, fp16=False, language=None if language in (None, "auto") else language)
        words = []
        for seg in r.get("segments", []):
            for w in seg.get("words", []) or []:
                words.append({"start": float(w["start"]), "end": float(w["end"]), "word": w.get("word", "")})
        return {"words": words, "text": r.get("text", ""), "engine": "openai-whisper", "language": r.get("language", language or "auto")}

def group_lines(words, max_words=4, max_chars=30, max_dur=3.2, gap=0.7):
    lines, cur = [], []
    for w in words or []:
        txt = str(w.get("word", "")).strip()
        if not txt: continue
        if cur:
            too_many = len(cur) >= max_words
            too_long = sum(len(x["word"].strip()) + 1 for x in cur) + len(txt) > max_chars
            too_slow = float(w["start"]) - float(cur[0]["start"]) > max_dur
            big_gap = float(w["start"]) - float(cur[-1]["end"]) > gap
            if too_many or too_long or too_slow or big_gap:
                lines.append(cur); cur = []
        cur.append(w)
    if cur: lines.append(cur)
    out = []
    for ln in lines:
        txt = " ".join(x["word"].strip() for x in ln).strip()
        if not txt: continue
        out.append({"start": float(ln[0]["start"]), "end": max(float(ln[-1]["end"]), float(ln[0]["start"]) + 0.3), "words": ln, "text": txt})
    return out

def build_ass(lines, path, w=1920, h=1080, colorful=True, brand="", intro_dur=3.0, outro_dur=5.0):
    font = os.path.basename(find_font() or "DejaVuSans-Bold.ttf").replace(".ttf", "").replace("-", " ")
    size = int(max(w, h) * 0.045); mv = int(h * 0.055); o = int(max(2, size * 0.08))
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: MAIN,{font},{size},&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,-1,0,0,0,100,100,1.2,0,1,{o},2,2,60,60,{mv},1",
        f"Style: BRAND,{font},{int(size*1.15)},&H00FFFFFF,&H00FFFFFF,&H00202020,&HA0000000,-1,0,0,0,100,100,2,0,1,3,3,5,80,80,60,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    ev = []
    if brand:
        ev.append(f"Dialogue: 0,{_ass_time(0.4)},{_ass_time(max(1.2, intro_dur - 0.2))},BRAND,,0,0,0,,{{\\fad(250,600)\\pos({w/2},{h*0.16})}}{brand}")
    for i, ln in enumerate(lines):
        if colorful:
            text = " ".join(f"{{\\c{PALETTE[(i + k) % len(PALETTE)]}}}{wd['word'].strip()}" for k, wd in enumerate(ln["words"]))
        else:
            text = ln["text"]
        tags = "{\\fad(70,90)\\t(0,110,\\fscx112\\fscy112)\\t(110,220,\\fscx100\\fscy100)}"
        ev.append(f"Dialogue: 0,{_ass_time(ln['start'])},{_ass_time(ln['end'])},MAIN,,0,0,0,,{tags}{text}")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + ev) + "\n")
    return path, len(lines)
