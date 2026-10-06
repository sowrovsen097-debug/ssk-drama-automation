import datetime as dt
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

import cv2
import requests

from core import ai, clean_error
from subtitle_generator import remap_words, write_ass

DEADLINE = float("inf")


def run(args, capture=False):
    remaining = DEADLINE - time.time()

    if remaining <= 5:
        raise TimeoutError("Night editing window ended")

    return subprocess.run(
        args,
        check=True,
        text=True,
        timeout=min(remaining, 15000),
        stdout=(
            subprocess.PIPE
            if capture
            else subprocess.DEVNULL
        ),
        stderr=subprocess.PIPE,
    )


def probe(file):
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(file),
        ],
        True,
    )

    return json.loads(result.stdout)


def scenes(file, duration):
    capture = cv2.VideoCapture(str(file))

    edges = [0.0]
    motion = []
    previous = None

    try:
        for second in range(0, int(duration), 2):
            if time.time() >= DEADLINE:
                raise TimeoutError(
                    "Editing window ended during analysis"
                )

            capture.set(
                cv2.CAP_PROP_POS_MSEC,
                second * 1000,
            )

            ok, frame = capture.read()

            if not ok:
                continue

            gray = cv2.cvtColor(
                cv2.resize(frame, (320, 180)),
                cv2.COLOR_BGR2GRAY,
            )

            value = (
                0
                if previous is None
                else float(
                    cv2.absdiff(gray, previous).mean()
                )
            )

            if value > 28 and second - edges[-1] >= 8:
                edges.append(float(second))

            motion.append((second, value))
            previous = gray

    finally:
        capture.release()

    edges.append(duration)

    # Split long scenes into manageable chronological units.
    split = [0.0]

    for end in edges[1:]:
        while end - split[-1] > 45:
            split.append(split[-1] + 30)

        if end - split[-1] > 0.1:
            split.append(end)

    return list(zip(split, split[1:])), motion


def silence_ranges(file, segments, duration, budget):
    result = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(file),
            "-vn",
            "-af",
            "silencedetect=noise=-35dB:d=1.2",
            "-f",
            "null",
            "-",
        ],
        True,
    )

    starts = [
        float(value)
        for value in re.findall(
            r"silence_start: ([0-9.]+)",
            result.stderr,
        )
    ]

    ends = [
        float(value)
        for value in re.findall(
            r"silence_end: ([0-9.]+)",
            result.stderr,
        )
    ]

    if len(ends) < len(starts):
        ends.append(duration)

    cuts = []
    used = 0.0

    for a, b in zip(starts, ends):
        a = max(10.0, a + 0.25)
        b = min(duration - 10, b - 0.25)

        if b <= a or used + b - a > budget:
            continue

        if any(
            segment["start"] < b
            and segment["end"] > a
            for segment in segments
        ):
            continue

        cuts.append((a, b))
        used += b - a

    return cuts


