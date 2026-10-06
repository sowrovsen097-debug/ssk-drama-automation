# -*- coding: utf-8 -*-
"""SSK DRAMA — GitHub Release কে স্টোরেজ হিসেবে ব্যবহার + status + অটো কমিট"""
import base64, json, os, subprocess, time
from datetime import datetime, timezone
import requests

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
RELEASE_TAG = "ssk-storage"
RELEASE_NAME = "SSK Video Storage (auto)"


def token():
    return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""


def owner_repo():
    r = (os.getenv("GITHUB_REPOSITORY") or "").split("/")
    if len(r) == 2 and r[0]:
        return r[0], r[1]
    return "", ""


def _headers(is_upload=False):
    h = {
        "Authorization": f"Bearer {token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    if is_upload:
        h["Content-Type"] = "application/octet-stream"
    return h


def api(method, url, retries=4, timeout=180, is_upload=False, **kw):
    last = None
    for i in range(retries):
        try:
            r = requests.request(method, url, headers=_headers(is_upload),
                                 timeout=timeout, **kw)
            if r.status_code in (200, 201, 204):
                return r
            if r.status_code in (409, 422, 429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(2 + 3 * i); continue
            return r
        except Exception as e:
            last = e
            time.sleep(2 + 3 * i)
    raise RuntimeError(f"GitHub API error: {last}")


# ---------------- local json ----------------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def keyword():
    return "ssk"


# ---------------- status updater ----------------
def update_status(stage=None, message=None, pending=None, last_edit=None, last_upload=None, analytics=None):
    status_file = "status.json"
    data = load_json(status_file, {
        "updated_at": "",
        "stage": "idle",
        "message": "",
        "pending": {"fb": 0, "yt": 0},
        "pending_list": [],
        "last_edit": None,
        "last_upload": None,
        "analytics": {}
    })
    
    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    if stage is not None:
        data["stage"] = stage
    if message is not None:
        data["message"] = message
    if pending is not None:
        data["pending"] = pending
    if last_edit is not None:
        data["last_edit"] = last_edit
    if last_upload is not None:
        data["last_upload"] = last_upload
    if analytics is not None:
        data["analytics"] = analytics

    save_json(status_file, data)
    return data


# ---------------- release storage ----------------
def ensure_release():
    o, r = owner_repo()
    if not o:
        raise RuntimeError("GITHUB_REPOSITORY পাওয়া যায়নি")
    res = api("GET", f"{API}/repos/{o}/{r}/releases/tags/{RELEASE_TAG}")
    if res.status_code == 200:
        return res.json()
    res = api("POST", f"{API}/repos/{o}/{r}/releases",
              json={"tag_name": RELEASE_TAG, "name": RELEASE_NAME,
                    "body": "SSK স্বয়ংক্রিয় ভিডিও স্টোরেজ। আপলোড শেষে নিজে নিজেই ডিলিট হয়।",
                    "prerelease": True})
    if res.status_code in (200, 201):
        return res.json()
    raise RuntimeError(f"release create failed {res.status_code}: {res.text[:200]}")


def upload_asset(local_path, asset_name):
    o, r = owner_repo()
    rel = ensure_release()
    rid = rel["id"]
    # একই নাম থাকলে আগে মুছে ফেলি
    for a in rel.get("assets", []):
        if a["name"] == asset_name:
            api("DELETE", f"{API}/repos/{o}/{r}/releases/assets/{a['id']}")
    size = os.path.getsize(local_path)
    if size > 2 * 1024 ** 3:
        raise RuntimeError("২GB-এর বেশি ফাইল Release-এ রাখা যায় না")
    with open(local_path, "rb") as fp:
        res = api("POST",
                  f"{UPLOADS}/repos/{o}/{r}/releases/{rid}/assets?name={asset_name}",
                  data=fp, is_upload=True,
                  timeout=1800)
    return res.json() if res.status_code in (200, 201) else None
