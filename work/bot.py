# bot.py — টেলিগ্রাম রিপ্লাই-বট: লাইভ আপডেট, সেটিংস, AI সহকারী, কমান্ড
import os, io, json, time, requests
import config as C

TG = "https://api.telegram.org/bot%s"
HELP = """🎬 <b>SSK DRAMA Bot</b>

• <b>SSK DRAMA LIVE</b> — এখন কী চলছে (লাইভ)
• <b>SSK DRAMA UPDATE</b> — সেটিংস দেখুন
• <b>SSK DRAMA START EDIT</b> — এখনই এডিট শুরু
• <b>SSK DRAMA STATUS</b> — কিউ, Drive জায়গা, ভিউ
• যেকোনো প্রশ্ন লিখুন — AI সহকারী উত্তর দেবে

⏱ প্রতি ৫ মিনিটে অটো আপডেট আসবে।"""

def token(): return os.getenv("TELEGRAM_BOT_TOKEN", "")
def chat(): return str(os.getenv("TELEGRAM_CHAT_ID", ""))

def send(text, chat_id=None, keyboard=None, parse_mode="HTML"):
    t = token()
    if not t: return {"ok": False, "error": "no bot token"}
    data = {"chat_id": chat_id or chat(), "text": text[:4000], "parse_mode": parse_mode,
            "disable_web_page_preview": True}
    if keyboard: data["reply_markup"] = json.dumps(keyboard)
    try: return requests.post((TG % t) + "/sendMessage", data=data, timeout=30).json()
    except Exception as e: print("telegram error:", e); return {"ok": False, "error": str(e)[:150]}

def get_updates(offset=None, timeout=0):
    t = token()
    if not t: return []
    try:
        r = requests.get((TG % t) + "/getUpdates",
                         params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
                         timeout=timeout + 20)
        return r.json().get("result", [])
    except Exception: return []

def load_offset(): return C.load_json("tg_offset.json", {}).get("offset")
def save_offset(o): C.save_json("tg_offset.json", {"offset": o})

def live_text():
    s = C.load_status(); q = C.load_queue(); run = s.get("run", {})
    cnt = s.get("counters", {}); st = s.get("stats", {})
    lines = ["🎬 <b>SSK DRAMA LIVE</b>", "🕒 " + s.get("updated_at_bst", "-"), "",
             "⚙️ এখন: <b>%s</b>" % (run.get("mode", "idle")), "📌 %s" % run.get("detail", "—"), "",
             "✅ আজ এডিট: <b>%s</b>" % cnt.get("edited_today", 0),
             "🚀 আজ আপলোড: <b>%s</b>" % cnt.get("uploaded_today", 0),
             "⚠️ ফেইল: <b>%s</b>" % cnt.get("failed_today", 0),
             "📦 কিউতে: <b>%d</b>টি ভিডিও" % len(q)]
    if st.get("views") is not None:
        lines += ["", "👁️ ভিউ (৭ দিন): <b>%s</b>" % st.get("views"),
                  "🌍 টপ দেশ: %s" % (st.get("top_countries") or "—")]
    if st.get("drive_free_gb") is not None:
        lines += ["💾 Drive খালি: <b>%s GB</b> / %s GB" % (st.get("drive_free_gb"), st.get("drive_total_gb"))]
    return "\n".join(lines)

def queue_text():
    q = C.load_queue()
    if not q: return "📦 কিউ খালি — পরের এডিট উইন্ডোতে নতুন ভিডিও যোগ হবে।"
    out = ["📦 <b>আপলোড কিউ (%d)</b>" % len(q)]
    for it in q[-12:]:
        out.append("• %s → <b>%s</b> · %s · %.1f MB" % (it.get("platform", "?").upper(),
                   it.get("slot_bst", "-"), (it.get("title") or "")[:40], float(it.get("size_mb", 0))))
    return "\n".join(out)

def settings_text():
    cfg = C.load_config(); good, bad = C.validate_times(cfg)
    t = ["⚙️ <b>SSK DRAMA UPDATE</b>", "", "🔵 Facebook চ্যানেল (১-৩):"]
    t += ["%d. %s" % (i + 1, (u or "—")[:52]) for i, u in enumerate(cfg["facebook_channels"])]
    t += ["", "🔴 YouTube চ্যানেল (৪-৬):"]
    t += ["%d. %s" % (i + 4, (u or "—")[:52]) for i, u in enumerate(cfg["youtube_channels"])]
    t += ["", "⏰ FB আপলোড (BST): %s" % ", ".join(cfg["fb_upload_times_bst"]),
          "⏰ YT আপলোড (BST): %s" % ", ".join(cfg["yt_upload_times_bst"]),
          "✂️ এডিট উইন্ডো: %s–%s BST (নূন্যতম %s ঘণ্টা আগে)" % (cfg["edit_window_bst"][0],
                                                             cfg["edit_window_bst"][1], cfg["min_edit_lead_hours"]),
          "🎯 রাখার অনুপাত: %s%% · ফিল্টার: %s দিনের পুরোনো" % (round(float(cfg.get("keep_ratio", 0.8125)) * 100, 2),
                                                            cfg.get("min_age_days", 60)),
          "📐 YouTube %sx%s · Facebook %sx%s · মিরর(FB): %s" % (cfg["yt_width"], cfg["yt_height"], cfg["fb_width"],
                                                               cfg["fb_height"], "চালু" if cfg.get("mirror_facebook") else "বন্ধ"),
          "", "✅ সঠিক সময়: %s" % ", ".join(good or ["—"]),
          ("🚫 বাদ পড়বে: %s (এডিট উইন্ডো/৬ ঘণ্টা নিয়ম ভাঙে)" % ", ".join(bad)) if bad else "",
          "", "👉 সেটিংস বদলাতে অ্যাপের <b>SSK DRAMA UPDATE</b> ট্যাব ব্যবহার করুন।"]
    return "\n".join([x for x in t if x is not None])