def choose_intervals(file, segments, config, duration):
    intervals, motion = scenes(file, duration)
    units = []

    for index, (a, b) in enumerate(intervals):
        speech = sum(
            max(
                0,
                min(b, segment["end"])
                - max(a, segment["start"]),
            )
            for segment in segments
        )

        text = " ".join(
            segment["text"].strip()
            for segment in segments
            if segment["start"] < b
            and segment["end"] > a
        )

        values = [
            value
            for second, value in motion
            if a <= second < b
        ]

        units.append({
            "id": index,
            "start": a,
            "end": b,
            "text": text[:700],
            "speech": min(1, speech / (b - a)),
            "motion": sum(values) / max(1, len(values)),
        })

    approved = set()
    ai_error = None

    if (
        config["editing"]["ai_scene_review"]
        and DEADLINE - time.time() > 180
    ):
        try:
            capture = cv2.VideoCapture(str(file))
            images = []

            try:
                for fraction in (
                    0.08,
                    0.22,
                    0.38,
                    0.54,
                    0.70,
                    0.86,
                ):
                    capture.set(
                        cv2.CAP_PROP_POS_MSEC,
                        fraction * duration * 1000,
                    )

                    ok, frame = capture.read()

                    if ok:
                        ok, jpg = cv2.imencode(
                            ".jpg",
                            cv2.resize(frame, (480, 270)),
                        )

                        if ok:
                            images.append(jpg.tobytes())

            finally:
                capture.release()

            response = ai(
                config,
                (
                    "You are a cautious drama editor. "
                    "Review chronological scene transcript units. "
                    "Identify ONLY units removable without losing "
                    "dialogue meaning, plot clues, relationships "
                    "or ending. Images are evenly spaced context "
                    "frames, not unit frames. Return JSON "
                    '{"removable_ids":[integer ids]}. '
                    "Be conservative. Units:\n"
                    + json.dumps(
                        units,
                        ensure_ascii=False,
                    )
                ),
                images,
                True,
            )

            approved = {
                int(value)
                for value in response.get(
                    "removable_ids",
                    [],
                )
            }

        except Exception as exc:
            ai_error = clean_error(exc)

    budget = duration * (
        1 - config["editing"]["keep_ratio"]
    )

    removable = [
        unit
        for unit in units[1:-1]
        if unit["speech"] < 0.15
        or unit["id"] in approved
    ]

    removable.sort(
        key=lambda unit: (
            unit["speech"],
            unit["motion"],
        )
    )

    cuts = silence_ranges(
        file,
        segments,
        duration,
        budget,
    )

    removed = set()
    used = sum(b - a for a, b in cuts)

    for unit in removable:
        length = unit["end"] - unit["start"]

        overlaps_existing_cut = any(
            a < unit["end"]
            and b > unit["start"]
            for a, b in cuts
        )

        if (
            used + length <= budget
            and not overlaps_existing_cut
        ):
            crosses_sentence = any(
                segment["start"]
                < unit["start"]
                < segment["end"]
                or segment["start"]
                < unit["end"]
                < segment["end"]
                for segment in segments
            )

            if crosses_sentence:
                continue

            removed.add(unit["id"])
            cuts.append(
                (unit["start"], unit["end"])
            )
            used += length

    keep = []
    cursor = 0.0

    for a, b in sorted(cuts):
        if a > cursor:
            keep.append((cursor, a))

        cursor = max(cursor, b)

    if cursor < duration:
        keep.append((cursor, duration))

    edited = duration - used

    return keep, {
        "source_seconds": duration,
        "edited_seconds": edited,
        "removed_units": len(removed),
        "silence_cuts": len(cuts) - len(removed),
        "ai_review_error": ai_error,
        "target_met": (
            duration * 0.75
            <= edited
            <= duration * 0.875
        ),
    }


def music(config, folder, seed):
    key = os.getenv("FREESOUND_API_KEY")

    if (
        not key
        or not config["editing"]["music_enabled"]
    ):
        return None, None

    response = requests.get(
        "https://freesound.org/apiv2/search/text/",
        headers={
            "Authorization": "Token " + key
        },
        params={
            "query": config["editing"]["music_query"],
            "filter": (
                'license:"Creative Commons 0" '
                "duration:[20 TO 600]"
            ),
            "fields": "id,name,license,previews,url",
            "page_size": 30,
        },
        timeout=45,
    )

    response.raise_for_status()

    sounds = [
        sound
        for sound in response.json().get("results", [])
        if sound.get(
            "license",
            "",
        ).rstrip("/").endswith("/zero/1.0")
        or sound.get("license") == "Creative Commons 0"
    ]

    if not sounds:
        raise RuntimeError(
            "No matching CC0 music was returned"
        )

    sound = random.Random(seed).choice(sounds)
    url = sound["previews"]["preview-hq-mp3"]
    path = folder / "music.mp3"

    response = requests.get(
        url,
        timeout=(20, 90),
    )

    response.raise_for_status()
    path.write_bytes(response.content)

    return path, {
        "name": sound["name"],
        "url": sound["url"],
        "license": sound["license"],
    }


