# -*- coding: utf-8 -*-
"""SSK DRAMA — YouTube + Facebook আপলোড এবং অ্যানালিটিক্স"""
import json, os, time
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

FB_VER = "v26.0"
FB_GRAPH = f"https://graph.facebook.com/{FB_VER}"
FB_VIDEO = f"https://graph-video.facebook.com/{FB_VER}"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]


def yt_creds():
    return Credentials(
        token=None,
        refresh_token=os.getenv("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"))


def upload_youtube(path, meta, cfg):
    yt = build("youtube", "v3", credentials=yt_creds(), cache_discovery=False)
    tags = (meta.get("tags") or [])[:35]
    body = {
        "snippet": {
            "title": (meta.get("title") or "SSK Drama")[:100],
            "description": (meta.get("description") or "")[:4900],
            "tags": tags,
            "categoryId": str(cfg.get("yt_category_id", "24")),
            "defaultLanguage": "bn",
            "defaultAudioLanguage": "hi",
        },
        "status": {
            "privacyStatus": cfg.get("yt_privacy", "public"),
            "selfDeclaredMadeForKids": False,
            "license": "youtube",
        },
    }
    media = MediaFileUpload(path, chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=media, notifySubscribers=True)
    resp, tries = None, 0
    while resp is None:
        try:
            status, resp = req.next_chunk()
            if status:
                print(f"[yt] আপলোড {int(status.progress()*100)}%")
        except (HttpError, Exception) as e:
            tries += 1
            print("[yt] retry:", e)
            if tries > 6:
                raise
            time.sleep(5 * tries)
    return {"video_id": resp.get("id"),
            "url": f"https://youtu.be/{resp.get('id')}"}


def upload_facebook(path, meta, cfg):
    page = os.getenv("FB_PAGE_ID"); tok = os.getenv("FB_ACCESS_TOKEN")
    url = f"{FB_VIDEO}/{page}/videos"
    size = os.path.getsize(path)
    s = requests.post(url, data={"upload_phase": "start", "file_size": size,
                                 "access_token": tok}, timeout=90).json()
    if "upload_session_id" not in s:
        raise RuntimeError(f"FB start failed: {s}")
    sid, vid = s["upload_session_id"], s.get("video_id")
    offset, chunk_size = 0, 4 * 1024 * 1024
    while offset < size:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(chunk_size)
        r = requests.post(url, data={"upload_phase": "transfer",
                                     "start_offset": offset,
                                     "upload_session_id": sid,
                                     "access_token": tok},
                          files={"video_file_chunk": ("chunk", chunk,
                                                      "application/octet-stream")},
                          timeout=900).json()
        if "start_offset" not in r:
            raise RuntimeError(f"FB chunk failed: {r}")
        offset = int(r["start_offset"])
        print(f"[fb] {int(offset*100/max(size,1))}%")
    fin = requests.post(url, data={"upload_phase": "finish",
                                   "upload_session_id": sid,
                                   "title": (meta.get("title") or "")[:200],
                                   "description": (meta.get("description") or "")[:5000],
                                   "published": "true" if cfg.get("fb_published", True) else "false",
                                   "access_token": tok}, timeout=180).json()
    if not fin.get("success"):
        raise RuntimeError(f"FB finish failed: {fin}")
    return {"video_id": vid, "url": f"https://www.facebook.com/{vid}"}


# ---------------- analytics ----------------
def yt_video_stats(video_id):
    key = os.getenv("YOUTUBE_API_KEY")
    if not key or not video_id:
        return None
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"part": "statistics,snippet", "id": video_id,
                                 "key": key}, timeout=60).json()
        it = (r.get("items") or [None])[0]
        if not it:
            return None
        s = it["statistics"]
        return {"title": it["snippet"]["title"],
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0))}
    except Exception as e:
        print("[yt stats]", e)
        return None


def yt_channel_analytics(days=7):
    """YouTube Analytics — views + দেশ অনুযায়ী ব্রেকডাউন (yt-analytics.readonly লাগে)।"""
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    out = {"range_days": days, "views": None, "minutes": None, "countries": [], "error": None}
    try:
        ya = build("youtubeAnalytics", "v2", credentials=yt_creds(),
                   cache_discovery=False)
        r = ya.reports().query(ids="channel==MINE",
                               startDate=start.isoformat(),
                               endDate=end.isoformat(),
                               metrics="views,estimatedMinutesWatched",
                               dimensions="country",
                               sort="-views", maxResults=8).execute()
        rows = r.get("rows") or []
        out["countries"] = [{"country": c or "?", "views": int(v)}
                            for c, v, _ in rows]
        out["views"] = sum(int(v) for c, v, _ in rows)
        out["minutes"] = sum(int(m) for _, _, m in rows)
    except Exception as e:
        out["error"] = str(e)[:200]
        print("[yt analytics]", e)
    return out


def fb_video_insights(video_id):
    tok = os.getenv("FB_ACCESS_TOKEN")
    if not tok or not video_id:
        return None
    try:
        r = requests.get(f"{FB_GRAPH}/{video_id}/video_insights",
                         params={"metric": "total_video_views,total_video_impressions",
                                 "access_token": tok}, timeout=60).json()
        vals = {}
        for d in r.get("data", []):
            name = d.get("name")
            v = (d.get("values") or [{}])[0].get("value")
            vals[name] = v
        return {"views": vals.get("total_video_views"),
                "impressions": vals.get("total_video_impressions")}
    except Exception as e:
        print("[fb insights]", e)
        return None
