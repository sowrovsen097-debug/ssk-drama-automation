# orchestrator.py — মাস্টার কন্ট্রোল স্ক্রিপ্ট
import os, io, sys, json, time, glob, shutil, datetime, traceback, requests
import config as C
import publish as P
import bot as TG

MODES = ("edit", "publish", "bot", "tick", "status", "manual", "self-test")

def log(msg, level="info"):
    print(f"[{level.upper()}] {msg}", flush=True)
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
    o = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": False, "socket_timeout": 30, "retries": 3, "nocheckcertificate": True, "geo_bypass": True}
    if ck and os.path.exists(ck): o["cookiefile"] = ck
    o.update(extra or {}); return o

def pick_video(channel_url, cfg, history, ck=None):
    import yt_dlp
    min_days = int(cfg.get("min_age_days", 60)); max_min = float(cfg.get("max_duration_min", 80))
    with yt_dlp.YoutubeDL(ydl_opts({"extract_flat": "in_playlist", "playlistend": 40}, ck)) as ydl:
        info = ydl.extract_info(channel_url.rstrip("/") + "/videos", download=False)
        entries = [e for e in (info or {}).get("entries", []) if e]
        log(f"চ্যানেল পাওয়া গেছে: {len(entries)}টি ভিডিও")
        now = C.now_utc()
        for e in entries:
            vid = e.get("id")
            if not vid or vid in history.get("done", {}): continue
            dur = e.get("duration") or 0
            if dur and dur > max_min * 60: continue
            try:
                with yt_dlp.YoutubeDL(ydl_opts({}, ck)) as ydl_inner:
                    v = ydl_inner.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            except Exception:
                continue
            ud, d = v.get("upload_date"), v.get("duration") or dur
            if not ud: continue
            age = (now - datetime.datetime.strptime(ud, "%Y%m%d").replace(tzinfo=C.UTC)).days
            if age >= min_days and d and d <= max_min * 60:
                return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": v.get("title", ""), "description": (v.get("description") or "")[:3000], "duration": d, "age_days": age, "channel": channel_url}
    return None

def download(video_url, dest_dir, ck=None):
    import yt_dlp
    os.makedirs(dest_dir, exist_ok=True)
    tpl = os.path.join(dest_dir, "src.%(ext)s")
    opts = ydl_opts({"skip_download": False, "format": "bestvideo[height<=1080]+bestaudio/best", "outtmpl": tpl, "merge_output_format": "mp4"}, ck)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    hit = sorted(glob.glob(os.path.join(dest_dir, "src.*")))
    return hit[0] if hit else ""

def seo_metadata(title, description, platform):
    key = os.getenv("GEMINI_API_KEY", "")
    fallback = {"title": f"{title[:70]} | বিশেষ পর্ব", "description": "উপভোগ করুন আজকের পর্ব। #BanglaDrama", "tags": ["SSK Drama", "Bangla Drama", "Drama"]}
    if not key: return fallback
    prompt = f"প্ল্যাটফর্ম: {platform}\nটাইটেল: {title}\nবর্ণনা: {description}\n\nবাংলায় আকর্ষণীয় SEO টাইটেল, বর্ণনা ও ট্যাগ দাও শুধুমাত্র এই JSON ফরম্যাটে: {{\"title\":\"...\",\"description\":\"...\",\"tags\":[\"...\"]}}"
    for model in ("gemini-2.0-flash", "gemini-1.5-flash"):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", params={"key": key}, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=90)
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            j = json.loads(txt.replace("```json", "").replace("```", ""))
            out = fallback.copy(); out.update({k: v for k, v in j.items() if v})
            return out
        except Exception:
            continue
    return fallback

