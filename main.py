# -*- coding: utf-8 -*-
"""
SSK DRAMA 24/7 Automation — Main Orchestrator
ব্যবহার:
  python main.py edit     # রাত ১টা-৬টা: ডাউনলোড → অ্যানালাইসিস → এডিট → স্টোরেজ
  python main.py upload   # সেট করা টাইমে: স্টোরেজ → YouTube/Facebook → ডিলিট
  python main.py bot      # প্রতি ৫ মিনিটে: Telegram + AI চ্যাট + লাইভ স্টেটাস
  python main.py status   # শুধু অ্যানালিটিক্স / স্টেটাস রিফ্রেশ
"""
import argparse, datetime, json, os, random, shutil, sys, traceback

import ai_helper, notify
import storage_helper as st
import subtitle_engine as se
import uploaders as up
import video_editor as ve

WORK = "work"
ASPECT = {"yt": ("yt", 1920, 1080), "fb": ("fb", 1080, 1080)}
BUSY_PHASES = {"edit": "🎬 ভিডিও এডিট হচ্ছে", "upload": "🚀 আপলোড হচ্ছে",
               "bot": "🤖 বট চলছে"}


def cfg():
    return st.load_json("config.json", {})


def bd_now():
    return ve.bd_now()


# ---------------------------------------------------------------- edit ----
def edit_one(platform, idx, video, c, hist):
    tag = f"{platform.upper()}-{idx+1}"
    vid = video["id"]
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    notify.send(f"⏳ [{tag}] ডাউনলোড হচ্ছে\n{video['title'][:70]}")

    raw = ve.download(video["url"], WORK, c)
    if not raw:
        raise RuntimeError("ডাউনলোড ব্যর্থ")
    info = ve.probe(raw)
    dur = info["duration"]
    notify.send(f"🎬 [{tag}] মোট দৈর্ঘ্য {dur/60:.1f} মিনিট — অ্যানালাইসিস চলছে…")

    sil = ve.detect_silences(raw, c.get("silence_noise_db", -32),
                             c.get("silence_min_dur", 0.7)) if \
        c.get("features", {}).get("silence_trim", True) else []
    scenes, motion = ve.scene_analyze(raw, c.get("scene_sample_seconds", 0.5),
                                      c.get("scene_threshold", 0.32))
    ranges = ve.plan_segments(dur, scenes, motion, sil, c)
    total = sum(e - s for s, e in ranges)
    notify.send(f"✂️ [{tag}] {len(ranges)} টুকরোয় কাটা হলো → নতুন দৈর্ঘ্য "
                f"{total/60:.1f} মিনিট")

    # সাবটাইটেল (মূল অডিও থেকে একবার ট্রান্সক্রাইব → টাইমলাইনে রিম্যাপ)
    words = []
    if c.get("features", {}).get("subtitles", True):
        wav = os.path.join(WORK, "audio.wav")
        se.extract_audio(raw, wav)
        raw_words, engine = se.transcribe(wav, c.get("whisper_model", "small"),
                                          c.get("whisper_engine", "auto"),
                                          c.get("whisper_language") or None)
        words = se.remap_words(raw_words, ranges)
        try:
            os.remove(wav)
        except Exception:
            pass
        print(f"[subs] {engine} → {len(words)} শব্দ")

    # CC0 মিউজিক
    music = None
    if c.get("features", {}).get("music", True):
        q = random.choice(c.get("music_queries", ["cinematic tension"]))
        music = ai_helper.fetch_cc0_music(q, os.path.join(WORK, "music.mp3"))

    mode, W, H = ASPECT[platform]
    ass_path = None
    if words:
        ass_path = os.path.join(WORK, f"subs_{platform}.ass")
        se.build_ass(words, ass_path, playres=(W, H), mode=platform, cfg=c)

    out = os.path.join(WORK, f"pending-{platform}-{vid}.mp4")
    ve.render(raw, out, platform, W, H, ranges, ass_path,
              (music or {}).get("path"), c, total,
              brand=c.get("brand", {}).get("name", "SSK DRAMA"))

    o = ve.probe(out)
    if o["duration"] < total * 0.9:
        raise RuntimeError(f"রেন্ডার ভেরিফাই ফেইল ({o['duration']:.0f}s < {total:.0f}s)")
    print(f"[ok] {out} → {o['duration']/60:.2f} মিনিট, "
          f"{o['size']/1048576:.0f} MB, {o['width']}x{o['height']}")

    meta = ai_helper.generate_seo(video, c, total, ranges, words)
    meta.update({"platform": platform, "channel_index": idx,
                 "source_id": vid, "source_title": video["title"],
                 "source_url": video["url"], "duration": round(o["duration"], 2),
                 "resolution": f"{o['width']}x{o['height']}",
                 "music": (music or {}).get("name"),
                 "created_bd": bd_now().strftime("%Y-%m-%d %H:%M:%S"),
                 "ranges": [[round(s, 2), round(e, 2)] for s, e in ranges][:200]})

    st.upload_pending(out, meta, platform)
    hist.append(vid)
    shutil.rmtree(WORK, ignore_errors=True)

    st.update_status({"stage": "idle",
                      "message": f"[{tag}] এডিট শেষ, স্টোরেজে সেভ হয়েছে",
                      "last_edit": {"tag": tag, "video_id": vid,
                                    "title": meta.get("title"),
                                    "minutes": round(o["duration"] / 60, 1),
                                    "resolution": f"{o['width']}x{o['height']}",
                                    "words": len(words),
                                    "at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}})
    notify.send(f"✅ [{tag}] এডিট ও স্টোরেজ সফল\n"
                f"📌 {meta.get('title')}\n"
                f"⏱️ {o['duration']/60:.1f} মিনিট | 🎞️ {o['width']}x{o['height']}\n"
                f"💬 সাবটাইটেল: {len(words)} শব্দ | 🎵 {meta.get('music') or 'নেই'}\n"
                f"🗓️ আপলোড হবে তোমার সেট করা টাইমে।")
    return True


def cmd_edit(manual_target="auto"):
    c = cfg()
    hist = st.load_json("history.json", [])
    if not isinstance(hist, list):
        hist = []
    state = st.load_json("state.json", {})
    today = bd_now().strftime("%Y-%m-%d")
    done = state.setdefault("edit_done", {}).setdefault(today, [])

    jobs = [("fb", i, u) for i, u in enumerate(c.get("facebook_channels", []))] + \
           [("yt", i, u) for i, u in enumerate(c.get("youtube_channels", []))]
    if manual_target in ("fb", "yt"):
        jobs = [j for j in jobs if j[0] == manual_target]

    if not jobs:
        notify.send("⚠️ config.json-এ কোনো চ্যানেল নেই। অ্যাপ থেকে লিংক সেভ করো।")
        return 0

    notify.send("🌙 এডিট রান শুরু (০১:০০–০৬:০০ বাংলাদেশ সময়)")
    for platform, idx, url in jobs:
        key = f"{platform}{idx}"
        if key in done or not url:
            continue
        v = ve.pick_source_video(url, hist, c)
        if not v:
            done.append(key)
            st.save_json("state.json", state)
            notify.send(f"ℹ️ {platform.upper()}-{idx+1}: নতুন (এখনো প্রসেস হয়নি) "
                        f"ভিডিও পাওয়া যায়নি — স্কিপ।")
            continue
        try:
            edit_one(platform, idx, v, c, hist)
        except Exception as e:
            traceback.print_exc()
            notify.send(f"❌ এডিট ফেইল [{platform.upper()}-{idx+1}] {v.get('id')}\n{e}")
            st.save_json("state.json", state)
            st.commit_and_push()
            return 1
        done.append(key)
        st.save_json("history.json", hist)
        st.save_json("state.json", state)
        st.commit_and_push()
        return 0

    st.update_status({"stage": "idle", "message": "আজকের সব এডিট কাজ শেষ।"})
    st.commit_and_push()
    notify.send("😴 আজকের সব এডিট শেষ — এখন আপলোডের টাইমের অপেক্ষা।", silent=True)
    return 0


# -------------------------------------------------------------- upload ----
def upload_one(platform, asset, c):
    meta_asset = next((a for a in st.list_assets()
                       if a["name"] == asset["name"] + ".meta.json"), None)
    os.makedirs(WORK, exist_ok=True)
    local = os.path.join(WORK, asset["name"])
    notify.send(f"⬇️ স্টোরেজ থেকে নামছে: {asset['name']}")
    st.download_asset(asset["id"], local)
    meta = {}
    if meta_asset:
        mp = os.path.join(WORK, "meta.json")
        st.download_asset(meta_asset["id"], mp)
        meta = st.load_json(mp, {})

    if platform == "yt":
        res = up.upload_youtube(local, meta, c)
    else:
        res = up.upload_facebook(local, meta, c)

    # আপলোড শেষ → স্টোরেজ ও লোকাল ফাইল ডিলিট
    st.delete_asset(asset["id"])
    if meta_asset:
        st.delete_asset(meta_asset["id"])
    shutil.rmtree(WORK, ignore_errors=True)

    vstats = up.yt_video_stats(res["video_id"]) if platform == "yt" else None
    st.update_status({
        "stage": "idle",
        "message": f"{'YouTube' if platform=='yt' else 'Facebook'} আপলোড সফল: {meta.get('title','')[:50]}",
        "last_upload": {"platform": platform, "video_id": res["video_id"],
                        "url": res.get("url"), "title": meta.get("title"),
                        "at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
        "last_yt_stats": vstats})
    notify.send(f"🚀 আপলোড সফল ({'YouTube' if platform=='yt' else 'Facebook'})\n"
                f"📌 {meta.get('title')}\n🔗 {res.get('url')}\n"
                f"🧹 স্টোরেজ ফাইল ডিলিট করা হয়েছে।")
    return res


def cmd_upload(manual_platform="auto"):
    c = cfg()
    now = bd_now()
    ws, we = c.get("edit_window_bd", ["01:00", "06:00"])
    s_dt, e_dt = ve.hhmm_to_dt(now, ws), ve.hhmm_to_dt(now, we)
    if s_dt <= now <= e_dt:
        print("[upload] এডিট উইন্ডো চলছে, আপলোড বন্ধ।")
        return 0

    state = st.load_json("state.json", {})
    today = now.strftime("%Y-%m-%d")
    done = state.setdefault("upload_done", {}).setdefault(today, [])
    pend = st.list_pending()

    plans = [("yt", c.get("yt_upload_times_bd", [])),
             ("fb", c.get("fb_upload_times_bd", []))]
    if manual_platform in ("yt", "fb"):
        plans = [p for p in plans if p[0] == manual_platform]

    any_ok = False
    for platform, times in plans:
        for t in sorted(times):
            key = f"{platform}@{t}"
            if key in done:
                continue
            slot = ve.hhmm_to_dt(now, t)
            if now < slot:
                continue
            lst = pend.get(platform, [])
            if not lst:
                continue
            asset = lst[0]
            try:
                upload_one(platform, asset, c)
                done.append(key)
                any_ok = True
                st.save_json("state.json", state)
            except Exception as e:
                traceback.print_exc()
                notify.send(f"❌ আপলোড ফেইল {key}\n{e}")
            break  # প্রতি রানে প্রতি প্ল্যাটফর্মে একটা
    if not any_ok:
        print("[upload] এখন আপলোডের সময় হয়নি বা স্টোরেজে কিছু নেই।")

    # পুরনো দিনের হিসাব ছেঁটে দাও
    for k in list(state.get("upload_done", {})):
        if k < (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d"):
            state["upload_done"].pop(k, None)
    for k in list(state.get("edit_done", {})):
        if k < (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d"):
            state["edit_done"].pop(k, None)
    st.save_json("state.json", state)
    st.update_status({"stage": "idle" if any_ok else "waiting"})
    st.commit_and_push()
    return 0


# ----------------------------------------------------------------- bot ----
HELP = ("🤖 SSK DRAMA বট\n\n"
        "/status – পুরো সিস্টেমের লাইভ অবস্থা\n"
        "/stats – ভিউ ও দেশ অনুযায়ী পরিসংখ্যান\n"
        "/storage – স্টোরেজে জমা ভিডিও\n"
        "/edit – এখনই এডিট চালু (GitHub Actions)\n"
        "/upload – এখনই আপলোড চালু\n"
        "/help – এই মেসেজ\n\n"
        "যেকোনো সাধারণ প্রশ্ন লিখলে AI বাংলায় উত্তর দেবে।")


def trigger_workflow(filename, inputs=None):
    o, r = st.owner_repo()
    res = st.api("POST", f"https://api.github.com/repos/{o}/{r}/actions/workflows/{filename}/dispatches",
                 json={"ref": os.getenv("GITHUB_REF_NAME", "main"),
                       "inputs": inputs or {}})
    return res.status_code in (200, 204)


def status_text():
    s = st.load_json("status.json", {})
    p = st.pending_counts()
    lu = s.get("last_upload") or {}
    le = s.get("last_edit") or {}
    a = s.get("analytics") or {}
    yt = (a.get("youtube") or {})
    txt = (f"📊 SSK DRAMA লাইভ স্টেটাস\n"
           f"🗓️ {bd_now().strftime('%d %b %Y, %I:%M %p')} (বাংলাদেশ)\n\n"
           f"🎬 শেষ এডিট: {le.get('title') or '—'} ({le.get('minutes','—')} মিনিট)\n"
           f"🚀 শেষ আপলোড: {lu.get('title') or '—'}\n"
           f"🔗 {lu.get('url') or '—'}\n\n"
           f"📦 স্টোরেজে জমা: ফেসবুক {p.get('fb',0)} টা | ইউটিউব {p.get('yt',0)} টা\n")
    if yt.get("views") is not None:
        txt += f"👁️ ইউটিউব ভিউ (৭ দিন): {yt.get('views')}\n"
        for ctry in (yt.get("countries") or [])[:5]:
            txt += f"   • {ctry['country']}: {ctry['views']}\n"
    return txt


def cmd_bot():
    c = cfg()
    s = st.load_json("status.json", {})
    off = st.load_json("telegram_offset.json", {"offset": 0}).get("offset", 0)

    # 1) টেলিগ্রাম মেসেজ হ্যান্ডেল
    for up_ in notify.get_updates(off):
        off = max(off, up_.get("update_id", 0) + 1)
        msg = up_.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        if low.startswith("/status"):
            notify.send(status_text())
        elif low.startswith("/stats"):
            notify.send(analytics_text())
        elif low.startswith("/storage"):
            p = st.list_pending()
            lines = [f"📦 ফেসবুক: {len(p['fb'])} টা", f"📦 ইউটিউব: {len(p['yt'])} টা"]
            for pl in ("fb", "yt"):
                for a in p[pl][:5]:
                    lines.append(f"  • {a['name']} ({round(a.get('size',0)/1048576,1)} MB)")
            notify.send("\n".join(lines) or "স্টোরেজ খালি।")
        elif low.startswith("/edit"):
            ok = trigger_workflow("edit.yml", {"target": "auto"})
            notify.send("✅ এডিট রান চালু করা হয়েছে।" if ok else "❌ চালু করা যায়নি।")
        elif low.startswith("/upload"):
            ok = trigger_workflow("upload.yml", {"platform": "auto"})
            notify.send("✅ আপলোড রান চালু করা হয়েছে।" if ok else "❌ চালু করা যায়নি।")
        elif low.startswith("/help") or low.startswith("/start"):
            notify.send(HELP)
        else:
            notify.send("🤔 ভাবছি… একটু অপেক্ষা করো।")
            ans = ai_helper.ask_ai(text, json.dumps(
                {"config": c, "status": s}, ensure_ascii=False)[:3000])
            notify.send("🤖 " + ans)
    st.save_json("telegram_offset.json", {"offset": off})

    # 2) অ্যাপের AI চ্যাট রিলে
    chat = st.load_json("chat.json", {})
    if chat.get("question") and not chat.get("answer"):
        ans = ai_helper.ask_ai(chat["question"], json.dumps(
            {"config": c, "status": s}, ensure_ascii=False)[:3000])
        chat["answer"] = ans
        chat["answered_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        st.save_json("chat.json", chat)

    # 3) লাইভ স্টেটাস (ড্যাশবোর্ড + টেলিগ্রাম)
    last_commit = s.get("status_committed_at", "")
    now_u = datetime.datetime.utcnow()
    do_commit = True
    if last_commit:
        try:
            dt = datetime.datetime.strptime(last_commit, "%Y-%m-%dT%H:%M:%SZ")
            do_commit = (now_u - dt).total_seconds() >= \
                c.get("status_commit_minutes", 5) * 60
        except Exception:
            pass

    patch = {"stage": "idle", "bot_last_run": now_u.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if do_commit:
        try:
            patch["analytics"] = {
                "youtube": up.yt_channel_analytics(7),
                "facebook": up.fb_video_insights(
                    (s.get("last_upload") or {}).get("video_id")
                    if (s.get("last_upload") or {}).get("platform") == "fb" else None),
                "at": now_u.strftime("%Y-%m-%dT%H:%M:%SZ")}
        except Exception as e:
            print("[analytics]", e)
        patch["status_committed_at"] = now_u.strftime("%Y-%m-%dT%H:%M:%SZ")
    st.update_status(patch)

    # টেলিগ্রামে নির্দিষ্ট বিরতিতে আপডেট
    tb_last = s.get("telegram_last_sent", "")
    due = True
    if tb_last:
        try:
            dt = datetime.datetime.strptime(tb_last, "%Y-%m-%dT%H:%M:%SZ")
            due = (now_u - dt).total_seconds() >= \
                c.get("telegram_update_minutes", 30) * 60
        except Exception:
            pass
    if due:
        notify.send(status_text(), silent=True)
        st.update_status({"telegram_last_sent": now_u.strftime("%Y-%m-%dT%H:%M:%SZ")})

    if do_commit:
        st.commit_and_push()
    else:
        st.save_json("status.json", st.load_json("status.json", {}))
    return 0


def analytics_text():
    a = (st.load_json("status.json", {}).get("analytics") or {})
    yt = a.get("youtube") or {}
    fb = a.get("facebook") or {}
    t = ["📈 পরিসংখ্যান (বাংলাদেশ সময় " + bd_now().strftime("%d %b %I:%M %p") + ")"]
    if yt.get("views") is not None:
        t.append(f"▶️ ইউটিউব ভিউ (৭ দিন): {yt['views']}")
        t.append(f"⏱️ দেখা হয়েছে: {yt.get('minutes') or 0} মিনিট")
        for ctry in (yt.get("countries") or [])[:8]:
            t.append(f"   • {ctry['country']}: {ctry['views']}")
    elif yt.get("error"):
        t.append("▶️ ইউটিউব অ্যানালিটিক্স পাওয়া যায়নি (স্কোপ চেক করুন)")
    if fb:
        t.append(f"📘 ফেসবুক ভিউ: {fb.get('views')}")
    return "\n".join(t)


# -------------------------------------------------------------- status ----
def cmd_status():
    now_u = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    st.update_status({"analytics": {
        "youtube": up.yt_channel_analytics(7), "facebook": None, "at": now_u},
        "status_committed_at": now_u})
    st.commit_and_push(["status.json"])
    return 0


# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["edit", "upload", "bot", "status"])
    args = ap.parse_args()
    try:
        if args.mode == "edit":
            return cmd_edit(os.getenv("MANUAL_TARGET", "auto"))
        if args.mode == "upload":
            return cmd_upload(os.getenv("MANUAL_PLATFORM", "auto"))
        if args.mode == "bot":
            return cmd_bot()
        return cmd_status()
    except Exception as e:
        traceback.print_exc()
        notify.send(f"❌ SSK ERROR ({args.mode}): {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
