"""main.py — SSK DRAMA orchestrator.  APP_MODE = prepare | upload | service

prepare : রাত ১টা–৬টা (BD) — সোর্স খুঁজে নামানো, এডিট, Google Drive বাফারে রাখা
upload  : নির্ধারিত সময়ে Drive → ফেসবুক/ইউটিউব, নিশ্চিত হলে Drive ফাঁকা
service : monitor.service_cycle (স্টেটাস, মেট্রিক, AI উত্তর, Telegram, ক্লিনআপ)
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests

import core
import editor
from core import CoreError, bd_hm, bd_now, bd_stamp, clean_error, env
from monitor import job_list, service_cycle

WORK = Path(os.environ.get("APP_WORK", "work"))
PLATFORMS = ("facebook", "youtube")
REMOTE_TAG = "SSK DRAMA"


# ------------------------------------------------------------------ settings
def slots(cfg: dict) -> list:
    out = []
    for platform in PLATFORMS:
        channels = cfg.get(f"{platform}_channels") or []
        times = cfg.get(f"{platform}_times_bd") or []
        for index in range(3):
            out.append({"platform": platform, "slot": index + 1,
                        "key": f"{platform}-{index + 1}",
                        "channel": channels[index] if index < len(channels) else None,
                        "time": times[index] if index < len(times) else None})
    return out


def source_usage() -> set:
    used = set()
    for platform in PLATFORMS:
        used |= core.history_ids(platform)
    for job in job_list().values():
        for field in ("source_url", "source_id"):
            if job.get(field):
                used.add(job[field])
    return used


# ------------------------------------------------------------ source finding
def channel_videos(url: str, cfg: dict) -> list:
    """yt-dlp দিয়ে চ্যানেলের ভিডিওর তালিকা — মূল লিংক ছোঁয়া হয় না, শুধু মেটাডেটা।"""
    import yt_dlp
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "ignoreerrors": True,
               "playlistend": int(cfg.get("source_scan_limit", 200)),
               "extractor_args": {"youtube": {"player_client": ["web_safari", "tv", "web"]}}}
    cookies = env("YT_COOKIES")
    if cookies:
        jar = WORK / "cookies.txt"
        jar.parent.mkdir(parents=True, exist_ok=True)
        jar.write_text(cookies, encoding="utf-8")
        options["cookiefile"] = str(jar)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in (info.get("entries") or []) if e]


def pick_source(platform: str, slot: dict, cfg: dict) -> dict:
    """day_filter (1/6/8/30/60/90) অনুযায়ী পুরোনো ভিডিও; ৮০ মিনিটের বেশি বাদ।"""
    day_filter = int(cfg.get("day_filter", 60))
    max_seconds = float(cfg.get("max_duration_minutes", 80)) * 60
    min_seconds = float(cfg.get("min_duration_seconds", 60))
    used = source_usage()
    now = time.time()
    candidates, problems = [], []
    for channel in [slot["channel"]] + [c["channel"] for c in slots(cfg)
                                        if c["platform"] == platform and c["channel"] != slot["channel"]]:
        if not channel:
            continue
        try:
            for entry in channel_videos(channel, cfg):
                vid = entry.get("id")
                url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
                stamp = entry.get("timestamp") or entry.get("release_timestamp")
                if not vid or url in used or vid in used:
                    continue
                duration = float(entry.get("duration") or 0)
                if duration and (duration > max_seconds or duration < min_seconds):
                    continue
                age_days = (now - float(stamp)) / 86400 if stamp else None
                if age_days is None or age_days < day_filter:
                    continue
                candidates.append({"url": url, "id": vid, "title": entry.get("title") or "",
                                   "duration": duration, "age_days": round(age_days, 1),
                                   "views": entry.get("view_count") or 0, "channel": channel})
        except Exception as exc:
            problems.append(f"{channel}: {clean_error(exc)}")
    if problems:
        core.log("সোর্স স্ক্যান সতর্কতা: " + " | ".join(problems[:2]), "warn")
    if not candidates:
        raise CoreError(f"{platform} slot {slot['slot']}: {day_filter} দিনের পুরোনো, "
                        f"৮০ মিনিটের কম দৈর্ঘ্যের নতুন ভিডিও পাওয়া যায়নি")
    candidates.sort(key=lambda item: (item["age_days"], -item["views"]))
    return candidates[0]


def download_source(source: dict, folder: Path, cfg: dict) -> str:
    import yt_dlp
    folder.mkdir(parents=True, exist_ok=True)
    options = {"quiet": True, "no_warnings": True, "ignoreerrors": True,
               "format": "bv*[height<=1080][vcodec!*=av01]+ba/b[height<=1080]/bv*+ba/b",
               "merge_output_format": "mp4", "retries": 5, "fragment_retries": 5,
               "concurrent_fragment_downloads": 4,
               "outtmpl": str(folder / "source.%(ext)s"),
               "extractor_args": {"youtube": {"player_client": ["web_safari", "tv", "web"]}}}
    cookies = env("YT_COOKIES")
    if cookies:
        jar = WORK / "cookies.txt"
        jar.write_text(cookies, encoding="utf-8")
        options["cookiefile"] = str(jar)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([source["url"]])
    for path in sorted(folder.glob("source.*")):
        if path.suffix.lower() in (".mp4", ".mkv", ".webm"):
            if path.suffix.lower() != ".mp4":
                target = folder / "source.mp4"
                editor.run([editor.FFMPEG, "-y", "-i", str(path), "-c", "copy", str(target)], timeout=3600)
                return str(target)
            return str(path)
    raise CoreError("ডাউনলোড করা ফাইল পাওয়া যায়নি")


def seo_package(source: dict, report: dict, platform: str, cfg: dict) -> dict:
    """Gemini দিয়ে title/description/tags/keywords — ভিডিওর নিজের ভাষায়।"""
    if env("GEMINI_API_KEY"):
        prompt = (f"Hindi crime-drama episode.\nOriginal title: {source['title']}\n"
                  f"Platform: {platform}\nLength after edit: {report.get('edited_seconds')} seconds\n"
                  "Write new, non-duplicate SEO metadata. Reply strict JSON only with keys "
                  '"title" (max 95 chars), "description" (2 short paragraphs, no URLs), '
                  '"tags" (max 15 items), "keywords" (max 10 items). Same language as the title.')
        try:
            pack = core.json_from(core.gemini(prompt, model=cfg.get("gemini_model"), max_tokens=900), default={})
            if pack.get("title"):
                pack.setdefault("description", source["title"])
                pack.setdefault("tags", [])
                pack.setdefault("keywords", [])
                pack["generated_by"] = "gemini"
                return pack
        except CoreError as exc:
            core.log(f"SEO জেনারেশন ব্যর্থ, মূল টাইটেল ব্যবহার: {clean_error(exc)}", "warn")
    return {"title": source["title"][:95], "description": f"{source['title']}\n{REMOTE_TAG}",
            "tags": [], "keywords": [], "generated_by": "fallback"}


# ------------------------------------------------------------------- prepare
def prepare_slot(cfg: dict, slot: dict, state: dict) -> dict:
    folder = WORK / slot["key"]
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    core.set_state(slot["key"], {"state": "downloading", "platform": slot["platform"],
                                "slot": slot["slot"], "upload_time": slot["time"]})
    source = state.get("source") or pick_source(slot["platform"], slot, cfg)
    core.log(f"{slot['key']}: সোর্স — {source['title'][:60]} ({source['age_days']} দিন পুরোনো)")
    path = state.get("path") or download_source(source, folder, cfg)
    core.set_state(slot["key"], {"state": "editing", "source_url": source["url"],
                                "source_id": source["id"], "source_title": source["title"],
                                "source_age_days": source["age_days"]})
    result = editor.edit_video(path, slot["platform"], cfg, folder)
    seo = seo_package(source, result["report"], slot["platform"], cfg)
    folder_id = core.drive_ensure_folder(cfg.get("drive_buffer_folder", "SSK DRAMA BUFFER"))
    need = result["size_bytes"]
    try:
        quota = core.drive_space()
        free = int(quota.get("limit") or 0) - int(quota.get("usage") or 0)
        if free and free < need * 1.1:
            raise CoreError(f"Drive-এ খালি জায়গা {free / 1073741824:.1f} GB, দরকার "
                            f"{need / 1073741824:.1f} GB — আগের ভিডিও আপলোড হলে জায়গা খালি হবে")
    except CoreError:
        raise
    except Exception as exc:
        core.log(f"Drive কোটা পড়া যায়নি (আপলোড চলবে): {clean_error(exc)}", "warn")
    asset = core.drive_upload(result["file"], f"{slot['key']}.mp4", folder_id)
    return {"state": "ready", "platform": slot["platform"], "slot": slot["slot"],
            "key": slot["key"], "upload_time": slot["time"],
            "drive_file_id": asset.get("id"), "drive_link": asset.get("webViewLink"),
            "size_bytes": result["size_bytes"], "duration": result["duration"],
            "report": result["report"], "edits_applied": result["edits_applied"],
            "edits_count": result["edits_count"], "subtitles": result["subtitles"],
            "notes": result.get("notes", []),
            "title": seo["title"], "description": seo["description"],
            "tags": seo["tags"], "keywords": seo["keywords"], "seo_source": seo.get("generated_by"),
            "source_url": source["url"], "source_title": source["title"],
            "prepared_at": bd_stamp()}


def prepare_cycle(cfg: dict, force: bool = False) -> dict:
    problems = core.validate_config(cfg)
    if problems:
        raise CoreError("config সমস্যা: " + " | ".join(problems))
    if cfg.get("paused"):
        core.log("সিস্টেম বিরতিতে — prepare বন্ধ")
        return {"skipped": "paused"}
    hour = bd_now().hour
    if not force and not (1 <= hour < 6):
        core.log(f"এখন {bd_hm()} BD — এডিটিং সময় ০১:০০–০৬:০০, তাই বন্ধ")
        return {"skipped": "outside window"}
    budget = float(env("APP_BUDGET_SECONDS", "19800") or 19800)
    started = time.time()
    done, failed = [], []
    for slot in slots(cfg):
        if time.time() - started > budget:
            core.log("সময় শেষ — বাকি স্লট পরের রানে")
            break
        current = core.get_state(slot["key"])
        if current.get("state") in ("ready", "uploading", "uploaded"):
            continue
        try:
            core.set_state(slot["key"], {"state": "preparing", "attempt": int(current.get("attempt", 0)) + 1})
            result = prepare_slot(cfg, slot, {})
            core.set_state(slot["key"], result)
            done.append(slot["key"])
            core.log(f"{slot['key']}: প্রস্তুত — {result['duration']:.0f}s, "
                     f"{result['size_bytes'] / 1048576:.0f} MB, Drive-এ রাখা হলো")
            core.telegram(f"SSK: {slot['platform']} slot {slot['slot']} প্রস্তুত "
                          f"({result['duration']:.0f}s, {result['size_bytes'] / 1048576:.0f} MB) — "
                          f"{slot['time']} (BD) এ যাবে")
        except Exception as exc:
            core.set_state(slot["key"], {"state": "failed", "error": clean_error(exc)})
            failed.append(slot["key"])
            core.log(f"{slot['key']}: ব্যর্থ — {clean_error(exc)}", "error")
            core.telegram(f"SSK: প্রস্তুতি ব্যর্থ {slot['platform']} slot {slot['slot']}\n"
                          f"{clean_error(exc)}")
        finally:
            shutil.rmtree(WORK / slot["key"], ignore_errors=True)
    core.set_state("progress", {"stage": "night preparation finished", "done": done, "failed": failed})
    return {"done": done, "failed": failed}


# -------------------------------------------------------------------- upload
def ensure_youtube_token() -> str:
    if not (env("GOOGLE_CLIENT_ID") and env("YOUTUBE_REFRESH_TOKEN")):
        raise CoreError("ইউটিউব আপলোডের জন্য GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / "
                        "YOUTUBE_REFRESH_TOKEN দরকার")
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("YOUTUBE_REFRESH_TOKEN"), "grant_type": "refresh_token"}, timeout=40)
    if res.status_code != 200:
        raise CoreError(f"YouTube token {res.status_code}: {res.text[:200]}")
    return res.json()["access_token"]


def upload_youtube(job: dict, path: str, cfg: dict) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials(token=ensure_youtube_token(), refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
                        client_id=env("GOOGLE_CLIENT_ID"), client_secret=env("GOOGLE_CLIENT_SECRET"),
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    publish = (cfg.get("publish") or {}).get("youtube", {})
    body = {"snippet": {"title": job["title"][:95], "description": job["description"][:4900],
                        "tags": [str(t)[:40] for t in (job.get("tags") or [])][:15],
                        "categoryId": str(publish.get("categoryId", "24"))},
            "status": {"privacyStatus": publish.get("privacyStatus", "public"),
                       "selfDeclaredMadeForKids": bool(publish.get("madeForKids", False))}}
    media = MediaFileUpload(path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries = None, 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except Exception as exc:
            retries += 1
            if retries > 6:
                raise CoreError(f"YouTube আপলোড ব্যর্থ: {clean_error(exc)}")
            time.sleep(min(60, 5 * retries))
    return response["id"]


def upload_facebook(job: dict, path: str, cfg: dict) -> str:
    version = cfg.get("facebook_graph_version", "v26.0")
    page, token = env("FB_PAGE_ID"), env("FB_ACCESS_TOKEN")
    if not (page and token):
        raise CoreError("FB_PAGE_ID / FB_ACCESS_TOKEN সেট করা নেই")
    size = os.path.getsize(path)
    start = requests.post(f"https://graph-video.facebook.com/{version}/{page}/videos",
                          data={"upload_phase": "start", "file_size": size,
                                "access_token": token}, timeout=60)
    if start.status_code != 200:
        raise CoreError(f"Facebook start {start.status_code}: {start.text[:200]}")
    plan = start.json()
    session, video_id, offset, chunk = plan["upload_session_id"], plan["video_id"], 0, 8 * 1024 * 1024
    with open(path, "rb") as handle:
        while offset < size:
            handle.seek(offset)
            blob = handle.read(chunk)
            res = requests.post("https://rupload.facebook.com/video-upload/" + version + "/" + video_id,
                               headers={"Authorization": "OAuth " + token,
                                        "offset": str(offset), "file_size": str(size),
                                        "Content-Type": "application/octet-stream"},
                               data=blob, timeout=900)
            if res.status_code not in (200, 201):
                raise CoreError(f"Facebook chunk {res.status_code}: {res.text[:200]}")
            offset += len(blob)
    finish = requests.post(f"https://graph-video.facebook.com/{version}/{page}/videos",
                           data={"upload_phase": "finish", "upload_session_id": session,
                                 "title": job["title"][:200], "description": job["description"][:4900],
                                 "access_token": token}, timeout=300)
    if finish.status_code != 200:
        raise CoreError(f"Facebook finish {finish.status_code}: {finish.text[:200]}")
    if not finish.json().get("success", True):
        raise CoreError("Facebook finish সফল হয়নি")
    job["facebook_session"] = session
    return video_id


def confirm_upload(platform: str, remote_id: str, cfg: dict, attempts: int = 6) -> bool:
    """আপলোড সত্যিই হয়েছে কি না — YouTube/Facebook থেকে ফিরে যাচাই।"""
    for _ in range(attempts):
        try:
            if platform == "youtube":
                token = ensure_youtube_token()
                res = requests.get("https://www.googleapis.com/youtube/v3/videos",
                                   params={"part": "status", "id": remote_id},
                                   headers={"Authorization": f"Bearer {token}"}, timeout=45)
                items = res.json().get("items") or []
                if items and items[0].get("status", {}).get("uploadStatus") in ("uploaded", "processed"):
                    return True
            else:
                version = cfg.get("facebook_graph_version", "v26.0")
                res = requests.get(f"https://graph.facebook.com/{version}/{remote_id}",
                                   params={"fields": "status,processing_progress",
                                           "access_token": env("FB_ACCESS_TOKEN")}, timeout=45)
                if res.status_code == 200:
                    status = (res.json().get("status") or {}).get("video_status")
                    if status in ("ready", "processing"):
                        return True
        except Exception:
            pass
        time.sleep(30)
    return False


def upload_cycle(cfg: dict) -> dict:
    if cfg.get("paused"):
        core.log("সিস্টেম বিরতিতে — upload বন্ধ")
        return {"skipped": "paused"}
    result = {"uploaded": [], "failed": [], "waiting": []}
    for slot in slots(cfg):
        job = core.get_state(slot["key"])
        state = job.get("state")
        if state in ("uploaded", "done") or not job:
            continue
        if state != "ready":
            result["waiting"].append(slot["key"])
            continue
        due = job.get("upload_time") or slot["time"]
        late_minutes = 0
        if bd_hm() < str(due):
            result["waiting"].append(slot["key"])
            continue
        try:
            due_today = bd_now().replace(hour=int(str(due)[:2]), minute=int(str(due)[3:5]), second=0)
            late_minutes = max(0, int((bd_now() - due_today).total_seconds() // 60))
        except Exception:
            late_minutes = 0
        folder = WORK / slot["key"]
        folder.mkdir(parents=True, exist_ok=True)
        try:
            core.set_state(slot["key"], {"state": "uploading", "upload_started_at": bd_stamp()})
            local = folder / f"{slot['key']}.mp4"
            core.drive_download(job["drive_file_id"], str(local))
            uploaded_size = local.stat().st_size
            remote_id = (upload_youtube(job, str(local), cfg) if slot["platform"] == "youtube"
                         else upload_facebook(job, str(local), cfg))
            core.set_state(slot["key"], {"state": "uploaded", "remote_id": remote_id,
                                         "remote_url": (f"https://www.youtube.com/watch?v={remote_id}"
                                                        if slot["platform"] == "youtube"
                                                        else f"https://www.facebook.com/{remote_id}"),
                                         "confirmed": False, "late_minutes": late_minutes})
            confirmed = confirm_upload(slot["platform"], remote_id, cfg)
            core.set_state(slot["key"], {"confirmed": confirmed})
            if not confirmed:
                core.log(f"{slot['key']}: আপলোড হয়েছে তবে নিশ্চিতকরণ এখনো আসেনি", "warn")
            record = {"remote_id": remote_id, "title": job.get("title"),
                      "source_url": job.get("source_url"), "uploaded_at": bd_stamp(),
                      "platform": slot["platform"], "slot": slot["slot"],
                      "size_bytes": uploaded_size, "late_minutes": late_minutes,
                      "confirmed": confirmed, "drive_file_id": job.get("drive_file_id")}
            core.append_history(slot["platform"], record)
            if confirmed:
                core.drive_delete(job["drive_file_id"])
                core.set_state(slot["key"], {"state": "done", "drive_file_id": None,
                                             "done_at": bd_stamp()})
                core.telegram(f"SSK: {slot['platform']} slot {slot['slot']} আপলোড সম্পন্ন "
                              f"({remote_id}) — Drive খালি করা হলো")
            result["uploaded"].append(slot["key"])
            core.log(f"{slot['key']}: আপলোড সম্পন্ন {remote_id} (late {late_minutes} মিনিট)")
        except Exception as exc:
            core.set_state(slot["key"], {"state": "ready", "error": clean_error(exc),
                                         "attempt": int(job.get("attempt", 0)) + 1})
            result["failed"].append(slot["key"])
            core.log(f"{slot['key']}: আপলোড ব্যর্থ — {clean_error(exc)}", "error")
            core.telegram(f"SSK: আপলোড ব্যর্থ {slot['platform']} slot {slot['slot']}\n{clean_error(exc)}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    return result


# -------------------------------------------------------------------- manual
def drive_id_from_url(url: str) -> str | None:
    """Google Drive লিংক থেকে file id — ফোন থেকে ভিডিও পাঠানোর সবচেয়ে সহজ পথ।"""
    text = str(url or "")
    for pattern in (r"/file/d/([A-Za-z0-9_-]{10,})", r"[?&]id=([A-Za-z0-9_-]{10,})",
                    r"/d/([A-Za-z0-9_-]{10,})"):
        found = re.search(pattern, text)
        if found:
            return found.group(1)
    return None


def manual_run(cfg: dict) -> dict:
    """ড্যাশবোর্ড/ফোন থেকে দেওয়া সোর্স — চাইলে এডিট করে, চাইলে সোজা আপলোড।"""
    source_url = env("MANUAL_SOURCE")
    platform = (env("MANUAL_PLATFORM", "youtube") or "youtube").lower()
    if platform not in PLATFORMS:
        raise CoreError("platform হবে youtube অথবা facebook")
    if not source_url:
        raise CoreError("MANUAL_SOURCE খালি — সোর্স লিংক দরকার")
    edit_it = env("MANUAL_EDIT", "1").lower() in ("1", "true", "yes")
    want_time = env("MANUAL_TIME")
    if want_time and not core.hhmm_ok(want_time):
        raise CoreError(f"ভুল সময়: {want_time} (HH:MM আকারে দিন)")
    slot_no = int(env("MANUAL_SLOT", "1") or 1)
    slot_no = slot_no if slot_no in (1, 2, 3) else 1
    key = f"{platform}-{slot_no}-manual-{bd_now().strftime('%m%d%H%M')}"
    folder = WORK / "manual"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    core.set_state(key, {"state": "preparing", "platform": platform, "slot": slot_no,
                         "manual": True, "upload_time": want_time or bd_hm()})
    try:
        drive_id = drive_id_from_url(source_url)
        if source_url.startswith("repo:"):
            rel = source_url.split("repo:", 1)[1].strip()
            blob = core.github("GET", f"/repos/{core.REPO}/contents/{rel}").json()
            local = str(folder / "source.mp4")
            Path(local).write_bytes(base64.b64decode(blob["content"]))
            source = {"url": source_url, "id": None, "title": "ফোন থেকে পাঠানো ভিডিও",
                      "age_days": 0, "views": 0}
        elif drive_id:
            local = str(folder / "source.mp4")
            core.drive_download(drive_id, local)
            source = {"url": source_url, "id": drive_id, "title": "ফোন থেকে পাঠানো ভিডিও",
                      "age_days": 0, "views": 0}
        else:
            source = {"url": source_url, "id": None, "title": "ম্যানুয়াল সোর্স",
                      "age_days": 0, "views": 0}
            try:
                local = download_source(source, folder, cfg)
            except Exception as exc:
                raise CoreError(f"সোর্স নামানো যায়নি: {clean_error(exc)}")
        job = {"title": env("MANUAL_TITLE"), "description": env("MANUAL_DESCRIPTION"),
               "platform": platform, "slot": slot_no, "upload_time": want_time or bd_hm(),
               "source_url": source_url}
        if not job["title"]:
            pack = seo_package(source, {"edited_seconds": 0}, platform, cfg)
            job.update({"title": pack["title"], "description": pack["description"],
                        "tags": pack.get("tags", []), "seo_source": pack.get("generated_by")})
        if not edit_it:
            remote_id = (upload_youtube(job, local, cfg) if platform == "youtube"
                         else upload_facebook(job, local, cfg))
            core.append_history(platform, {"remote_id": remote_id, "title": job["title"],
                                           "source_url": source_url, "uploaded_at": bd_stamp(),
                                           "platform": platform, "slot": slot_no, "manual": True,
                                           "confirmed": True})
            core.set_state(key, {"state": "done", "remote_id": remote_id, "manual": True})
            core.telegram(f"SSK: ম্যানুয়াল আপলোড সম্পন্ন ({platform}, এডিট ছাড়া) — {remote_id}")
            return {"uploaded": remote_id, "edited": False}
        result = editor.edit_video(local, platform, cfg, folder)
        drive_folder = core.drive_ensure_folder(cfg.get("drive_buffer_folder", "SSK DRAMA BUFFER"))
        asset = core.drive_upload(result["file"], f"{key}.mp4", drive_folder)
        core.set_state(key, {"state": "ready", "drive_file_id": asset.get("id"),
                             "duration": result["duration"], "size_bytes": result["size_bytes"],
                             "report": result["report"], "edits_count": result["edits_count"],
                             "title": job["title"], "description": job["description"],
                             "tags": job.get("tags", []), "manual": True,
                             "platform": platform, "slot": slot_no})
        core.telegram(f"SSK: ম্যানুয়াল ভিডিও প্রস্তুত — {job['title'][:60]}\n"
                      f"{result['duration']:.0f}s, {result['size_bytes'] / 1048576:.0f} MB, "
                      f"{job['upload_time']} (BD) এ যাবে")
        return {"prepared": key, "edited": True, "ratio": result["report"].get("actual_ratio")}
    except Exception as exc:
        core.set_state(key, {"state": "failed", "error": clean_error(exc), "manual": True})
        core.telegram(f"SSK: ম্যানুয়াল কাজ ব্যর্থ\n{clean_error(exc)}")
        raise
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------- main
def main() -> int:
    mode = (env("APP_MODE", "service") or "service").lower()
    core.log(f"run শুরু — mode={mode}")
    try:
        cfg = core.load_config()
    except CoreError as exc:
        print(clean_error(exc))
        return 2
    if mode == "prepare":
        force = env("APP_FORCE", "").lower() in ("1", "true", "yes")
        print(json.dumps(prepare_cycle(cfg, force=force), ensure_ascii=False))
    elif mode == "upload":
        print(json.dumps(upload_cycle(cfg), ensure_ascii=False))
    elif mode == "manual":
        print(json.dumps(manual_run(cfg), ensure_ascii=False))
    else:
        print(json.dumps(service_cycle(cfg), ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