def run_edit():
    cfg = C.load_config(); history = C.load_history(); ck = cookies_file()
    t0 = time.time(); budget = 5.2 * 3600
    dtoken = P.drive_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"), os.getenv("GOOGLE_REFRESH_TOKEN"))[0] if os.getenv("GOOGLE_REFRESH_TOKEN") else None
    dfolder = P.drive_folder_id(dtoken) if dtoken else None
    jobs = ([{"url": u, "platform": "facebook"} for u in cfg["facebook_channels"] if u.strip()] + [{"url": u, "platform": "youtube"} for u in cfg["youtube_channels"] if u.strip()])
    
    made = 0
    for jb in jobs:
        if time.time() - t0 > budget: break
        try:
            v = pick_video(jb["url"], cfg, history, ck)
            if not v: continue
            wd = f"/tmp/work_{v['id']}"; shutil.rmtree(wd, ignore_errors=True)
            src = download(v["url"], wd, ck)
            if not src: continue
            import studio
            out = os.path.join(wd, f"edited_{jb['platform']}.mp4")
            final, meta = studio.make_edited(src, out, cfg, jb["platform"], os.path.join(wd, "build"))
            seo = seo_metadata(v["title"], v["description"], jb["platform"])
            item = {"video_id": v["id"], "platform": jb["platform"], "title": seo["title"], "description": seo["description"], "tags": seo.get("tags", []), "source_title": v["title"], "channel": jb["url"], "size_mb": meta["size_mb"], "duration_min": round(meta["duration"] / 60, 1), "edited_at_bst": C.now_utc().astimezone(C.BST).strftime("%Y-%m-%d %H:%M")}
            if dtoken:
                up = P.drive_upload(dtoken, dfolder, final, f"{jb['platform']}_{v['id']}.mp4")
                if up.get("ok"): item["drive_file_id"] = up["file_id"]
            slot = C.next_upload_slot(cfg["fb_upload_times_bst" if jb["platform"] == "facebook" else "yt_upload_times_bst"], C.now_utc(), cfg.get("min_edit_lead_hours", 6))
            if slot is None: continue
            item["slot_utc"] = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
            item["slot_bst"] = slot.astimezone(C.BST).strftime("%d %b %I:%M %p")
            q = C.load_queue()
            if item.get("drive_file_id"): item["local_path"] = final
            q.append(item); C.save_queue(q); C.gh_put_json(C.QUEUE_FILE, q, "queue update")
            C.mark_done(v["id"], {"platform": jb["platform"], "title": seo["title"]})
            C.gh_put_json(C.HISTORY_FILE, C.load_history(), "history update")
            made += 1
            TG.send(f"✅ <b>এডিট সম্পন্ন</b>\n🎬 {seo['title'][:80]}\n📱 {jb['platform'].upper()}\n⏰ আপলোড: <b>{item['slot_bst']}</b>")
        except Exception as e:
            TG.send(f"⚠️ <b>ফেইল</b> ({jb['platform']}): {str(e)[:150]}")
    return made

def run_publish(force=False):
    cfg = C.load_config(); q = C.load_queue()
    if not q: return 0
    now = C.now_utc().astimezone(C.BST); done = 0; keep = []
    dtoken = P.drive_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"), os.getenv("GOOGLE_REFRESH_TOKEN"))[0] if os.getenv("GOOGLE_REFRESH_TOKEN") else None
    yt_tok = P.yt_token(os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"), os.getenv("YOUTUBE_REFRESH_TOKEN"))[0]
    
    for it in q:
        due = False
        try:
            slot = datetime.datetime.strptime(it["slot_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=C.UTC)
            due = force or (now.astimezone(C.UTC) >= slot)
        except Exception:
            due = True
        if not due: keep.append(it); continue
        try:
            path = it.get("local_path", "")
            if not path or not os.path.exists(path):
                if dtoken and it.get("drive_file_id"):
                    path = f"/tmp/{it['video_id']}.mp4"
                    P.drive_download(dtoken, it["drive_file_id"], path)
            if it["platform"] == "facebook":
                res = P.fb_post_video(os.getenv("FB_PAGE_ID"), os.getenv("FB_ACCESS_TOKEN"), path, it["title"], it["description"])
            else:
                res = P.yt_upload(yt_tok, path, it["title"], it["description"], tags=it.get("tags"))
            if not res.get("ok"): raise RuntimeError(str(res.get("error")))
            C.mark_uploaded({"video_id": it.get("video_id"), "platform": it["platform"], "title": it["title"], "url": res.get("url")})
            C.gh_put_json(C.HISTORY_FILE, C.load_history(), "history uploaded")
            TG.send(f"🚀 <b>আপলোড সফল!</b>\n📱 {it['platform'].upper()}\n🔗 {res.get('url')}")
            if dtoken and it.get("drive_file_id"): P.drive_delete(dtoken, it["drive_file_id"])
            if path and os.path.exists(path): os.remove(path)
            done += 1
        except Exception as e:
            it["last_error"] = str(e)[:200]; keep.append(it)
            TG.send(f"⚠️ আপলোড ফেইল: {str(e)[:150]}")
    C.save_queue(keep); C.gh_put_json(C.QUEUE_FILE, keep, "queue publish")
    return done

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
        C.dispatch("ssk-edit", {})
        C.status_patch({"run": {"edit_triggered_date": today, "mode": "scheduler", "detail": f"এডিট চালু {now.strftime('%H:%M')} BST"}})
        acted.append("edit")
    q = C.load_queue()
    for it in q:
        try: slot = datetime.datetime.strptime(it["slot_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=C.UTC)
        except Exception: slot = C.now_utc()
        if slot <= C.now_utc() or it.get("local_path"):
            C.dispatch("ssk-publish", {}); acted.append("publish"); break
    C.dispatch("ssk-bot", {})
    return acted

def main():
    mode = (os.getenv("MODE") or (sys.argv[1] if len(sys.argv) > 1 else "status")).strip()
    if mode not in MODES: mode = "status"
    if mode == "edit": run_edit()
    elif mode == "publish": run_publish(force=os.getenv("FORCE_PUBLISH") == "1")
    elif mode == "bot": TG.poll_loop(280, log)
    elif mode == "tick": run_tick()
    elif mode == "status": C.status_patch({"run": {"mode": "idle", "detail": "সিস্টেম প্রস্তুত"}})
    elif mode == "self-test": print("SELF-TEST OK")

if __name__ == "__main__":
    main()
