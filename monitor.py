"""monitor.py — SSK DRAMA status, metrics, AI assistant, Telegram, cleanup."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

import core
from core import CoreError, bd_now, bd_hm, bd_stamp, clean_error, env
from core import assistant_reply, json_from, telegram, telegram_get_updates, telegram_answer_callback

PLATFORM_LABEL = {"facebook": "ফেসবুক", "youtube": "ইউটিউব"}
STATE_LABEL = {"ready": "প্রস্তুত", "uploading": "আপলোড হচ্ছে", "uploaded": "আপলোড হয়েছে",
               "done": "সম্পন্ন", "failed": "ব্যর্থ", "pending": "অপেক্ষায়", "review": "যাচাই দরকার"}


def job_list() -> dict:
    """শুধু আসল জব (facebook-১ / youtube-১) — অন্য কী বাদ।"""
    return {k: v for k, v in core.jobs().items() if re.fullmatch(r"(facebook|youtube)-\d+", k)}


def snapshot(cfg: dict, drive: dict | None = None) -> dict:
    jobs = job_list()
    counts = {}
    for job in jobs.values():
        counts[job.get("state", "pending")] = counts.get(job.get("state", "pending"), 0) + 1
    latest = sorted(jobs.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)[:24]
    log, _ = core.read_json("log.json", default={"items": []})
    history, _ = core.read_json("history.json", default={"facebook": [], "youtube": []})
    totals = {"created": len(jobs),
              "uploaded": sum(len((history or {}).get(p) or []) for p in ("facebook", "youtube")),
              "failed": counts.get("failed", 0), "ready": counts.get("ready", 0),
              "editing": counts.get("editing", 0) + counts.get("preparing", 0),
              "uploading": counts.get("uploading", 0)}
    return {"updated_at": bd_stamp(), "now_bd": bd_now().strftime("%Y-%m-%d %H:%M"),
            "totals": totals,
            "paused": bool(cfg.get("paused")),
            "mode": cfg.get("mode", "live"),
            "panel": cfg.get("panel", ""),
            "counts": counts, "total_jobs": len(jobs),
            "jobs": {k: v for k, v in latest},
            "drive": drive or {},
            "log": (log.get("items") or [])[-60:],
            "settings": {"facebook_times_bd": cfg.get("facebook_times_bd"),
                         "youtube_times_bd": cfg.get("youtube_times_bd"),
                         "day_filter": cfg.get("day_filter"),
                         "keep_ratio": cfg.get("keep_ratio"),
                         "whisper_model": cfg.get("whisper_model")}}


# ------------------------------------------------------------------- metrics
def youtube_stats(remote_id: str, cfg: dict) -> dict:
    key = env("YOUTUBE_API_KEY")
    if not key:
        return {}
    res = requests.get("https://www.googleapis.com/youtube/v3/videos",
                       params={"part": "statistics,snippet", "id": remote_id, "key": key}, timeout=40)
    if res.status_code != 200:
        raise CoreError(f"YouTube statistics {res.status_code}")
    items = res.json().get("items") or []
    if not items:
        return {}
    st = items[0].get("statistics", {})
    return {"views": st.get("viewCount"), "likes": st.get("likeCount"),
            "comments": st.get("commentCount")}


def youtube_countries() -> list:
    """YouTube Analytics — refresh token-এ analytics scope না থাকলে চুপচাপ বাদ যায়।"""
    if not (env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET") and env("YOUTUBE_REFRESH_TOKEN")):
        return []
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("YOUTUBE_REFRESH_TOKEN"), "grant_type": "refresh_token"}, timeout=40)
    if res.status_code != 200:
        raise CoreError("YouTube Analytics token ব্যর্থ")
    token = res.json()["access_token"]
    end, start = bd_now().date(), bd_now().date().fromordinal(bd_now().date().toordinal() - 28)
    rep = requests.get("https://youtubeanalytics.googleapis.com/v2/reports", params={
        "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
        "metrics": "views", "dimensions": "country", "sort": "-views", "maxResults": 12},
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if rep.status_code != 200:
        raise CoreError("YouTube Analytics report ব্যর্থ (scope নেই?)")
    return [{"country": row[0], "views": row[1]} for row in rep.json().get("rows", [])]


def facebook_video_stats(remote_id: str, cfg: dict) -> dict:
    version = cfg.get("facebook_graph_version", "v26.0")
    res = requests.get(f"https://graph.facebook.com/{version}/{remote_id}/video_insights",
                       params={"metric": "total_video_views,total_video_views_by_country_id",
                               "access_token": env("FB_ACCESS_TOKEN")}, timeout=45)
    if res.status_code != 200:
        raise CoreError(f"Facebook insights {res.status_code}")
    out = {}
    for block in res.json().get("data", []):
        for value in block.get("values", []):
            if block.get("name") == "total_video_views":
                out["views"] = value.get("value")
    return out


def metrics(cfg: dict) -> dict:
    out = {"youtube": {}, "facebook": {}, "youtube_countries": [], "facebook_countries": [],
           "warnings": []}
    history, _ = core.read_json("history.json", default={"facebook": [], "youtube": []})
    for platform in ("youtube", "facebook"):
        for record in (history or {}).get(platform, [])[-12:]:
            if not isinstance(record, dict) or not record.get("remote_id"):
                continue
            try:
                if platform == "youtube":
                    out["youtube"][record["remote_id"]] = youtube_stats(record["remote_id"], cfg)
                else:
                    out["facebook"][record["remote_id"]] = facebook_video_stats(record["remote_id"], cfg)
            except Exception as exc:
                out["warnings"].append(f"{platform.capitalize()} views: {clean_error(exc)}")
    try:
        out["youtube_countries"] = youtube_countries()
    except Exception as exc:
        out["warnings"].append(f"YouTube দেশভিত্তিক রিপোর্ট নেই: {clean_error(exc)}")
    try:
        version = cfg.get("facebook_graph_version", "v26.0")
        res = requests.get(f"https://graph.facebook.com/{version}/{env('FB_PAGE_ID')}/insights",
                           params={"metric": "page_fans_country", "period": "lifetime",
                                   "access_token": env("FB_ACCESS_TOKEN")}, timeout=45)
        if res.status_code == 200:
            for block in res.json().get("data", []):
                for value in block.get("values", []):
                    out["facebook_countries"] = value.get("value") or []
        else:
            raise CoreError(f"{res.status_code}")
    except Exception as exc:
        out["warnings"].append(f"ফেসবুক দর্শক দেশ (পেজ দর্শক, ভিডিও নয়): {clean_error(exc)}")
    return out


# ----------------------------------------------------------------- AI + chat
def live_text(cfg: dict, snap: dict, met: dict | None = None) -> str:
    lines = [f"SSK DRAMA LIVE — {snap.get('now_bd')} (বাংলাদেশ সময়)"]
    totals = snap.get("totals") or {}
    if totals:
        lines.append(f"এখন পর্যন্ত: তৈরি {totals.get('created', 0)} | আপলোড {totals.get('uploaded', 0)} "
                     f"| ব্যর্থ {totals.get('failed', 0)} | প্রস্তুত {totals.get('ready', 0)}")
    if cfg.get("paused"):
        lines.append("⚠️ সিস্টেম বিরতিতে আছে (/resume দিয়ে চালু করুন)")
    counts = snap.get("counts", {})
    lines.append("অবস্থা: " + ", ".join(
        f"{STATE_LABEL.get(k, k)} {v}" for k, v in sorted(counts.items())) or "কোনো কাজ নেই")
    up = (cfg.get("youtube_times_bd") or [])[:3]
    fp = (cfg.get("facebook_times_bd") or [])[:3]
    lines.append("ফেসবুক সময়: " + ", ".join(fp))
    lines.append("ইউটিউব সময়: " + ", ".join(up))
    for key, job in list(snap.get("jobs", {}).items())[:4]:
        report = job.get("report") or {}
        lines.append(f"• {PLATFORM_LABEL.get(job.get('platform'), job.get('platform'))} slot {job.get('slot')}"
                     f" — {STATE_LABEL.get(job.get('state'), job.get('state'))}"
                     + (f", দৈর্ঘ্য {report.get('edited_seconds')}s" if report else "")
                     + (f", {job.get('upload_time')} এ যাবে" if job.get("upload_time") else ""))
    if met:
        total = sum(int(v.get("views") or 0) for v in (met.get("youtube") or {}).values())
        lines.append(f"মোট ইউটিউব ভিউ (সর্বশেষ ১২টি): {total}")
        for row in (met.get("youtube_countries") or [])[:3]:
            lines.append(f"  {row['country']}: {row['views']}")
        if met.get("warnings"):
            lines.append("সতর্কতা: " + " | ".join(met["warnings"][:2]))
    log, _ = core.read_json("log.json", default={"items": []})
    for item in (log.get("items") or [])[-4:]:
        lines.append(f"– {item.get('at')} {item.get('text')}")
    return "\n".join(lines)[:3900]


def update_text(cfg: dict) -> str:
    problems = core.validate_config(cfg)
    lines = ["SSK DRAMA UPDATE — সেটিংস প্যানেল", "নিচের অপশনগুলো বদলাতে পারেন:"]
    lines.append("ফেসবুক চ্যানেল: " + ", ".join(str(c).split("@")[-1][:18] for c in (cfg.get("facebook_channels") or [])))
    lines.append("ইউটিউব চ্যানেল: " + ", ".join(str(c).split("@")[-1][:18] for c in (cfg.get("youtube_channels") or [])))
    lines.append("ফেসবুক সময়: " + ", ".join(cfg.get("facebook_times_bd") or []))
    lines.append("ইউটিউব সময়: " + ", ".join(cfg.get("youtube_times_bd") or []))
    lines.append(f"পুরোনো ভিডিও ফিল্টার: {cfg.get('day_filter')} দিন "
                 f"(বদলাতে: /day 1|6|8|30|60|90)")
    lines.append(f"রাখার অনুপাত: {cfg.get('keep_ratio')} (81.25% = ৪০ মিনিট → ~৩২.৫ মিনিট)")
    lines.append(f"Whisper: {cfg.get('whisper_model')} | মিউজিক: {cfg.get('music_enabled')}")
    lines.append("বদলানোর কমান্ড: /seturl facebook 1 <লিংক> | /settime youtube 2 14:30")
    lines.append("/day 30 | /ratio 0.8125 | /pause | /resume | /prepare | /status")
    if problems:
        lines.append("⚠️ সমস্যা: " + " | ".join(problems[:4]))
    return "\n".join(lines)[:3900]


def handle_message(cfg: dict, text: str, snap: dict, met: dict | None = None) -> str:
    raw = (text or "").strip()
    upper = raw.upper().replace("  ", " ")
    if not raw:
        return "বার্তা খালি।"
    if upper in ("/START", "/HELP"):
        return ("SSK DRAMA বট — কমান্ড:\n"
                "SSK DRAMA LIVE — এখন কী চলছে (কত ভিডিও তৈরি/আপলোড/ব্যর্থ)\n"
                "SSK DRAMA UPDATE — সব সেটিংস বদলানোর প্যানেল\n"
                "অথবা সোজা বাংলায় প্রশ্ন করুন, সাথে সাথে উত্তর পাবেন।")
    if "SSK DRAMA LIVE" in upper or upper == "/STATUS":
        return live_text(cfg, snap, met)
    if "SSK DRAMA UPDATE" in upper or upper == "/SETTINGS":
        return update_text(cfg)
    try:
        return assistant_reply(raw, {"status": snap, "metrics": met or {}},
                               model=cfg.get("gemini_model"))
    except CoreError as exc:
        return "উত্তর দিতে পারিনি: " + clean_error(exc)


def answer_chat_requests(cfg: dict, snap: dict, met: dict, limit: int = 5) -> int:
    done = 0
    try:
        listing = core.github("GET", f"/repos/{core.REPO}/contents/chat").json()
    except CoreError:
        return 0
    for entry in listing if isinstance(listing, list) else []:
        if done >= limit or not str(entry.get("name", "")).endswith(".json"):
            continue
        try:
            payload, sha = core.read_json("chat/" + entry["name"], default={})
            if payload.get("state") not in ("pending", None):
                continue
            reply = handle_message(cfg, payload.get("message", ""), snap, met)
            payload["state"], payload["reply"], payload["answered_at"] = "done", reply, bd_stamp()
            core.write_json("chat/" + entry["name"], payload, "chat: answer", sha)
            done += 1
        except Exception as exc:
            core.log(f"চ্যাট উত্তর ব্যর্থ ({entry.get('name')}): {clean_error(exc)}", "warn")
    return done


def poll_telegram(cfg: dict, snap: dict, met: dict) -> int:
    if not cfg.get("telegram_updates_enabled") or not env("TELEGRAM_BOT_TOKEN"):
        return 0
    handled = 0
    for update in telegram_get_updates(timeout=5):
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = message.get("text") or ""
        cb = update.get("callback_query") or {}
        if cb:
            telegram_answer_callback(cb.get("id"), "পেয়েছি")
            continue
        if not text or chat_id != env("TELEGRAM_CHAT_ID"):
            continue
        reply = handle_message(cfg, text, snap, met)
        telegram(reply, chat_id=chat_id)
        handled += 1
    return handled


# ---------------------------------------------------------------- housekeeping
def cleanup(cfg: dict) -> dict:
    """Done ফাইল Drive থেকে মুছে ফেলে; queue/log ছোট রাখে।"""
    removed, deleted = 0, 0
    keep_seconds = int(cfg.get("storage_retention_days", 3)) * 86400
    now = time.time()
    for key, job in job_list().items():
        if job.get("state") == "done" and job.get("drive_file_id"):
            core.drive_delete(job["drive_file_id"])
            deleted += 1
            core.set_state(key, {"drive_file_id": None, "files_deleted_at": bd_stamp()})
        elif job.get("state") == "done":
            age = 0
            try:
                age = now - time.mktime(time.strptime(job.get("updated_at", "")[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                age = 0
            if age > keep_seconds:
                removed += 1
    return {"drive_deleted": deleted, "old_records": removed}


def service_cycle(cfg: dict) -> dict:
    """APP_MODE=service — snapshot, metrics, AI উত্তর, Telegram, পরিষ্কার করা।"""
    drive = {}
    try:
        from core import drive_space
        drive = drive_space()
    except Exception as exc:
        drive = {"error": clean_error(exc)}
    snap = snapshot(cfg, drive)
    met = metrics(cfg)
    snap["metrics"] = met
    answers = answer_chat_requests(cfg, snap, met)
    handled = poll_telegram(cfg, snap, met)
    house = cleanup(cfg)
    snap["cleanup"] = house
    snap["chat_answered"] = answers
    snap["telegram_handled"] = handled
    core.write_status(snap)
    if handled or answers:
        telegram(live_text(cfg, snap, met))
    core.log(f"service cycle: jobs={snap['total_jobs']} answers={answers} cleanup={house}")
    return snap


if __name__ == "__main__":
    cfg = core.load_config()
    print(json.dumps(service_cycle(cfg), ensure_ascii=False)[:2000])
