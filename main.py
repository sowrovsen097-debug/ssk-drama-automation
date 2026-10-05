"""SSK Drama Daily Edit Orchestrator
বাংলাদেশ সময় রাত ০১:০০টায় (UTC ১৯:০০) চলে।
৬টি সোর্স চ্যানেল (৩ FB + ৩ YT) থেকে ৬০ দিনের পুরনো ভিডিও ডাউনলোড +
এডিট + GitHub Release-এ সংরক্ষণ করে।
"""
import os
import json
import datetime
import subprocess
import requests
import yt_dlp
import google.generativeai as genai

from video_editor import edit_for_youtube, edit_for_facebook
from subtitle_generator import generate_subtitles
from release_manager import (
    get_release, create_release, upload_asset,
    download_manifest_text, upload_manifest_text,
)

YOUTUBE_API_KEY     = os.getenv("YOUTUBE_API_KEY")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
FREESOUND_API_KEY   = os.getenv("FREESOUND_API_KEY")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
GITHUB_REPO         = os.getenv("GITHUB_REPOSITORY")

genai.configure(api_key=GEMINI_API_KEY)

CONFIG_FILE  = "config.json"
HISTORY_FILE = "history.json"
TG_API = "https://api.telegram.org/bot" + (TELEGRAM_BOT_TOKEN or "")


def notify(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"{TG_API}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                          timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def load_json(p, default):
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(p, data):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_old_videos(channel_url, history, min_age_days=60):
    """yt-dlp দিয়ে min_age_days এর চেয়ে পুরনো ভিডিও (প্রতি চ্যানেলে সর্বোচ্চ ২টি)"""
    opts = {"extract_flat": True, "playlistend": 30, "quiet": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
        entries = info.get("entries") or []
    except Exception as e:
        print("Channel fetch failed:", channel_url, e)
        return []

    out = []
    now = datetime.datetime.now(datetime.timezone.utc)
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        for e in entries:
            vid = e.get("id")
            if not vid or vid in history:
                continue
            try:
                full = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={vid}", download=False)
                up = full.get("upload_date")
                if up:
                    dt = datetime.datetime.strptime(up, "%Y%m%d").replace(
                        tzinfo=datetime.timezone.utc)
                    age = (now - dt).days
                    if age >= min_age_days:
                        out.append(full)
                        if len(out) >= 2:
                            break
            except Exception:
                continue
    return out


def download_video(url):
    out_tpl = "raw_input.%(ext)s"
    opts = {
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_tpl, "quiet": True, "noplaylist": True,
        "max_filesize": 500 * 1024 * 1024,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    for f in os.listdir("."):
        if f.startswith("raw_input"):
            return f
    return None


def generate_seo(title, desc):
    """Gemini 2.0 Flash দিয়ে বাংলা SEO মেটাডেটা"""
    prompt = (
        "YouTube drama video থেকে viral নতুন বাংলা SEO টাইটেল, ডেসক্রিপশন ও ট্যাগ তৈরি করো।\n"
        'JSON format (strict):\n'
        '{"title":"নতুন টাইটেল (সর্বোচ্চ ৯০ অক্ষর)",'
        '"description":"বাংলা ডেসক্রিপশন #হ্যাশট্যাগ সহ",'
        '"tags":["t1","t2","t3","t4","t5"]}\n\n'
        f"Original title: {title}\nOriginal description: {(desc or '')[:500]}\n"
    )
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        r = model.generate_content(prompt)
        text = r.text.strip()
        s = text.find("{"); e = text.rfind("}") + 1
        if s >= 0 and e > s:
            return json.loads(text[s:e])
    except Exception as ex:
        print("Gemini error:", ex)
    return {
        "title": ("নাটক বিশেষ - " + (title or "video")[:40])[:90],
        "description": "আজকের বিশেষ নাটক পর্ব উপভোগ করুন। লাইক ও সাবস্ক্রাইব করুন!\n#BanglaDrama #নাটক #SSKDrama",
        "tags": ["bangla drama", "নাটক", "SSK Drama", "crime alert", "hindi webseries"],
    }


def process_channel(channel, label, history, manifest):
    """একটি চ্যানেল সম্পূর্ণ প্রসেস + Release-এ আপলোড"""
    candidates = fetch_old_videos(channel, history)
    if not candidates:
        print(f"[{label}] কোনো পুরনো ভিডিও পাওয়া যায়নি: {channel}")
        return 0

    count = 0
    for v in candidates:
        vid = v["id"]
        url = f"https://www.youtube.com/watch?v={vid}"
        orig_title = v.get("title", "video")
        orig_desc = v.get("description", "")

        notify(f"⬇️ ডাউনলোড ({label}): {orig_title}")
        raw = download_video(url)
        if not raw:
            print("Download failed:", vid)
            continue

        seo = generate_seo(orig_title, orig_desc)

        try:
            sub_path = generate_subtitles(raw, f"sub_{vid}.srt")
        except Exception as e:
            print("Subtitle failed:", e); sub_path = None

        notify(f"🎬 YouTube 16:9 এডিট: {seo['title']}")
        yt_out = f"yt_{vid}.mp4"
        edit_for_youtube(raw, sub_path, yt_out)

        notify(f"🎬 Facebook 1:1 এডিট: {seo['title']}")
        fb_out = f"fb_{vid}.mp4"
        edit_for_facebook(raw, sub_path, fb_out)

        today = datetime.date.today().isoformat()
        tag = f"daily-{today}"
        notify(f"📤 Release-এ আপলোড হচ্ছে: {tag}")
        rel = get_release(tag) or create_release(tag, f"Daily {today}")
        if rel:
            upload_asset(rel, yt_out, f"{vid}_yt.mp4")
            upload_asset(rel, fb_out, f"{vid}_fb.mp4")

        manifest[vid] = {
            "title": seo["title"],
            "description": seo["description"],
            "tags": seo.get("tags", []),
            "yt_asset": f"{vid}_yt.mp4",
            "fb_asset": f"{vid}_fb.mp4",
            "uploaded_fb": False,
            "uploaded_yt": False,
            "original_channel": channel,
            "label": label,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        history.append(vid)
        save_json(HISTORY_FILE, history)

        for f in list(os.listdir(".")):
            if (f.startswith("raw_input") or f.startswith("yt_") or
                f.startswith("fb_") or f.startswith("sub_")):
                try: os.remove(f)
                except: pass

        count += 1
    return count


def main():
    notify("🌙 এডিট উইন্ডো শুরু (BD রাত ০১:০০–০৬:০০)")
    config = load_json(CONFIG_FILE, {})
    history = load_json(HISTORY_FILE, [])

    today = datetime.date.today().isoformat()
    tag = f"daily-{today}"
    raw = download_manifest_text(tag)
    manifest = json.loads(raw) if raw else {}

    total = 0
    for ch in config.get("facebook_channels", []):
        total += process_channel(ch, "FB", history, manifest)
    for ch in config.get("youtube_channels", []):
        total += process_channel(ch, "YT", history, manifest)

    upload_manifest_text(tag, json.dumps(manifest, ensure_ascii=False, indent=2))
    save_json(HISTORY_FILE, history)
    notify(f"✅ এডিট শেষ! আজকে {total}টি ভিডিও প্রসেস হয়েছে।")


if __name__ == "__main__":
    main()
