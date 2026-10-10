# orchestrator.py — মাস্টার কন্ট্রোল: mode=edit / publish / bot / tick / status / manual / self-test
import os, io, sys, json, time, glob, shutil, datetime, traceback, requests
import config as C
import publish as P
import bot as TG

MODES = ("edit", "publish", "bot", "tick", "status", "manual", "self-test")

def log(msg, level="info"):
    print("[%s] %s" % (level.upper(), msg), flush=True)
    try: C.status_log(msg, level)
    except Exception: pass

def cookies_file():
    raw = os.getenv("YT_COOKIES", "")
    if not raw: return None
    p = "/tmp/yt_cookies.txt"
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(raw if raw.lstrip().startswith("#") else raw.replace("\\n", "\n"))
    return p

def ydl_opts(extra=None, ck=None):
    o = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": False,
         "socket_timeout": 30, "retries": 3, "nocheckcertificate": True, "geo_bypass": True}
    if ck and os.path.exists(ck): o["cookiefile"] = ck
    o.update(extra or {}); return o

def pick_video(channel_url, cfg, history, ck=None):
    import yt_dlp
    min_days = int(cfg.get("min_age_days", 60)); max_min = float(cfg.get("max_duration_min", 80))
    with yt_dlp.YoutubeDL(ydl_opts({"extract_flat": "in_playlist", "playlistend": 40}, ck)) as ydl:
        info = ydl.extract_info(channel_url.rstrip("/") + "/videos", download=False)
    entries = [e for e in (info or {}).get("entries", []) if e]
    log("চ্যানেল %s → %dটি ভিডিও পাওয়া গেল" % (channel_url.split("@")[-1][:22], len(entries)))
    now = C.now_utc()
    for e in entries:
        vid = e.get("id")
        if not vid or vid in history.get("done", {}): continue
        dur = e.get("duration") or 0
        if dur and dur > max_min * 60: continue
        try:
            with yt_dlp.YoutubeDL(ydl_opts({}, ck)) as ydl:
                v = ydl.extract_info("https://www.youtube.com/watch?v=%s" % vid, download=False)
        except Exception as ex:
            log("skip %s (%s)" % (vid, str(ex)[:60])); continue
        ud, d = v.get("upload_date"), v.get("duration") or dur
        if not ud: continue
        age = (now - datetime.datetime.strptime(ud, "%Y%m%d").replace(tzinfo=C.UTC)).days
        if age >= min_days and d and d <= max_min * 60:
            return {"id": vid, "url": "https://www.youtube.com/watch?v=%s" % vid,
                    "title": v.get("title", ""), "description": (v.get("description") or "")[:3000],
                    "duration": d, "age_days": age, "channel": channel_url}
        log("skip %s: %s দিন বয়স, %.0f মিনিট" % (vid, age, (d or 0) / 60.0))
    return None

def download(video_url, dest_dir, ck=None):
    import yt_dlp
    os.makedirs(dest_dir, exist_ok=True)
    tpl = os.path.join(dest_dir, "src.%(ext)s")
    opts = ydl_opts({"skip_download": False, "format": "bestvideo[height<=1080]+bestaudio/best",
                     "outtmpl": tpl, "merge_output_format": "mp4", "concurrent_fragment_downloads": 4}, ck)
    with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([video_url])
    hit = sorted(glob.glob(os.path.join(dest_dir, "src.*")))
    return hit[0] if hit else ""

