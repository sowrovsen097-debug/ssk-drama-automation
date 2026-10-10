# publish.py — Facebook / YouTube আপলোড + Google Drive বাফার/ডিলিট + ভিউ-স্ট্যাটস
import os, io, json, datetime, requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

API = "https://graph.facebook.com/v21.0"; GAPI = "https://www.googleapis.com"

def fb_post_video(page_id, token, video_path, title, description, log=print):
    size = os.path.getsize(video_path)
    if size < 100 * 1024 * 1024:
        with io.open(video_path, "rb") as f:
            m = MultipartEncoder(fields={"title": title[:200], "description": description[:4800],
                                         "access_token": token, "source": ("video.mp4", f, "video/mp4")})
            r = requests.post("%s/%s/videos" % (API, page_id), data=m,
                              headers={"Content-Type": m.content_type}, timeout=1800)
        j = r.json()
        if "id" in j: return {"ok": True, "id": j["id"], "url": "https://www.facebook.com/%s" % j["id"]}
        return {"ok": False, "error": str(j)[:300]}
    start = requests.post("%s/%s/videos" % (API, page_id),
                          data={"upload_phase": "start", "file_size": size, "access_token": token}, timeout=60).json()
    up, sess = start.get("upload_session_id"), start.get("video_id")
    if not up: return {"ok": False, "error": "fb start failed: %s" % str(start)[:200]}
    off, chunk = int(start.get("start_offset", 0)), int(start.get("end_offset", 0))
    while off < size:
        with io.open(video_path, "rb") as f:
            f.seek(off)
            m = MultipartEncoder(fields={"upload_phase": "transfer", "start_offset": str(off),
                                         "upload_session_id": up, "access_token": token,
                                         "video_file_chunk": ("chunk", f.read(chunk - off), "application/octet-stream")})
            r = requests.post("%s/%s/videos" % (API, page_id), data=m,
                              headers={"Content-Type": m.content_type}, timeout=1800).json()
        if "start_offset" not in r: return {"ok": False, "error": "fb chunk failed: %s" % str(r)[:200]}
        off, chunk = int(r["start_offset"]), int(r["end_offset"]); log("fb upload %.0f%%" % (off/size*100))
    fin = requests.post("%s/%s/videos" % (API, page_id),
                        data={"upload_phase": "finish", "upload_session_id": up, "access_token": token,
                              "title": title[:200], "description": description[:4800], "published": "true"},
                        timeout=300).json()
    return {"ok": True, "id": fin.get("video_id", sess), "url": "https://www.facebook.com/%s" % fin.get("video_id", "")}

def fb_insights(video_id, token):
    try:
        j = requests.get("%s/%s/video_insights" % (API, video_id),
                         params={"metric": "total_video_views,total_video_view_time,post_video_view_time_by_country",
                                 "access_token": token}, timeout=30).json()
        out = {}
        for d in j.get("data", []): out[d.get("name")] = d.get("values", [{}])[0].get("value")
        return out
    except Exception as e: return {"error": str(e)[:120]}

def yt_token(client_id, client_secret, refresh_token):
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=60).json()
    return r.get("access_token"), r

