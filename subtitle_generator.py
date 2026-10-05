"""Whisper ASR দিয়ে SRT subtitle ফাইল তৈরি।"""
import os
import subprocess
import whisper


def format_timestamp(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    msecs = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{msecs:03d}"


def generate_subtitles(video_path, srt_path):
    audio_path = "temp_audio.wav"
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-vn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path
    ], check=True, capture_output=True)

    model = whisper.load_model("base")
    result = model.transcribe(audio_path, word_timestamps=True)

    with open(srt_path, "w", encoding="utf-8") as f:
        seg_id = 1
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            start = format_timestamp(seg["start"])
            end   = format_timestamp(seg["end"])
            f.write(f"{seg_id}\n{start} --> {end}\n{text}\n\n")
            seg_id += 1

    if os.path.exists(audio_path):
        os.remove(audio_path)
    return srt_path