def seo_metadata(title, description, platform):
    key = os.getenv("GEMINI_API_KEY", "")
    fallback = {"title": ("%s | নতুন পর্ব" % title[:70]),
                "description": "আজকের পর্ব উপভোগ করুন। ভালো লাগলে লাইক, শেয়ার ও সাবস্ক্রাইব করুন।",
                "tags": ["SSK Drama", "Bangla Drama", "Bengali Series", "Drama", "New Episode"]}
    if not key: return fallback
    prompt = ("তুমি বাংলা নাটক/ক্রাইম সিরিজের SEO বিশেষজ্ঞ। নিচের ভিডিওর জন্য সম্পূর্ণ নতুন, "
              "ক্লিক-রেট বাড়ানো টাইটেল, বর্ণনা ও ট্যাগ বাংলায় তৈরি করো। মূল লেখা হুবহু কপি করবে না, "
              "নতুন রচনা করবে (অন্য শব্দ, অন্য বাক্যক্রম)।\nপ্ল্যাটফর্ম: %s\nমূল টাইটেল: %s\nমূল বর্ণনা: %s\n\n"
              "শুধু এই JSON দাও: {\"title\":\"...\",\"description\":\"...(ইমোজি + হ্যাশট্যাগ)\","
              "\"tags\":[\"..\",\"..\"],\"hook\":\"৬-৮ শব্দের স্ক্রল-থামানো লাইন\"}" % (platform, title, description))
    for model in ("gemini-2.0-flash", "gemini-1.5-flash"):
        try:
            r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
                              params={"key": key},
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"temperature": 0.9, "maxOutputTokens": 900,
                                                         "responseMimeType": "application/json"}}, timeout=90)
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            j = json.loads(txt.replace("```json", "").replace("```", ""))
            out = fallback.copy(); out.update({k: v for k, v in j.items() if v})
            if isinstance(out.get("tags"), list): out["tags"] = [str(t)[:30] for t in out["tags"]][:25]
            else: out["tags"] = fallback["tags"]
            out["engine"] = model; return out
        except Exception as e:
            log("seo %s failed: %s" % (model, str(e)[:100]))
    return fallback

def run_edit():
    cfg = C.load_config(); history = C.load_history(); ck = cookies_file()
    t0 = time.time(); budget = 5.2 * 3600
    dtoken = P.drive_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                           os.getenv("GOOGLE_REFRESH_TOKEN"))[0] if os.getenv("GOOGLE_REFRESH_TOKEN") else None
    dfolder = P.drive_folder_id(dtoken) if dtoken else None
    jobs = ([{"url": u, "platform": "facebook"} for u in cfg["facebook_channels"] if u.strip()] +
            [{"url": u, "platform": "youtube"} for u in cfg["youtube_channels"] if u.strip()])
    log("এডিট উইন্ডো শুরু — %dটি চ্যানেল" % len(jobs))
    C.status_patch({"run": {"mode": "editing", "detail": "%dটি চ্যানেল প্রসেস হবে" % len(jobs),
                            "started_bst": C.now_utc().astimezone(C.BST).strftime("%d %b %H:%M")},
                    "counters": {"edited_today": 0, "uploaded_today": 0, "failed_today": 0}})
    made = 0
    for jb in jobs:
        if time.time() - t0 > budget: log("সময় শেষ — বাকি চ্যানেল পরের রানে"); break
        try:
            log("🔎 %s চ্যানেল স্ক্যান: %s" % (jb["platform"], jb["url"][:60]))
            C.status_patch({"run": {"mode": "editing", "detail": "স্ক্যান: %s" % jb["url"][:45]}})
            v = pick_video(jb["url"], cfg, history, ck)
            if not v:
                log("উপযুক্ত নতুন (≥%s দিন, ≤%s মিনিট) ভিডিও নেই" % (cfg["min_age_days"], cfg["max_duration_min"]))
                continue
            wd = "/tmp/work_%s" % v["id"]
            shutil.rmtree(wd, ignore_errors=True)
            log("⬇️ ডাউনলোড: %s (%.0f মিনিট, %s দিন আগের)" % (v["title"][:60], v["duration"]/60, v["age_days"]))
            C.status_patch({"run": {"mode": "downloading", "detail": v["title"][:60]}})
            src = download(v["url"], wd, ck)
            if not src: raise RuntimeError("download failed")
            import studio
            out = os.path.join(wd, "edited_%s.mp4" % jb["platform"])
            final, meta = studio.make_edited(src, out, cfg, jb["platform"], os.path.join(wd, "build"))
            seo = seo_metadata(v["title"], v["description"], jb["platform"])
            item = {"video_id": v["id"], "platform": jb["platform"], "title": seo["title"],
                    "description": seo["description"], "tags": seo.get("tags", []),
                    "source_title": v["title"], "channel": jb["url"], "size_mb": meta["size_mb"],
                    "duration_min": round(meta["duration"] / 60, 1),
                    "edited_at_bst": C.now_utc().astimezone(C.BST).strftime("%Y-%m-%d %H:%M"), "meta": meta}
            if dtoken:
                up = P.drive_upload(dtoken, dfolder, final, "%s_%s.mp4" % (jb["platform"], v["id"]))
                if up.get("ok"):
                    item["drive_file_id"] = up["file_id"]
                    log("💾 Drive-এ বাফার: %s (%.1f MB)" % (up["file_id"], meta["size_mb"]))
                else: log("Drive বাফার ব্যর্থ: %s" % up.get("error"))
            slot = C.next_upload_slot(cfg["fb_upload_times_bst" if jb["platform"] == "facebook" else "yt_upload_times_bst"],
                                      C.now_utc(), cfg.get("min_edit_lead_hours", 6))
            if slot is None: raise RuntimeError("কোনো আপলোড স্লট পাওয়া যায়নি (টাইম সেটিং দেখুন)")
            item["slot_utc"] = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
            item["slot_bst"] = slot.astimezone(C.BST).strftime("%d %b %I:%M %p")
            q = C.load_queue()
            if item.get("drive_file_id"): item["local_path"] = final
            q.append(item); C.save_queue(q); C.gh_put_json(C.QUEUE_FILE, q, "queue: +%s" % v["id"])
            C.mark_done(v["id"], {"platform": jb["platform"], "title": seo["title"],
                                  "at_bst": item["edited_at_bst"], "slot_bst": item["slot_bst"]})
            C.gh_put_json(C.HISTORY_FILE, C.load_history(), "history: %s" % v["id"])
            made += 1
            C.status_patch({"counters": {"edited_today": made},
                            "run": {"mode": "editing", "detail": "তৈরি: %s" % seo["title"][:45]}})
            TG.send("✅ <b>এডিট সম্পন্ন</b>\n🎬 %s\n📱 %s · ⏱ %.1f মিনিট · 💾 %.1f MB\n⏰ আপলোড: <b>%s (BST)</b>"
                    % (seo["title"][:80], jb["platform"].upper(), item["duration_min"], item["size_mb"], item["slot_bst"]))
            log("✅ %s প্রস্তুত → স্লট %s" % (v["id"], item["slot_bst"]))
        except Exception as e:
            log("❌ %s ব্যর্থ: %s" % (jb["platform"], str(e)[:200]), "error")
            C.status_patch({"counters": {"failed_today": 1}})
            TG.send("⚠️ <b>ফেইল</b> (%s): %s" % (jb["platform"], str(e)[:220]))
    C.status_patch({"run": {"mode": "idle", "detail": "এডিট উইন্ডো শেষ — %dটি তৈরি" % made}})
    TG.send("🌙 <b>এডিট উইন্ডো শেষ</b>\nতৈরি: %dটি ভিডিও\nপরের ধাপ: নির্ধারিত সময়ে অটো আপলোড" % made)
    return made

