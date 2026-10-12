"""SSK DRAMA publish — SEO প্যাকেজ, YouTube resumable আপলোড, Facebook chunked আপলোড."""
from __future__ import annotations

import json
import os

import requests

import core
from core import SskError, clean, env


# ------------------------------------------------------------------ SEO
def seo_package(cfg: dict, source: dict, platform: str, slot: int) -> dict:
    base = (source.get("title") or "SSK DRAMA").strip()
    fallback = {
        "title": f"{base} | SSK DRAMA",
        "description": (f"{base}\n\nSSK DRAMA-র নতুন এপিসোড। গল্পটি সম্পূর্ণ দেখা শেষ না হওয়া পর্যন্ত "
                        f"সাথে থাকুন।\n\n#SSKDRAMA #NewEpisode #ShortFilm"),
        "tags": ["ssk drama", "new episode", "short film", "crime story", "story"],
    }
    try:
        prompt = (
            "তুমি ইউটিউব/ফেসবুক SEO লেখক। নিচের সোর্স ভিডিওর জন্য SEO প্যাকেজ বানাও। "
            "শুধু JSON দাও: {\"title\":..,\"description\":..,\"tags\":[..]} — "
            "title সর্বোচ্চ ১০০ অক্ষর, description ৩-৫ লাইন শেষে ৫টি হ্যাশট্যাগ, tags ১০টি। "
            f"প্ল্যাটফর্ম: {platform}। সোর্স শিরোনাম: {base}")
        raw = core.gemini(prompt)
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
        data["title"] = str(data.get("title") or fallback["title"])[:100]
        data["description"] = str(data.get("description") or fallback["description"])
        tags = data.get("tags") or fallback["tags"]
        data["tags"] = [str(t)[:60] for t in tags][:15]
        return data
    except Exception as exc:
        core.log("SEO জেনারেশন ব্যর্থ, ফোলব্যাক ব্যবহার: " + clean(exc), "warn")
        return fallback


# ----------------------------------------------------------------- YouTube
def youtube_token() -> str:
    for need in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        if not env(need):
            raise SskError(f"{need} সেট করা নেই")
    r = requests.post("https://oauth2.googleapis.com/token", timeout=core.TIMEOUT, data={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("YOUTUBE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
    j = r.json()
    if not j.get("access_token"):
        raise SskError("YouTube টোকেন পাওয়া গেল না: " + clean(j, 200))
    if "youtube.upload" not in str(j.get("scope", "")) and "youtube" not in str(j.get("scope", "")):
        raise SskError("টোকেনে youtube.upload স্কোপ নেই")
    return j["access_token"]


def upload_youtube(path: str, seo: dict, cfg: dict) -> str:
    token = youtube_token()
    size = os.path.getsize(path)
    meta = {"snippet": {"title": seo["title"], "description": seo["description"],
                        "tags": seo.get("tags", []), "categoryId": "24"},
            "status": {"privacyStatus": cfg.get("youtube_privacy", "public"),
                       "selfDeclaredMadeForKids": False}}
    start = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"}, timeout=core.TIMEOUT,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
        json=meta)
    if start.status_code >= 400:
        raise SskError(f"YouTube আপলোড শুরু হয়নি HTTP {start.status_code}: {clean(start.text, 250)}")
    url = start.headers.get("Location")
    if not url:
        raise SskError("YouTube resumable URL পাওয়া গেল না")
    chunk, sent = 8 * 1024 * 1024, 0
    with open(path, "rb") as fh:
        while sent < size:
            data = fh.read(chunk)
            r = requests.put(url, timeout=1800, data=data, headers={
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {sent}-{sent + len(data) - 1}/{size}"})
            if r.status_code in (200, 201):
                vid = (r.json() or {}).get("id")
                if not vid:
                    raise SskError("YouTube রেসপন্সে video id নেই")
                return "https://www.youtube.com/watch?v=" + vid
            if r.status_code != 308:
                raise SskError(f"YouTube আপলোড ব্যর্থ HTTP {r.status_code}: {clean(r.text, 250)}")
            sent += len(data)
    raise SskError("YouTube আপলোড অসম্পূর্ণ")


# ----------------------------------------------------------------- Facebook
def upload_facebook(path: str, seo: dict, cfg: dict) -> str:
    page = str(cfg.get("facebook_page_id") or "me")
    version = cfg.get("facebook_graph_version", "v26.0")
    base = f"https://graph.facebook.com/{version}/{page}/videos"
    size = os.path.getsize(path)
    token = env("FB_ACCESS_TOKEN")
    if not token:
        raise SskError("FB_ACCESS_TOKEN নেই")
    start = requests.post(base, timeout=core.TIMEOUT, data={
        "upload_phase": "start", "file_size": size, "access_token": token})
    j = start.json()
    if "upload_session_id" not in j:
        raise SskError("Facebook আপলোড শুরু হয়নি: " + clean(j, 250))
    session, video_id = j["upload_session_id"], j.get("video_id")
    offset = int(j.get("start_offset", 0))
    with open(path, "rb") as fh:
        while True:
            fh.seek(offset)
            data = fh.read(4 * 1024 * 1024)
            if not data:
                break
            r = requests.post(base, timeout=1800, data={
                "upload_phase": "transfer", "upload_session_id": session,
                "start_offset": offset, "access_token": token},
                files={"video_file_chunk": ("chunk", data, "application/octet-stream")})
            j = r.json()
            if "start_offset" not in j:
                raise SskError("Facebook চাঙ্ক আপলোড ব্যর্থ: " + clean(j, 250))
            offset = int(j["start_offset"])
            if j.get("end_offset") in (None, "", False):
                break
    finish = requests.post(base, timeout=600, data={
        "upload_phase": "finish", "upload_session_id": session, "access_token": token,
        "title": seo["title"], "description": seo["description"]}).json()
    if not finish.get("success"):
        raise SskError("Facebook আপলোড শেষ হয়নি: " + clean(finish, 250))
    return f"https://www.facebook.com/{page}/videos/{video_id}" if video_id else \
           f"https://www.facebook.com/{page}/videos"


def facebook_page_info(cfg: dict) -> dict:
    version = cfg.get("facebook_graph_version", "v26.0")
    page = str(cfg.get("facebook_page_id") or "me")
    r = requests.get(f"https://graph.facebook.com/{version}/{page}", timeout=core.TIMEOUT,
                     params={"fields": "id,name,fan_count,category,link",
                             "access_token": env("FB_ACCESS_TOKEN")})
    return r.json()


def confirm_processing(platform: str, remote_id: str, cfg: dict, tries: int = 8) -> bool:
    """আপলোডের পর প্ল্যাটফর্ম processing শেষ করেছে কি না দেখে, তারপর স্টোরেজ মোছে।"""
    import time
    if platform == "youtube":
        for _ in range(tries):
            try:
                r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=core.TIMEOUT,
                                 params={"part": "status", "id": remote_id,
                                         "key": env("YOUTUBE_API_KEY")})
                item = (r.json().get("items") or [{}])[0]
                if item.get("status", {}).get("uploadStatus") in ("processed", "uploaded"):
                    return True
            except Exception:
                pass
            time.sleep(20)
        return False
    for _ in range(tries):
        try:
            r = requests.get(f"https://graph.facebook.com/{cfg.get('facebook_graph_version', 'v26.0')}/{remote_id}",
                             timeout=core.TIMEOUT,
                             params={"fields": "status", "access_token": env("FB_ACCESS_TOKEN")})
            status = (r.json().get("status") or {}).get("video_status")
            if status in ("ready", "processed"):
                return True
        except Exception:
            pass
        time.sleep(20)
    return False
