"""Telegram Bot: getUpdates long-poll করে প্রায়-রিয়েল-টাইমে (৫-মিনিট চক্র) Gemini রিপ্লাই দেয়।
নোট: GitHub Actions-এর ক্রোন সর্বনিম্ন ৫ মিনিট, তাই রিপ্লাই নিয়ে-ই-আসা নয়, প্রায়-রিয়েল-টাইম।"""
import os
import json
import datetime
import requests
import google.generativeai as genai

GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
GITHUB_REPO         = os.getenv("GITHUB_REPOSITORY")

genai.configure(api_key=GEMINI_API_KEY)

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
OFFSET_FILE = "telegram_offset.txt"


def get_offset():
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE, "r") as f:
            return int(f.read().strip() or "0")
    return 0


def save_offset(off):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(off))


def send(cid, text):
    try:
        requests.post(f"{API}/sendMessage",
                      data={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                      timeout=15)
    except Exception as e:
        print("send error:", e)


def ai_reply(text):
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        prompt = (
            "তুমি SSK Drama অটোমেশনের বাংলা AI সহকারী। "
            "সংক্ষেপে, স্পষ্টভাবে, বন্ধুসুলভ ভাবে উত্তর দাও। "
            "৫০০ অক্ষরের মধ্যে রাখো। প্রশ্ন: " + text
        )
        r = model.generate_content(prompt)
        return r.text.strip()[:4000]
    except Exception as e:
        return f"⚠️ AI রিপ্লাই ব্যর্থ: {e}"


def get_status():
    today = datetime.date.today().isoformat()
    msg = f"🕐 {today}\n📊 SSK Drama পাইপলাইন স্ট্যাটাস:\n\n"
    try:
        from release_manager import list_assets
        assets = list_assets(f"daily-{today}")
        yt = sum(1 for a in assets if a["name"].endswith("_yt.mp4"))
        fb = sum(1 for a in assets if a["name"].endswith("_fb.mp4"))
        msg += f"📦 আজকের Release-এ:\n  🎬 YouTube assets: {yt}\n  📘 Facebook assets: {fb}\n"
    except Exception:
        msg += "📦 Release-এর ডেটা পড়া যাচ্ছে না।\n"
    msg += f"\n⏰ আগামী এডিট: আগামীকাল BD রাত ০১:০০\n"
    msg += f"⏰ আপলোড: প্রতি ঘণ্টায় চেক, কনফিগ-এ নির্দিষ্ট সময়ে হয়"
    return msg


def main():
    off = get_offset()
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"offset": off, "timeout": 25}, timeout=35)
        updates = r.json().get("result", [])
    except Exception as e:
        print("getUpdates error:", e)
        return

    for u in updates:
        off = max(off, u["update_id"] + 1)
        msg = u.get("message") or {}
        cid = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not cid or not text:
            continue
        if TELEGRAM_CHAT_ID and str(cid) != str(TELEGRAM_CHAT_ID):
            continue

        if text == "/status" or text == "status":
            send(cid, get_status())
        elif text in ("/start", "hi", "hello", "হ্যালো", "/hi"):
            send(cid, "👋 আসসালামু আলাইকুম!\n"
                     "আমি SSK Drama অটোমেশনের AI সহকারী।\n"
                     "• /status — পাইপলাইন স্ট্যাটাস\n"
                     "• যেকোনো প্রশ্ন বাংলায় লিখুন")
        elif text.startswith("/"):
            send(cid, "⚙️ এই কমান্ড পাওয়া যায়নি। /status লিখুন বা প্রশ্ন করুন।")
        else:
            send(cid, ai_reply(text))

    save_offset(off)


if __name__ == "__main__":
    main()