def run_publish(force=False):
    cfg = C.load_config(); q = C.load_queue()
    if not q: log("কিউ খালি"); return 0
    now = C.now_utc().astimezone(C.BST); done = 0; keep = []
    dtoken = P.drive_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                           os.getenv("GOOGLE_REFRESH_TOKEN"))[0] if os.getenv("GOOGLE_REFRESH_TOKEN") else None
    yt_tok = P.yt_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                        os.getenv("YOUTUBE_REFRESH_TOKEN"))[0]
    for it in q:
        due = False
        try:
            slot = datetime.datetime.strptime(it["slot_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=C.UTC)
            due = force or (now.astimezone(C.UTC) >= slot)
        except Exception: due = True
        if not due: keep.append(it); continue
        try:
            path = it.get("local_path", "")
            if not path or not os.path.exists(path):
                if dtoken and it.get("drive_file_id"):
                    path = "/tmp/%s.mp4" % it["drive_file_id"]; log("☁️ Drive থেকে নামানো হচ্ছে...")
                    r = P.drive_download(dtoken, it["drive_file_id"], path)
                    if not r.get("ok"): raise RuntimeError("drive download: %s" % r.get("error"))
                else: raise RuntimeError("ভিডিও পাওয়া যায়নি (local/Drive দুটোই নেই)")
            log("🚀 আপলোড (%s): %s" % (it["platform"], it["title"][:60]))
            C.status_patch({"run": {"mode": "uploading", "detail": "%s → %s" % (it["platform"], it["title"][:40])}})
            if it["platform"] == "facebook":
                res = P.fb_post_video(os.getenv("FB_PAGE_ID"), os.getenv("FB_ACCESS_TOKEN"), path, it["title"],
                                      it["description"] + "\n\n" + " ".join("#%s" % t.replace(" ", "")
                                                                             for t in (it.get("tags") or [])[:12]))
            else:
                res = P.yt_upload(yt_tok, path, it["title"], it["description"], tags=it.get("tags"),
                                  privacy=cfg.get("privacy_status", "public"), category=cfg.get("category_id", "24"))
            if not res.get("ok"): raise RuntimeError(str(res.get("error"))[:220])
            C.mark_uploaded({"video_id": it.get("video_id"), "platform": it["platform"], "title": it["title"],
                             "url": res.get("url"), "remote_id": res.get("id"),
                             "uploaded_bst": now.strftime("%Y-%m-%d %H:%M")})
            C.gh_put_json(C.HISTORY_FILE, C.load_history(), "history: uploaded %s" % it.get("video_id"))
            log("✅ আপলোড সফল → %s" % res.get("url"))
            TG.send("🚀 <b>আপলোড সফল!</b>\n📱 %s\n🎬 %s\n🔗 %s\n🕒 %s (BST)"
                    % (it["platform"].upper(), it["title"][:80], res.get("url"), now.strftime("%d %b %I:%M %p")))
            if dtoken and it.get("drive_file_id"):
                log("🗑 Drive ফাইল ডিলিট: %s" % P.drive_delete(dtoken, it["drive_file_id"]).get("ok"))
            if path and os.path.exists(path):
                try: os.remove(path)
                except Exception: pass
            for f in glob.glob("/tmp/work_%s*" % it.get("video_id")): shutil.rmtree(f, ignore_errors=True)
            done += 1
            s = C.load_status()
            C.status_patch({"counters": {**s.get("counters", {}),
                                         "uploaded_today": int(s.get("counters", {}).get("uploaded_today", 0)) + 1},
                            "run": {"mode": "idle", "detail": "আপলোড সম্পন্ন"}})
        except Exception as e:
            log("❌ আপলোড ব্যর্থ: %s" % str(e)[:200], "error")
            TG.send("⚠️ <b>আপলোড ফেইল</b> (%s): %s\nকিউতে থাকল, পরের রানে আবার চেষ্টা হবে।" % (it.get("platform"), str(e)[:220]))
            it["last_error"] = str(e)[:200]; keep.append(it)
    C.gh_put_json(C.QUEUE_FILE, keep, "queue: after publish"); C.save_queue(keep)
    return done

def run_status():
    st = {}
    try:
        tok = P.yt_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                         os.getenv("YOUTUBE_REFRESH_TOKEN"))[0]
        if tok:
            hist = C.load_history().get("uploaded", [])
            yt_ids = [h.get("remote_id") for h in hist if h.get("platform") == "youtube" and h.get("remote_id")]
            s = P.yt_stats(tok, yt_ids)
            if s.get("ok"):
                rows = s.get("totals", {}).get("rows") or [[0, 0, 0]]
                st["views"] = rows[0][0] if rows else 0
                st["watch_minutes"] = rows[0][1] if rows and len(rows[0]) > 1 else 0
                st["avg_view_sec"] = round(rows[0][2], 1) if rows and len(rows[0]) > 2 else 0
            st["top_countries"] = ", ".join(["%s(%s)" % (r[0], r[1]) for r in (s.get("countries") or [])[:5]])
    except Exception as e: log("yt stats: %s" % str(e)[:120])
    try:
        if os.getenv("GOOGLE_REFRESH_TOKEN"):
            dt = P.drive_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                               os.getenv("GOOGLE_REFRESH_TOKEN"))[0]
            u = P.drive_usage(dt)
            if u.get("ok"):
                st.update({"drive_total_gb": u.get("limit_gb"), "drive_free_gb": u.get("free_gb"),
                           "drive_used_gb": u.get("used_gb")})
    except Exception as e: log("drive usage: %s" % str(e)[:120])
    h = C.load_history()
    st["total_done"] = len(h.get("done", {})); st["total_uploaded"] = len(h.get("uploaded", []))
    st["recent_uploads"] = h.get("uploaded", [])[-10:][::-1]
    C.status_patch({"stats": st, "run": {"mode": "idle", "detail": "স্ট্যাটাস আপডেট"}})
    try: TG.send("📊 <b>লাইভ আপডেট</b>\n" + TG.live_text())
    except Exception: pass
    log("status updated: %s" % json.dumps(st, ensure_ascii=False)[:300]); return st

