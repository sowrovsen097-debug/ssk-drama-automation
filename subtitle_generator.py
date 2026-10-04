import whisper
import subprocess
import os

def generate_subtitles_and_burn_to_video(video_path, output_path):
    print("⏳ Step 1: Extracting Audio from Video...")
    audio_path = "temp_audio.wav"
    # ভিডিও থেকে অডিও আলাদা করা
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path, 
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', audio_path
    ], check=True)

    print("⏳ Step 2: Loading Whisper Model & Transcribing...")
    # 'base' বা 'tiny' মডেল লোড করা (GitHub Actions-এ দ্রুত রান হয়)
    model = whisper.load_model("base")
    result = model.transcribe(audio_path, word_timestamps=True)

    print("⏳ Step 3: Generating SRT Subtitle File...")
    srt_path = "subtitles.srt"
    
    # SRT ফাইল ফরম্যাট তৈরি করা
    with open(srt_path, "w", encoding="utf-8") as f:
        segment_id = 1
        for segment in result['segments']:
            start = format_timestamp(segment['start'])
            end = format_timestamp(segment['end'])
            text = segment['text'].strip()
            
            f.write(f"{segment_id}\n")
            f.write(f"{start} --> {end}\n")
            f.write(f"{text}\n\n")
            segment_id += 1

    print("⏳ Step 4: Hardcoding Subtitles onto Video with Style...")
    # FFmpeg দিয়ে ভিডিওর গায়ে সুন্দর হলুদ স্টাইলের সাবটাইটেল বসানো
    ffmpeg_cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vf', f"subtitles={srt_path}:force_style='Fontname=Arial,Fontsize=18,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=1,Shadow=1,MarginV=30'",
        '-c:a', 'copy',
        output_path
    ]
    subprocess.run(ffmpeg_cmd, check=True)

    # টেম্পোরারি ফাইলগুলো মুছে ফেলা
    if os.path.exists(audio_path): os.remove(audio_path)
    if os.path.exists(srt_path): os.remove(srt_path)
    print("✅ Subtitle Added Successfully!")

def format_timestamp(seconds):
    """সেকেন্ডকে SRT টাইমস্ট্যাম্প ফরম্যাটে (HH:MM:SS,mmm) রূপান্তর করে"""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    msecs = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{msecs:03d}"

# টেস্ট করার জন্য
if __name__ == "__main__":
    generate_subtitles_and_burn_to_video("input_video.mp4", "output_video_with_subtitles.mp4")
