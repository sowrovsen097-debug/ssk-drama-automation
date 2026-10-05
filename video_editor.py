"""FFmpeg-ভিত্তিক ভিডিও এডিটর।
16:9 → YouTube (color grade + subtitle burn)
1:1  → Facebook (blurred background pad — মূল ভিডিও ক্লিয়ার, কিছু কাটা যায় না)
ভিডিও ৩০ মিনিটের বেশি হলে শেষ ১৫% কেটে ছোট করা হয়।
"""
import os
import subprocess
import json

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def get_duration(path):
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "format=duration", "-of", "json", path]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def trim_to_target_length(input_path, output_path):
    """৩০ মিনিটের বেশি হলে শেষ ১৫% কেটে ২৫–৩০ মিনিটে আনা"""
    dur = get_duration(input_path)
    if dur <= 1800:
        return input_path
    keep_seconds = int(dur * 0.85)
    cmd = [FFMPEG, "-y", "-i", input_path,
           "-t", str(keep_seconds), "-c", "copy", output_path]
    subprocess.run(cmd, check=True)
    return output_path


def edit_for_youtube(input_path, sub_path, output_path):
    """16:9 এডিট — color grade + subtitle burn; সব ফ্রেম ক্লিয়ার"""
    sub_filter = (
        f"subtitles={sub_path}:force_style='Fontname=Arial,Fontsize=22,"
        f"PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=3,"
        f"Outline=2,Shadow=1,MarginV=40'"
    ) if sub_path and os.path.exists(sub_path) else "null"

    vf = (
        "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,"
        "eq=brightness=0.04:contrast=1.08:saturation=1.12:gamma=1.02,"
        "hue=h=2:s=1.05,"
    )
    if sub_filter != "null":
        vf += sub_filter + ","
    vf += "format=yuv420p[v]"

    cmd = [FFMPEG, "-y", "-i", input_path,
           "-filter_complex", vf,
           "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-crf", "22", "-preset", "veryfast",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", output_path]
    subprocess.run(cmd, check=True)


def edit_for_facebook(input_path, sub_path, output_path):
    """1:1 এডিট — blurred background pad + foreground (মূল ভিডিও ক্লিয়ার)"""
    sub_filter = (
        f"subtitles={sub_path}:force_style='Fontname=Arial,Fontsize=20,"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=4,"
        f"Outline=2,Shadow=1,MarginV=30,Alignment=2'"
    ) if sub_path and os.path.exists(sub_path) else "null"

    vf = (
        "[0:v]split[orig][copy];"
        "[copy]scale=720:720:force_original_aspect_ratio=increase,"
        "crop=720:720,gblur=sigma=20[bg];"
        "[orig]scale=-2:720[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        "eq=brightness=0.04:contrast=1.08:saturation=1.15,"
        "hue=h=-2:s=1.05,"
    )
    if sub_filter != "null":
        vf += sub_filter + ","
    vf += "format=yuv420p[v]"

    cmd = [FFMPEG, "-y", "-i", input_path,
           "-filter_complex", vf,
           "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-crf", "22", "-preset", "veryfast",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", output_path]
    subprocess.run(cmd, check=True)
