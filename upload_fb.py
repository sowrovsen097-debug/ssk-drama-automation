"""প্রতি ঘণ্টায় চলে, config-এ নির্দিষ্ট বাংলাদেশ সময়ে Facebook-এ আপলোড।
সফল হলে Release asset ডিলিট করে — স্টোরেজ খালি।
"""
import os
import json
import datetime
import requests
from release_manager import (
    list_assets, delete_asset, download_asset,
    download_manifest_text, upload_manifest_text,
)

FB_PAGE_ID        = os.getenv("FB_PAGE_ID")
FB_ACCESS_TOKEN   = os.getenv("FB_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
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
    """বাংলাদেশ সময়কে UTC-তে রূপান্তর (BD = UTC+6)"""
    h, m = map(int, t.split(":"))
    h = (h - 6) % 24
    return f"{h:02d}:{m:02d}"


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

    for bd_t in config.get("fb_upload_times_bd", []):
        if bd_to_utc(bd_t) == utc_now:
            break
    else:
        return  # এই মিনিটে কোনো FB আপলোড নয়

    assets = list_assets(tag)
    for vid, info in manifest.items():
        if info.get("uploaded_fb"):
            continue
        asset_name = info.get("fb_asset")
        target = next((a for a in assets if a["name"] == asset_name), None)
        if not target:
            continue

        local = f"/tmp/upload_{vid}_fb.mp4"
        if not download_asset(target["browser_download_url"], local):
            notify(f"❌ ডাউনলোড ব্যর্থ: {asset_name}")
            continue

        notify(f"🚀 FB আপলোড শুরু: {info['title']}")
        url = f"https://graph-video.facebook.com/v25.0/{FB_PAGE_ID}/videos"
        with open(local, "rb") as f:
            r = requests.post(url,
                              data={"title": info["title"][:100],
                                    "description": info["description"],
                                    "access_token": FB_ACCESS_TOKEN},
                              files={"source": f}, timeout=900)
        result = r.json()
        if "id" in result:
            fb_vid = result["id"]
            manifest[vid]["uploaded_fb"] = True
            manifest[vid]["fb_video_id"] = fb_vid
            manifest[vid]["fb_uploaded_at"] = (
                datetime.datetime.now(datetime.timezone.utc).isoformat())
            delete_asset(target["id"])
            upload_manifest_text(tag, json.dumps(manifest, ensure_ascii=False, indent=2))
            notify(f"✅ FB আপলোড সফল: {info['title']}\n"
                   f"লিংক: https://facebook.com/{fb_vid}")
            try: os.remove(local)
            except: pass
            break
        else:
            notify(f"❌ FB আপলোড ব্যর্থ: {json.dumps(result)[:300]}")


if __name__ == "__main__":
    main()