def run_tick():
    cfg = C.load_config(); now = C.now_utc().astimezone(C.BST)
    hhmm = now.hour * 60 + now.minute
    win = cfg.get("edit_window_bst", ["01:00", "06:00"])
    ws = C.hhmm(win[0]) or (1, 0); we = C.hhmm(win[1]) or (6, 0)
    start, end = ws[0]*60 + ws[1], we[0]*60 + we[1]
    inside = (start <= hhmm < end) if start < end else (hhmm >= start or hhmm < end)
    acted = []; today = now.strftime("%Y-%m-%d")
    already = C.load_status().get("run", {}).get("edit_triggered_date")
    if inside and not cfg.get("dry_run") and already != today:
        C.dispatch("ssk-edit", {"slot": now.strftime("%H:%M")})
        C.status_patch({"run": {"edit_triggered_date": today, "mode": "scheduler",
                                "detail": "এডিট জব চালু করা হয়েছে %s BST" % now.strftime("%H:%M")}})
        acted.append("edit")
    try:
        m = C.load_json("manual.json", {"items": []})
        if any(i.get("status") == "pending" for i in m.get("items", [])):
            C.dispatch("ssk-manual", {"at": now.strftime("%H:%M")}); acted.append("manual")
    except Exception: pass
    q = C.load_queue()
    for it in q:
        try: slot = datetime.datetime.strptime(it["slot_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=C.UTC)
        except Exception: slot = C.now_utc()
        if slot <= C.now_utc() or it.get("local_path"):
            C.dispatch("ssk-publish", {"at": now.strftime("%H:%M")}); acted.append("publish"); break
    C.dispatch("ssk-bot", {})
    C.status_patch({"run": {"mode": "scheduler",
                            "detail": "টিক %s BST · %s" % (now.strftime("%H:%M"), ",".join(acted) or "অপেক্ষা"),
                            "edit_window": "%s–%s BST" % (win[0], win[1]), "in_window": inside},
                    "queue": [{"platform": i.get("platform"), "slot_bst": i.get("slot_bst"),
                               "title": (i.get("title") or "")[:60], "size_mb": i.get("size_mb")} for i in q]})
    log("tick %s BST → %s" % (now.strftime("%H:%M"), acted or "কিছু করার নেই")); return acted

def run_manual():
    cfg = C.load_config(); m = C.load_json("manual.json", {"items": []})
    items = m.get("items", []); pend = [i for i in items if i.get("status") == "pending"]
    if not pend: log("manual: অপেক্ষারত কিছু নেই"); return 0
    for it in pend:
        try:
            src = it.get("src", "")
            if not src: it["status"] = "failed"; it["error"] = "লিংক নেই"; continue
            if it.get("platform") not in ("facebook", "youtube"): it["platform"] = "youtube"
            log("📥 ম্যানুয়াল আপলোড ডাউনলোড: %s" % src[:70])
            item = {"video_id": "manual_%s" % it.get("id", int(time.time())), "platform": it["platform"],
                    "title": (it.get("title") or "নতুন ভিডিও")[:100], "description": it.get("desc") or "",
                    "tags": it.get("tags") or ["SSK Drama"], "source_title": it.get("title") or "manual",
                    "channel": "manual", "size_mb": 0, "duration_min": 0, "manual": True,
                    "edited_at_bst": C.now_utc().astimezone(C.BST).strftime("%Y-%m-%d %H:%M")}
            if str(it.get("time_bst", "now")).lower() in ("now", "", "এখন"):
                wd = "/tmp/manual_%s" % item["video_id"]; shutil.rmtree(wd, ignore_errors=True)
                if "youtube.com" in src or "youtu.be" in src:
                    p = download(src, wd, cookies_file())
                else:
                    os.makedirs(wd, exist_ok=True); p = os.path.join(wd, "manual.mp4")
                    if src.startswith("drive:"):
                        dt = P.drive_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"),
                                           os.getenv("GOOGLE_REFRESH_TOKEN"))[0]
                        rr = P.drive_download(dt, src.replace("drive:", "").strip(), p)
                        if not rr.get("ok"): raise RuntimeError("drive download: %s" % rr.get("error"))
                    else:
                        rr = requests.get(src, stream=True, timeout=900)
                        with io.open(p, "wb") as f:
                            for b in rr.iter_content(262144): f.write(b)
                item["local_path"] = p
                item["slot_utc"] = C.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"); item["slot_bst"] = "এখনই"
            else:
                slot = C.next_upload_slot([it["time_bst"]], C.now_utc(), cfg.get("min_edit_lead_hours", 6))
                if slot is None: it["status"] = "failed"; it["error"] = "সময় নিয়ম ভাঙে"; continue
                item["slot_utc"] = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
                item["slot_bst"] = slot.astimezone(C.BST).strftime("%d %b %I:%M %p")
                item["src"] = src; item["time_bst"] = it["time_bst"]; item["pending_local_download"] = True
            q = C.load_queue(); q.append(item); C.save_queue(q); C.gh_put_json(C.QUEUE_FILE, q, "manual queue")
            it["status"] = "queued"
            TG.send("📤 <b>ম্যানুয়াল আপলোড কিউতে যোগ হয়েছে</b>\n🎬 %s\n📱 %s · ⏰ %s"
                    % (item["title"][:70], item["platform"].upper(), item["slot_bst"]))
        except Exception as e:
            it["status"] = "failed"; it["error"] = str(e)[:200]
            log("manual fail: %s" % str(e)[:180], "error"); TG.send("⚠️ ম্যানুয়াল আপলোড ফেইল: %s" % str(e)[:200])
    C.gh_put_json("manual.json", m, "manual: processed")
    run_publish(force=os.getenv("FORCE_PUBLISH") == "1"); return len(pend)

def run_selftest():
    out = []
    for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FB_PAGE_ID", "FB_ACCESS_TOKEN", "GEMINI_API_KEY",
              "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "GOOGLE_REFRESH_TOKEN", "YT_COOKIES"]:
        out.append("%s: %s" % (k, "সেট আছে ✅" if os.getenv(k) else "নেই ❌"))
    test = "/tmp/ssk_test.mp4"
    if not os.path.exists(test):
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=26",
                        "-f", "lavfi", "-i", "sine=frequency=300:duration=26", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "aac", "-shortest", test],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = os.path.exists(test); out.append("টেস্ট ক্লিপ: %s" % ("তৈরি ✅" if ok else "ব্যর্থ ❌"))
    if ok:
        cfg = C.load_config(); cfg["whisper_model"] = "tiny"; import studio
        for plat in ("youtube", "facebook"):
            try:
                p, meta = studio.make_edited(test, "/tmp/out_%s.mp4" % plat, cfg, plat, "/tmp/st_%s" % plat)
                out.append("%s এডিট: ✅ %s · %s · %.1f MB" % (plat, meta["dims"], meta["duration"], meta["size_mb"]))
            except Exception as e: out.append("%s এডিট: ❌ %s" % (plat, str(e)[:180]))
    txt = "\n".join(out); print(txt); TG.send("🧪 <b>সেলফ-টেস্ট</b>\n" + txt); return txt

def main():
    mode = (os.getenv("MODE") or (sys.argv[1] if len(sys.argv) > 1 else "status")).strip()
    if mode not in MODES: mode = "status"
    log("▶️ MODE=%s · %s (BST)" % (mode, C.now_utc().astimezone(C.BST).strftime("%d %b %Y %I:%M %p")))
    try:
        if mode == "edit": run_edit()
        elif mode == "publish": run_publish(force=os.getenv("FORCE_PUBLISH") == "1")
        elif mode == "bot": TG.poll_loop(int(os.getenv("BOT_SECONDS", "280")), log)
        elif mode == "tick": run_tick()
        elif mode == "manual": run_manual()
        elif mode == "self-test": run_selftest()
        else: run_status()
    except Exception as e:
        log("FATAL: %s" % str(e)[:300], "error"); log(traceback.format_exc()[-600:], "error")
        try: TG.send("🛑 <b>জরুরি ত্রুটি</b>\n%s" % str(e)[:300])
        except Exception: pass
        raise

if __name__ == "__main__": main()
