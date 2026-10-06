from pathlib import Path

COLORS = [
    "&H00FFFF&",
    "&HFFFF00&",
    "&HFF80FF&",
    "&H80FF80&",
]


def ass_time(seconds):
    value = max(0, round(seconds * 100))

    return (
        f"{value // 360000}:"
        f"{value // 6000 % 60:02d}:"
        f"{value // 100 % 60:02d}."
        f"{value % 100:02d}"
    )


def safe_text(text):
    return (
        str(text)
        .replace("\\", " ")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
    )


def remap_words(segments, intervals):
    result = []
    offset = 0.0

    for start, end in intervals:
        for segment in segments:
            words = segment.get("words") or [{
                "start": segment["start"],
                "end": segment["end"],
                "word": segment.get("text", ""),
            }]

            for word in words:
                a = float(word["start"])
                b = float(word["end"])

                if a >= start and b <= end and b > a:
                    result.append({
                        "start": offset + a - start,
                        "end": offset + b - start,
                        "word": word["word"],
                    })

        offset += end - start

    return result


def write_ass(words, output, width, height):
    size = 46 if width > height else 42
    margin = 64 if width > height else 135

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Bengali,{size},&H0000FFFF,&H00FFFFFF,&H00202020,&H90000000,1,0,0,0,100,100,0,0,1,3,1,2,65,65,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []
    group = []

    def flush():
        if not group:
            return

        start = group[0]["start"]
        end = group[-1]["end"]
        color = COLORS[len(lines) % len(COLORS)]

        body = "{\\1c" + color + "\\fad(70,70)}"

        for index, word in enumerate(group):
            # Include gaps between words in the karaoke timing.
            until = (
                group[index + 1]["start"]
                if index + 1 < len(group)
                else end
            )

            duration = max(
                1,
                round((until - word["start"]) * 100),
            )

            body += (
                "{\\kf"
                + str(duration)
                + "}"
                + safe_text(word["word"]).strip()
                + " "
            )

        lines.append(
            f"Dialogue: 0,{ass_time(start)},"
            f"{ass_time(end)},Default,,0,0,0,,{body}"
        )

    for word in words:
        if group and (
            len(group) >= 6
            or word["end"] - group[0]["start"] > 3.5
            or word["start"] - group[-1]["end"] > 0.75
        ):
            flush()
            group = []

        group.append(word)

    flush()

    Path(output).write_text(
        header + "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    return str(output)