def render(
    input_file,
    platform,
    config,
    folder,
    deadline,
):
    global DEADLINE
    DEADLINE = deadline

    data = probe(input_file)

    if not any(
        stream["codec_type"] == "audio"
        for stream in data["streams"]
    ):
        raise ValueError(
            "Source video has no audio for subtitles"
        )

    duration = float(
        data["format"]["duration"]
    )

    audio = folder / "audio.wav"

    run([
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(input_file),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio),
    ])

    # Run Whisper separately so transcription can be timed out.
    transcript = folder / "transcript.json"

    run([
        "python",
        "-m",
        "editor",
        "transcribe",
        str(audio),
        str(transcript),
        config["editing"]["whisper_model"],
    ])

    segments = json.loads(
        transcript.read_text()
    )["segments"]

    intervals, report = choose_intervals(
        input_file,
        segments,
        config,
        duration,
    )

    parts = []

    for index, (a, b) in enumerate(intervals):
        part = folder / f"part-{index:03d}.mp4"

        run([
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(a),
            "-i",
            str(input_file),
            "-t",
            str(b - a),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            "fps=25,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(part),
        ])

        parts.append(part)

    listing = folder / "concat.txt"

    listing.write_text(
        "\n".join(
            "file '"
            + part.resolve().as_posix()
            + "'"
            for part in parts
        )
    )

    joined = folder / "joined.mp4"

    run([
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        str(joined),
    ])

    width, height = (
        (1280, 720)
        if platform == "youtube"
        else (1080, 1080)
    )

    ass = folder / "captions.ass"

    write_ass(
        remap_words(segments, intervals),
        ass,
        width,
        height,
    )

    bgm = None
    music_info = None
    music_error = None

    try:
        bgm, music_info = music(
            config,
            folder,
            folder.name,
        )

    except Exception as exc:
        music_error = clean_error(exc)

    # Crop only the background, never the foreground frame.
    graph = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={width}:{height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        "boxblur=20:2[back];"
        f"[fg]scale={width}:{height}:"
        "force_original_aspect_ratio=decrease[front];"
        "[back][front]overlay=(W-w)/2:(H-h)/2,"
        "setsar=1,"
        "eq=contrast=1.03:saturation=1.04,"
        f"subtitles='{ass.resolve().as_posix()}',"
        "drawtext=font='Noto Sans':"
        "text='SSK DRAMA':"
        "x=w-tw-24:y=24:"
        "fontsize=24:fontcolor=white@0.8:"
        "shadowx=2:shadowy=2[v];"
        "[0:a]afftdn=nf=-25,"
        "loudnorm=I=-16:TP=-1.5:LRA=11,"
        "aresample=48000,"
        "aformat=channel_layouts=stereo[voice];"
    )

    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(joined),
    ]

    if bgm:
        args += [
            "-stream_loop",
            "-1",
            "-i",
            str(bgm),
        ]

        graph += (
            "[voice]asplit=2[main][side];"
            "[1:a]volume=0.10,"
            "aresample=48000,"
            "aformat=channel_layouts=stereo[music];"
            "[music][side]sidechaincompress="
            "threshold=0.02:ratio=8:"
            "attack=20:release=400[duck];"
            "[main][duck]amix=inputs=2:"
            "duration=first:normalize=0,"
            "alimiter=limit=0.95[a]"
        )

    else:
        graph += "[voice]anull[a]"

    output = folder / (
        platform + "-ready.mp4"
    )

    cap_kbps = max(
        300,
        min(
            3500,
            int(
                1.8 * 1024**3 * 8
                / report["edited_seconds"]
                / 1000
            ) - 192,
        ),
    )

    run(args + [
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-crf",
        "21",
        "-maxrate",
        f"{cap_kbps}k",
        "-bufsize",
        f"{cap_kbps * 2}k",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-t",
        str(report["edited_seconds"]),
        str(output),
    ])

    final = probe(output)

    video_stream = next(
        stream
        for stream in final["streams"]
        if stream["codec_type"] == "video"
    )

    actual = float(
        final["format"]["duration"]
    )

    if (
        (
            video_stream["width"],
            video_stream["height"],
        ) != (width, height)
        or abs(
            actual - report["edited_seconds"]
        ) > 2
    ):
        raise RuntimeError(
            "Output dimensions/duration validation failed"
        )

    report.update({
        "actual_seconds": actual,
        "music": music_info,
        "music_error": music_error,
    })

    transcript_text = " ".join(
        segment["text"]
        for segment in segments
    )[:15000]

    return output, report, transcript_text


if __name__ == "__main__":
    import sys
    import whisper

    if (
        len(sys.argv) != 5
        or sys.argv[1] != "transcribe"
    ):
        raise SystemExit(
            "Usage: python -m editor transcribe AUDIO JSON MODEL"
        )

    model = whisper.load_model(
        sys.argv[4]
    )

    result = model.transcribe(
        sys.argv[2],
        word_timestamps=True,
        fp16=False,
        verbose=False,
    )

    Path(sys.argv[3]).write_text(
        json.dumps(
            result,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