def ai_answer(question, context=""):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key: return "⚠️ GEMINI_API_KEY সেট নেই।"
    s = C.load_status()
    facts = json.dumps({"counters": s.get("counters", {}), "queue": len(C.load_queue()),
                        "run": s.get("run", {}), "stats": s.get("stats", {}),
                        "log": [l.get("msg") for l in (s.get("log") or [])[-8:]]}, ensure_ascii=False)[:1500]
    prompt = ("তুমি SSK DRAMA নামের একটি ২৪/৭ ভিডিও অটোমেশন সিস্টেমের সহকারী। "
              "ব্যবহারকারী বাংলায় লিখবেন, উত্তরও সহজ বাংলায় দাও (৫-৮ লাইন, ইমোজি সহ)। "
              "সিস্টেমের বর্তমান অবস্থা (JSON): %s\n\nআগের কথা: %s\n\nব্যবহারকারীর প্রশ্ন: %s"
              % (facts, context[-500:], question))
    for model in ("gemini-2.0-flash", "gemini-1.5-flash"):
        try:
            r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
                              params={"key": key},
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"temperature": 0.6, "maxOutputTokens": 700}}, timeout=60)
            parts = ((r.json().get("candidates") or [{}])[0].get("content", {}) or {}).get("parts", [])
            if parts: return parts[0].get("text", "").strip()
        except Exception: continue
    return "⚠️ AI উত্তর দিতে পারছে না, আবার চেষ্টা করুন।"

def bridge_poll():
    ch = C.load_json(C.CHAT_FILE, {"msgs": []}); msgs = ch.get("msgs", []); changed = False
    for m in msgs[-8:]:
        if m.get("role") == "user" and not m.get("answered"):
            ans = ai_answer(m.get("text", ""))
            msgs.append({"role": "ai", "text": ans,
                         "t": C.now_utc().astimezone(C.BST).strftime("%d %b %H:%M")})
            m["answered"] = True; changed = True
            send("💬 <b>অ্যাপ থেকে প্রশ্ন:</b> %s\n\n%s" % (m.get("text", "")[:200], ans))
    if changed:
        ch["msgs"] = msgs[-40:]; C.gh_put_json(C.CHAT_FILE, ch, "ai chat bridge")
    return changed

COMMANDS = {"ssk drama live": lambda: live_text(),
            "ssk drama status": lambda: live_text() + "\n\n" + queue_text(),
            "ssk drama update": settings_text, "ssk drama queue": queue_text}

def handle(text):
    t = (text or "").strip().lower()
    if t in ("/start", "start", "help", "/help"): return HELP, None
    if t in COMMANDS: return COMMANDS[t](), None
    if t in ("ssk drama start edit", "/edit", "ssk drama edit"):
        C.dispatch("ssk-edit", {"reason": "telegram"})
        return "✂️ এডিট শুরু করার সিগন্যাল পাঠানো হয়েছে।", None
    if t in ("ssk drama now upload", "/upload"):
        C.dispatch("ssk-publish", {"reason": "telegram"})
        return "🚀 আপলোড চেক শুরু করা হয়েছে।", None
    if t in ("ssk drama settings", "/settings"):
        return settings_text(), {"inline_keyboard": [[{"text": "✂️ এখনই এডিট", "callback_data": "edit"},
                                                     {"text": "🚀 কিউ চেক", "callback_data": "publish"}]]}
    if t.startswith("/ask ") or t.startswith("ask "): return ai_answer(text.split(" ", 1)[1]), None
    return ai_answer(text), None

def poll_loop(seconds=280, log=print):
    off = load_offset(); end = time.time() + float(seconds); handled = 0
    while time.time() < end:
        for u in get_updates(offset=off, timeout=20):
            off = u["update_id"] + 1; save_offset(off)
            txt = (u.get("message") or {}).get("text")
            if txt:
                reply, kb = handle(txt); send(reply, keyboard=kb); handled += 1
        try: bridge_poll()
        except Exception as e: log("bridge: %s" % str(e)[:120])
        C.status_patch({"bot": {"last_poll_bst": C.now_utc().astimezone(C.BST).strftime("%d %b %H:%M:%S"),
                                "handled": handled, "online": True, "interval": "৫ মিনিটে অটো আপডেট"}})
        if time.time() >= end: break
        time.sleep(8)
    return handled
