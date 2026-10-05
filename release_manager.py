"""GitHub Releases ব্যবহার করে এডিটেড ভিডিও সংরক্ষণ।
প্রতিটি asset ২ GB পর্যন্ত হতে পারে। আপলোড হয়ে গেলে asset ডিলিট — স্টোরেজ খালি।"""
import os
import requests

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO  = os.getenv("GITHUB_REPOSITORY")

API = "https://api.github.com"


def _h():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def get_release(tag):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    r = requests.get(f"{API}/repos/{GITHUB_REPO}/releases/tags/{tag}",
                     headers=_h(), timeout=20)
    if r.status_code == 200:
        return r.json()
    return None


def create_release(tag, name=None, body=""):
    payload = {"tag_name": tag, "name": name or tag,
               "body": body or "SSK Drama Daily Storage"}
    r = requests.post(f"{API}/repos/{GITHUB_REPO}/releases",
                      headers=_h(), json=payload, timeout=20)
    if r.status_code in (200, 201):
        return r.json()
    return get_release(tag)


def upload_asset(release, local_path, asset_name):
    if not release or not os.path.exists(local_path):
        return False
    upload_url = release["upload_url"].split("{")[0]
    with open(local_path, "rb") as f:
        r = requests.post(
            upload_url,
            params={"name": asset_name},
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Content-Type": "application/octet-stream"},
            data=f.read(), timeout=900)
    return r.status_code in (200, 201)


def list_assets(tag):
    rel = get_release(tag)
    return rel.get("assets", []) if rel else []


def delete_asset(asset_id):
    if not asset_id:
        return False
    r = requests.delete(f"{API}/repos/{GITHUB_REPO}/releases/assets/{asset_id}",
                        headers=_h(), timeout=20)
    return r.status_code == 204


def download_asset(asset_url, local_path):
    try:
        r = requests.get(asset_url, timeout=900, stream=True)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    return True


def download_manifest_text(tag):
    for a in list_assets(tag):
        if a["name"] == "manifest.json":
            return requests.get(a["browser_download_url"], timeout=30).text
    return None


def upload_manifest_text(tag, content):
    rel = get_release(tag) or create_release(tag)
    if not rel:
        return False
    upload_url = rel["upload_url"].split("{")[0]
    r = requests.post(
        upload_url, params={"name": "manifest.json"},
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Content-Type": "application/json"},
        data=content.encode("utf-8"), timeout=60)
    return r.status_code in (200, 201)
