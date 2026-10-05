"""প্রতি ঘণ্টায় চলে, config-এ নির্দিষ্ট বাংলাদেশ সময়ে YouTube-এ আপলোড।"""
import os
import json
import datetime
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from release_manager import (
    list_assets, delete_asset, download_asset,
    download_manifest_text, upload_manifest_text,
)

YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
GOOGLE_CLIENT_ID      = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET  = os.getenv("GOOGLE_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID")
CONFIG_FILE = "config.json"


def notify(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=15)
        except Exception as e:
            print("TG error:", e)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def bd_to_utc(t):
    h, m = map(int, t.split(":"))
    return f"{(h - 6) % 24:02d}:{m:02d}"


def get_client():
    creds = Credentials(
        token=None, refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def main():
    today = datetime.date.today().isoformat()
    tag = f"daily-{today}"
    text = download_manifest_text(tag)
    if not text:
        notify("ℹ️ আজকের Release-এ ভিডিও নেই।")
        return
    manifest = json.loads(text)

    config = load_config()
    utc_now = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")
    if not any(bd_to_utc(t) == utc_now for t in config.get("yt_upload_times_bd", [])):
        return

    youtube = get_client()
    assets = list_assets(tag)

    for vid, info in manifest.items():
        if info.get("uploaded_yt"):
            continue
        asset_name = info.get("yt_asset")
        target = next((a for a in assets if a["name"] == asset_name), None)
        if not target:
            continue

        local = f"/tmp/upload_{vid}_yt.mp4"
        if not download_asset(target["browser_download_url"], local):
            notify(f"❌ ডাউনলোড ব্যর্থ: {asset_name}")
            continue

        notify(f"🚀 YouTube আপলোড শুরু: {info['title']}")
        body = {
            "snippet": {
                "title": info["title"][:100],
                "description": info["description"],
                "tags": info.get("tags", [])[:15],
                "categoryId": "24",
                "defaultLanguage": "bn",
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(local, chunksize=-1, resumable=True)
        try:
            request = youtube.videos().insert(
                part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                _, response = request.next_chunk()
            yt_vid_id = response["id"]
            manifest[vid]["uploaded_yt"] = True
            manifest[vid]["yt_video_id"] = yt_vid_id
            manifest[vid]["yt_uploaded_at"] = (
                datetime.datetime.now(datetime.timezone.utc).isoformat())
            delete_asset(target["id"])
            upload_manifest_text(tag, json.dumps(manifest, ensure_ascii=False, indent=2))
            notify(f"✅ YouTube আপলোড সফল: {info['title']}\n"
                   f"লিংক: https://youtu.be/{yt_vid_id}")
            try: os.remove(local)
            except: pass
            break
        except Exception as e:
            notify(f"❌ YouTube আপলোড ব্যর্থ: {e}")


if __name__ == "__main__":
    main()