def yt_upload(access_token, video_path, title, description, tags=None, privacy="public", category="24", log=print):
    size = os.path.getsize(video_path)
    body = {"snippet": {"title": title[:100], "description": description[:4900], "categoryId": str(category),
                        "tags": (tags or ["SSK Drama", "Bangla Drama", "Crime Alert", "Bengali Series"])[:28]},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    init = requests.post("%s/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status" % GAPI,
                         headers={"Authorization": "Bearer %s" % access_token,
                                  "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                         data=json.dumps(body).encode("utf-8"), timeout=60)
    if init.status_code not in (200, 201): return {"ok": False, "error": "yt init %s %s" % (init.status_code, init.text[:250])}
    url, chunk, off = init.headers.get("Location"), 8 * 1024 * 1024, 0
    while off < size:
        with io.open(video_path, "rb") as f:
            f.seek(off); data = f.read(chunk)
        r = requests.put(url, data=data, timeout=1800,
                         headers={"Content-Length": str(len(data)),
                                  "Content-Range": "bytes %d-%d/%d" % (off, off + len(data) - 1, size)})
        if r.status_code in (200, 201):
            j = r.json(); return {"ok": True, "id": j.get("id"), "url": "https://youtu.be/%s" % j.get("id")}
        if r.status_code == 308:
            off = int(r.headers.get("Range", "0-%d" % (off - 1)).split("-")[1]) + 1
            log("yt upload %.0f%%" % (off / size * 100)); continue
        return {"ok": False, "error": "yt chunk %s %s" % (r.status_code, r.text[:250])}
    return {"ok": False, "error": "yt upload ended without id"}

def yt_stats(access_token, video_ids, channel_id="MINE", days=7):
    end = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    ids = ",".join([v for v in (video_ids or []) if v][:30])
    base = {"ids": "channel==%s" % channel_id, "startDate": start, "endDate": end,
            "metrics": "views,estimatedMinutesWatched,averageViewDuration"}
    if ids: base["filters"] = "video==%s" % ids
    try:
        views = requests.get("%s/youtube/analytics/v2/reports" % GAPI, params=base,
                             headers={"Authorization": "Bearer %s" % access_token}, timeout=60).json()
        c = requests.get("%s/youtube/analytics/v2/reports" % GAPI,
                         params={**base, "dimensions": "country", "sort": "-views", "maxResults": 5},
                         headers={"Authorization": "Bearer %s" % access_token}, timeout=60).json()
        return {"ok": True, "totals": views, "countries": c.get("rows", [])}
    except Exception as e: return {"ok": False, "error": str(e)[:150]}

def drive_token(client_id, client_secret, refresh_token):
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=60).json()
    return r.get("access_token"), r

def drive_folder_id(access_token, name="SSK-DRAMA-BUFFER", parent=None):
    q = "mimeType='application/vnd.google-apps.folder' and name='%s' and trashed=false" % name
    if parent: q += " and '%s' in parents" % parent
    r = requests.get("%s/drive/v3/files" % GAPI, params={"q": q, "fields": "files(id)"},
                     headers={"Authorization": "Bearer %s" % access_token}, timeout=60).json()
    if r.get("files"): return r["files"][0]["id"]
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent: body["parents"] = [parent]
    return requests.post("%s/drive/v3/files" % GAPI, json=body,
                         headers={"Authorization": "Bearer %s" % access_token}, timeout=60).json().get("id")

def drive_upload(access_token, folder, path, name=None, log=print):
    size = os.path.getsize(path)
    meta = {"name": name or os.path.basename(path), "parents": [folder] if folder else []}
    init = requests.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name",
                         headers={"Authorization": "Bearer %s" % access_token,
                                  "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Length": str(size)},
                         data=json.dumps(meta).encode("utf-8"), timeout=60)
    if init.status_code not in (200, 201): return {"ok": False, "error": "drive init %s %s" % (init.status_code, init.text[:200])}
    url, chunk, off = init.headers.get("Location"), 16 * 1024 * 1024, 0
    while off < size:
        with io.open(path, "rb") as f:
            f.seek(off); data = f.read(chunk)
        r = requests.put(url, data=data, timeout=1800,
                         headers={"Content-Length": str(len(data)),
                                  "Content-Range": "bytes %d-%d/%d" % (off, off + len(data) - 1, size)})
        if r.status_code in (200, 201):
            j = r.json(); return {"ok": True, "file_id": j.get("id"), "name": j.get("name")}
        if r.status_code == 308:
            off = int(r.headers.get("Range", "0-%d" % (off - 1)).split("-")[1]) + 1; continue
        return {"ok": False, "error": "drive chunk %s %s" % (r.status_code, r.text[:200])}
    return {"ok": False, "error": "drive upload no id"}

def drive_download(access_token, file_id, dest, log=print):
    r = requests.get("%s/drive/v3/files/%s?alt=media" % (GAPI, file_id),
                     headers={"Authorization": "Bearer %s" % access_token}, stream=True, timeout=1800)
    if r.status_code != 200: return {"ok": False, "error": "drive download %s" % r.status_code}
    with io.open(dest, "wb") as f:
        for buf in r.iter_content(1024 * 256):
            if buf: f.write(buf)
    return {"ok": True, "path": dest, "size": os.path.getsize(dest)}

def drive_delete(access_token, file_id):
    r = requests.delete("%s/drive/v3/files/%s" % (GAPI, file_id),
                        headers={"Authorization": "Bearer %s" % access_token}, timeout=60)
    return {"ok": r.status_code in (200, 204), "status": r.status_code}

def drive_usage(access_token):
    try:
        q = requests.get("%s/drive/v3/about" % GAPI, params={"fields": "storageQuota"},
                         headers={"Authorization": "Bearer %s" % access_token}, timeout=30).json().get("storageQuota", {})
        lim, use = int(q.get("limit", 0)), int(q.get("usage", 0))
        return {"ok": True, "limit_gb": round(lim/1073741824, 2), "used_gb": round(use/1073741824, 2),
                "free_gb": round((lim - use)/1073741824, 2) if lim else None}
    except Exception as e: return {"ok": False, "error": str(e)[:120]}
