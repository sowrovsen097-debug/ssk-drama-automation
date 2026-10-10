# bot.py — টেলিগ্রাম বট ও এআই অ্যাসিস্ট্যান্ট হ্যান্ডলার
import os, io, json, time, requests
import config as C

TG = "https://api.telegram.org/bot%s"
HELP = """🎬 <b>SSK DRAMA Bot</b>\n\n• <b>SSK DRAMA LIVE</b> — লাইভ স্ট্যাটাস\n• <b>SSK DRAMA UPDATE</b> — সেটিংস\n• <b>SSK DRAMA START EDIT</b> — এডিট শুরু\n• <b>SSK DRAMA STATUS</b> — কিউ ও স্ট্যাটাস\n\nযেকোনো প্রশ্ন লিখুন — AI সহকারী উত্তর দেবে।"""

def token(): return os.getenv("TELEGRAM_BOT_TOKEN", "")
def chat(): return str(os.getenv("TELEGRAM_CHAT_ID", ""))

def send(text, chat_id=None, keyboard=None, parse_mode="HTML"):
    t = token()
    if not t: return {"ok": False}
    data = {"chat_id": chat_id or chat(), "text": text[:4000], "parse_mode": parse_mode, "disable_web_page_preview": True}
    if keyboard: data["reply_markup"] = json.dumps(keyboard)
    try:
        return requests.post((TG % t) + "/sendMessage", data=data, timeout=30).json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def get_updates(offset=None, timeout=0):
    t = token()
    if not t: return []
    try:
        r = requests.get((TG % t) + "/getUpdates", params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'}, timeout=timeout + 20)
        return r.json().get("result", [])
    except Exception:
        return []

def load_offset(): return C.load_json("tg_offset.json", {}).get("offset")
def save_offset(o): C.save_json("tg_offset.json", {"offset": o})

def live_text():
    s = C.load_status(); q = C.load_queue(); run = s.get("run", {}); cnt = s.get("counters", {}); st = s.get("stats", {})
    lines = [
        "🎬 <b>SSK DRAMA LIVE</b>", f"🕒 {s.get('updated_at_bst', '-')}", "",
        f"⚙️ বর্তমান অবস্থা: <b>{run.get('mode', 'idle')}</b>", f"📌 {run.get('detail', '—')}", "",
        f"✅ আজ এডিট: <b>{cnt.get('edited_today', 0)}</b>", f"🚀 আজ আপলোড: <b>{cnt.get('uploaded_today', 0)}</b>",
        f"📦 কিউতে: <b>{len(q)}</b>টি ভিডিও"
    ]
    if st.get("views") is not None:
        lines += ["", f"👁️ ভিউ (৭ দিন): <b>{st.get('views')}</b>", f"🌍 টপ দেশ: {st.get('top_countries', '—')}"]
    if st.get("drive_free_gb") is not None:
        lines += [f"💾 Drive খালি: <b>{st.get('drive_free_gb')} GB</b>"]
    return "\n".join(lines)

def settings_text():
    cfg = C.load_config(); good, bad = C.validate_times(cfg)
    t = ["⚙️ <b>SSK DRAMA UPDATE</b>", "", "🔵 Facebook চ্যানেল:"]
    t += [f"{i+1}. {u or '—'}" for i, u in enumerate(cfg["facebook_channels"])]
    t += ["", "🔴 YouTube চ্যানেল:"]
    t += [f"{i+4}. {u or '—'}" for i, u in enumerate(cfg["youtube_channels"])]
    t += ["", f"⏰ FB আপলোড: {', '.join(cfg['fb_upload_times_bst'])}", f"⏰ YT আপলোড: {', '.join(cfg['yt_upload_times_bst'])}"]
    return "\n".join(t)

def ai_answer(question, context=""):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key: return "⚠️ GEMINI_API_KEY সেট নেই।"
    s = C.load_status()
    prompt = f"তুমি SSK DRAMA অটোমেশনের সহকারী। বাংলায় সংক্ষিপ্ত ও সুন্দর উত্তর দাও। স্ট্যাটাস: {json.dumps(s, ensure_ascii=False)[:1000]}\nপ্রশ্ন: {question}"
    for model in ("gemini-2.0-flash", "gemini-1.5-flash"):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", params={"key": key}, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            parts = r.json()["candidates"][0]["content"]["parts"]
            if parts: return parts[0].get("text", "").strip()
        except Exception:
            continue
    return "⚠️ AI উত্তর দিতে পারছে না।"

def bridge_poll():
    ch = C.load_json(C.CHAT_FILE, {"msgs": []}); msgs = ch.get("msgs", []); changed = False
    for m in msgs[-8:]:
        if m.get("role") == "user" and not m.get("answered"):
            ans = ai_answer(m.get("text", ""))
            msgs.append({"role": "ai", "text": ans, "t": C.now_utc().astimezone(C.BST).strftime("%d %b %H:%M")})
            m["answered"] = True; changed = True
            send(f"💬 <b>অ্যাপ থেকে প্রশ্ন:</b> {m.get('text', '')}\n\n{ans}")
    if changed:
        ch["msgs"] = msgs[-40:]; C.gh_put_json(C.CHAT_FILE, ch, "ai chat bridge")
    return changed

def handle(text):
    t = (text or "").strip().lower()
    if t in ("/start", "start", "help", "/help"): return HELP, None
    if t in ("ssk drama live", "live"): return live_text(), None
    if t in ("ssk drama status", "status"): return live_text(), None
    if t in ("ssk drama update", "update"): return settings_text(), None
    if t in ("ssk drama start edit", "edit"):
        C.dispatch("ssk-edit", {"reason": "telegram"})
        return "✂️ এডিট শুরু করার সিগন্যাল পাঠানো হয়েছে।", None
    return ai_answer(text), None

def poll_loop(seconds=280, log=print):
    off = load_offset(); end = time.time() + float(seconds); handled = 0
    while time.time() < end:
        for u in get_updates(offset=off, timeout=20):
            off = u["update_id"] + 1; save_offset(off)
            txt = (u.get("message") or {}).get("text")
            if txt:
                reply, kb = handle(txt); send(reply, keyboard=kb); handled += 1
        try: bridge_poll() except Exception: pass
        C.status_patch({"bot": {"last_poll_bst": C.now_utc().astimezone(C.BST).strftime("%d %b %H:%M:%S"), "handled": handled, "online": True}})
        if time.time() >= end: break
        time.sleep(8)
    return handled
