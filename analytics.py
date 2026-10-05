"""গতকাল আপলোড হওয়া ভিডিওগুলোর YouTube (views/country) + Facebook (views) মেট্রিক্কে stats.json।"""
import os
import json
import datetime
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from release_manager import download_manifest_text

YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
GOOGLE_CLIENT_ID      = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET  = os.getenv("GOOGLE_CLIENT_SECRET")
FB_ACCESS_TOKEN       = os.getenv("FB_ACCESS_TOKEN")


def yt_analytics():
    creds = Credentials(
        token=None, refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/yt-analytics.readonly"],
    )
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def get_yt_stats(video_id):
    try:
        ya = yt_analytics()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        today = datetime.date.today().isoformat()
        daily = ya.reports().query(
            ids="channel==MINE", startDate=yesterday, endDate=today,
            metrics="views,likes,comments,estimatedMinutesWatched",
            filters=f"video=={video_id}").execute()
        country = ya.reports().query(
            ids="channel==MINE", startDate=yesterday, endDate=today,
            dimensions="country", metrics="views",
            filters=f"video=={video_id}", sort="-views", maxResults=10).execute()
        return {"daily": daily.get("rows", []),
                "countries": country.get("rows", [])}
    except Exception as e:
        return {"error": str(e)}


def get_fb_stats(video_id):
    try:
        url = (f"https://graph.facebook.com/v25.0/{video_id}/video_insights"
               f"?metric=total_video_views,total_video_30s_views"
               f"&access_token={FB_ACCESS_TOKEN}")
        return requests.get(url, timeout=20).json()
    except Exception as e:
        return {"error": str(e)}


def main():
    yesterday_tag = f"daily-{(datetime.date.today() - datetime.timedelta(days=1)).isoformat()}"
    text = download_manifest_text(yesterday_tag)
    if not text:
        print("Yesterday manifest not found"); return
    manifest = json.loads(text)

    out = {"date": datetime.date.today().isoformat(),
           "generated_at": datetime.datetime.utcnow().isoformat(), "videos": {}}
    for vid, info in manifest.items():
        out["videos"][vid] = {"title": info.get("title", "")}
        yt_id = info.get("yt_video_id")
        fb_id = info.get("fb_video_id")
        if yt_id:
            out["videos"][vid]["youtube"] = get_yt_stats(yt_id)
        if fb_id:
            out["videos"][vid]["facebook"] = get_fb_stats(fb_id)

    with open("stats.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("stats.json updated.")


if __name__ == "__main__":
    main()
