"""Word-by-word colourful ASS (karaoke) subtitles, in the video's own language.

Whisper gives word timings on the edited timeline; this module styles them.
"""
from __future__ import annotations

import re

# ASS uses &HBBGGRR — a rainbow-ish palette that stays readable on video
PALETTE = [
    "&H0000FFFF",  # bright yellow
    "&H00FFFF00",  # cyan
    "&H0000FF00",  # green
    "&H00FF00FF",  # magenta
    "&H0080FFFF",  # light gold
    "&H00FF8C00",  # sky blue
    "&H00FFFFFF",  # white
    "&H0080FF80",  # light green
]
OUTLINE = "&H00101010"


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    cents = int(round((seconds - int(seconds)) * 100))
    if cents == 100:
        cents = 99
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def safe_text(value: str) -> str:
    return (str(value or "").replace("\\", "＼").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())


def remap_words(segments, intervals, offset_speed: float = 1.0):
    """Move whisper word timings from the source timeline onto the edited timeline."""
    out = []
    for segment in segments or []:
        for word in segment.get("words", []) or []:
            start = float(word.get("start") or 0.0)
            end = float(word.get("end") or start)
            shift = 0.0
            for kept_start, kept_end in intervals:
                if kept_start <= start < kept_end:
                    new_start = (start - kept_start) / offset_speed
                    new_end = min(end, kept_end)
                    new_end = (new_end - kept_start) / offset_speed
                    out.append({"text": str(word.get("word") or "").strip(),
                                "start": shift + new_start,
                                "end": shift + max(new_end, new_start + 0.06)})
                shift += (kept_end - kept_start) / offset_speed
    return [w for w in out if w["text"]]


def group_words(words, max_words: int = 5, max_span: float = 3.2, gap: float = 0.75):
    groups, current = [], []
    previous_end = None
    for word in words:
        if current and (len(current) >= max_words
                        or word["start"] - current[0]["start"] > max_span
                        or (previous_end is not None and word["start"] - previous_end > gap)):
            groups.append(current)
            current = []
        current.append(word)
        previous_end = word["end"]
    if current:
        groups.append(current)
    return groups


def write_ass(words, output: str, width: int, height: int, title_color: bool = True) -> str:
    """Write a styled ASS file sized for the target canvas."""
    portrait = height >= width
    font_size = 44 if portrait else 40
    margin_v = 150 if portrait else 66
    margin_h = 70 if portrait else 90
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Bengali,{font_size},&H00FFFFFF,&H00FFFFFF,{OUTLINE},&H64000000,-1,0,0,0,100,100,0.4,0,1,3.4,1.6,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    index = 0
    for group in group_words(words):
        if not group:
            continue
        start = group[0]["start"]
        end = group[-1]["end"]
        chunks = []
        for position, word in enumerate(group):
            duration_cs = max(6, int(round((word["end"] - word["start"]) * 100)))
            if title_color:
                color = PALETTE[index % len(PALETTE)]
                chunks.append("{\\1c%s\\fscx106\\fscy106\\kf%d}%s" % (color, duration_cs, safe_text(word["text"])))
            else:
                chunks.append("{\\kf%d}%s" % (duration_cs, safe_text(word["text"])))
            index += 1
        text = "{\\fad(70,70)}" + " ".join(chunks)
        lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (ass_time(start), ass_time(end + 0.12), text))
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(lines) + "\n")
    return output
