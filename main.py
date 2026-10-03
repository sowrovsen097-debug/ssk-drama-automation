import os
import json
import time
import datetime
import subprocess
import requests
import yt_dlp
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Environment Variables থেকে Secrets লোড করা
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Gemini AI কনফিগারেশন
genai.configure(api_key=GEMINI_API_KEY)

HISTORY_FILE = "history.json"
CONFIG_FILE = "config.json"

def send_telegram(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=payload)
        except Exception as e:
            print(f"Telegram alert error: {e}")

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

def save_history(video_id):
    history = load_history()
    if video_id not in history:
        history.append(video_id)
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def get_60day_old_video(channel_url, history):
    ydl_opts = {
        'extract_flat': True,
        'playlistend': 30,
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
        entries = info.get('entries', [])
        
    now = datetime.datetime.now(datetime.timezone.utc)
    for entry in entries:
        video_id = entry.get('id')
        if video_id in history:
            continue
            
        # বিস্তারিত তথ্য আনা
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl_inner:
            v_info = ydl_inner.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            upload_date_str = v_info.get('upload_date') # YYYYMMDD
            if upload_date_str:
                upload_date = datetime.datetime.strptime(upload_date_str, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
                days_old = (now - upload_date).days
                if days_old >= 60:
                    return v_info
    return None

def download_video(video_url):
    output_template = "input_video.%(ext)s"
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_template,
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])
    
    for f in os.listdir('.'):
        if f.startswith('input_video'):
            return f
    return None

def process_video_ffmpeg(input_file):
    output_file = "processed_video.mp4"
    # FFmpeg Anti-Copyright Processing: Trim start/end, Crop 2%, Flip horizontally, Color Grade
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-ss', '00:00:10', # শুরু থেকে ১০ সেকেণ্ড কাটা
        '-i', input_file,
        '-vf', "crop=iw*0.96:ih*0.96,hflip,eq=brightness=0.03:contrast=1.1:saturation=1.15",
        '-c:v', 'libx264', '-crf', '23', '-preset', 'fast',
        '-c:a', 'aac', '-b:a', '128k',
        output_file
    ]
    subprocess.run(ffmpeg_cmd, check=True)
    return output_file

def generate_seo_metadata(original_title, original_desc):
    prompt = f"""
    Analyze this drama video title and description to generate a brand new, highly engaging Title and SEO Description with viral hashtags in Bengali.
    Original Title: {original_title}
    Original Description: {original_desc}
    
    Output Format (JSON strictly):
    {{
      "title": "New Catchy Bengali Title",
      "description": "Engaging Bengali description with #hashtags"
    }}
    """
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    try:
        text = response.text.strip().replace('```json', '').replace('```', '')
        return json.loads(text)
    except:
        return {
            "title": f"নতুন নাটক বিশেষ পর্ব - {original_title[:30]}",
            "description": "উপভোগ করুন আজকের বিশেষ নাটক পর্ব। লাইক ও সাবস্ক্রাইব করে সাথে থাকুন! #BanglaDrama #SSKDrama"
        }

def upload_to_facebook(video_path, title, description):
    url = f"https://graph-video.facebook.com/v18.0/{FB_PAGE_ID}/videos"
    payload = {
        'title': title,
        'description': description,
        'access_token': FB_ACCESS_TOKEN
    }
    with open(video_path, 'rb') as f:
        files = {'source': f}
        response = requests.post(url, data=payload, files=files)
    return response.json()

def upload_to_youtube(video_path, title, description):
    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET
    )
    youtube = build("youtube", "v3", credentials=creds)
    
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "categoryId": "24" # Entertainment
        },
        "status": {
            "privacyStatus": "public"
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    return response

def cleanup():
    for f in os.listdir('.'):
        if f.startswith('input_video') or f == 'processed_video.mp4':
            try:
                os.remove(f)
            except:
                pass

def main():
    config = load_config()
    history = load_history()
    
    fb_channels = config.get("facebook_channels", [])
    yt_channels = config.get("youtube_channels", [])
    
    # Process Facebook Target
    for ch in fb_channels:
        v_info = get_60day_old_video(ch, history)
        if v_info:
            v_id = v_info['id']
            send_telegram(f"⏳ Downloading video for Facebook: {v_info.get('title')}")
            raw_file = download_video(f"https://www.youtube.com/watch?v={v_id}")
            if raw_file:
                send_telegram("✂️ Editing video (FFmpeg anti-copyright filter)...")
                edited_file = process_video_ffmpeg(raw_file)
                seo = generate_seo_metadata(v_info.get('title', ''), v_info.get('description', ''))
                
                send_telegram("🚀 Uploading to Facebook Page...")
                res = upload_to_facebook(edited_file, seo['title'], seo['description'])
                
                save_history(v_id)
                cleanup()
                send_telegram(f"✅ Facebook Upload Success: {seo['title']}")
                break

    # Process YouTube Target
    for ch in yt_channels:
        v_info = get_60day_old_video(ch, history)
        if v_info:
            v_id = v_info['id']
            send_telegram(f"⏳ Downloading video for YouTube: {v_info.get('title')}")
            raw_file = download_video(f"https://www.youtube.com/watch?v={v_id}")
            if raw_file:
                send_telegram("✂️ Editing video (FFmpeg anti-copyright filter)...")
                edited_file = process_video_ffmpeg(raw_file)
                seo = generate_seo_metadata(v_info.get('title', ''), v_info.get('description', ''))
                
                send_telegram("🚀 Uploading to YouTube Channel...")
                res = upload_to_youtube(edited_file, seo['title'], seo['description'])
                
                save_history(v_id)
                cleanup()
                send_telegram(f"✅ YouTube Upload Success: {seo['title']}")
                break

if __name__ == "__main__":
    main()
