"""SSK DRAMA core — env, Bangladesh time, GitHub state, Google Drive, Telegram, Gemini."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

BD = timezone(timedelta(hours=6))
API = "https://api.github.com"
REPO = os.environ.get("APP_REPOSITORY", "sowrovsen097-debug/ssk-drama-automation")
BRANCH = os.environ.get("APP_BRANCH", "main")
TOKEN = os.environ.get("APP_TOKEN", "").strip()
WORK = os.environ.get("APP_WORK", "work")
TIMEOUT = 90
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UP = "https://www.googleapis.com/upload/drive/v3"
SENSITIVE = ("FB_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "GOOGLE_CLIENT_SECRET",
             "GOOGLE_REFRESH_TOKEN", "YOUTUBE_REFRESH_TOKEN", "FREESOUND_API_KEY",
             "YOUTUBE_API_KEY", "GOOGLE_CLIENT_ID", "APP_TOKEN")


class SskError(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers
def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def bd_now() -> datetime:
    return datetime.now(BD)


def bd_stamp(dt=None) -> str:
    return (dt or bd_now()).isoformat(timespec="seconds")


def bd_hm(dt=None) -> str:
    return (dt or bd_now()).strftime("%H:%M")


def clean(text, limit: int = 400) -> str:
    s = str(text)
    for name in SENSITIVE:
        value = os.environ.get(name)
        if value and len(value) > 8:
            s = s.replace(value, "***")
    s = re.sub(r"(key=|token=|Bearer\s+)[^\s\"'&]+", r"\1***", s)
    return re.sub(r"\s+", " ", s)[:limit]


def hhmm_ok(value) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value or "")))


def hours_until(hhmm: str) -> float:
    now = bd_now()
    target = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() / 3600.0


def in_window(cfg: dict, when=None) -> bool:
    start, end = (cfg.get("edit_window_bd") or ["01:00", "06:00"])[:2]
    now = (when or bd_now()).strftime("%H:%M")
    return start <= now < end if start < end else (now >= start or now < end)


def lead_check(cfg: dict, when: str | None):
    """৬ ঘণ্টার নিয়ম: সময় ঠিক করা থাকলে অন্তত min_lead_hours আগে হতে হবে।"""
    need = float(cfg.get("min_lead_hours", 6))
    if not when:
        return False, f"কোনো সময় দেওয়া হয়নি — কমপক্ষে {need:.0f} ঘণ্টা আগে সময় ঠিক করতে হবে"
    if not hhmm_ok(when):
        return False, f"সময়ের ফরম্যাট ভুল: {when} (HH:MM দরকার)"
    lead = hours_until(when)
    if lead < need:
        return False, f"{when} — মাত্র {lead:.1f} ঘণ্টা বাকি; সর্বনিম্ন {need:.0f} ঘণ্টা আগে ঠিক করতে হবে"
    return True, f"{when} — {lead:.1f} ঘণ্টা বাকি"


# ----------------------------------------------------------------- GitHub state
def _gh(method: str, path: str, body=None):
    if not TOKEN:
        raise SskError("APP_TOKEN নেই — GitHub state লেখা যাবে না")
    r = requests.request(method, API + path, json=body, timeout=TIMEOUT, headers={
        "Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ssk-drama"})
    if r.status_code >= 400:
        raise SskError(f"GitHub {r.status_code}: {clean(r.text, 200)}")
    return r.json() if r.text.strip() else {}


def read_file(path: str):
    if not TOKEN:
        raise SskError("APP_TOKEN নেই")
    r = requests.get(f"{API}/repos/{REPO}/contents/{path}", timeout=TIMEOUT, headers={
        "Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ssk-drama"})
    if r.status_code == 404:
        return None, None
    if r.status_code >= 400:
        raise SskError(f"GitHub {r.status_code}: {clean(r.text, 160)}")
    j = r.json()
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def read_json(path: str, default=None):
    fallback = {} if default is None else default
    try:
        text, sha = read_file(path)
    except Exception:
        return fallback, None
    if text is None:
        return fallback, None
    try:
        return json.loads(text), sha
    except json.JSONDecodeError:
        return fallback, sha


def write_json(path: str, payload, message: str, sha=None):
    blob = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    body = {"message": message, "branch": BRANCH,
            "content": base64.b64encode(blob.encode()).decode()}
    if sha:
        body["sha"] = sha
    else:
        _, current = read_file(path)
        if current:
            body["sha"] = current
    return _gh("PUT", f"/repos/{REPO}/contents/{path}", body)


def change(path: str, mutate, message: str, default=None, tries: int = 6):
    for attempt in range(tries):
        data, sha = read_json(path, default)
        try:
            return write_json(path, mutate(data or {}), message, sha)
        except SskError as exc:
            if "409" in str(exc) or "422" in str(exc):
                time.sleep(1 + attempt)
                continue
            raise
    raise SskError(f"{path}: {tries} বার চেষ্টার পরও লেখা যায়নি")


def log(text: str, level: str = "info"):
    try:
        change("log.json", lambda d: {"items": (d.get("items", []) + [
            {"at": bd_stamp(), "level": level, "text": clean(text, 400)}])[-400:]}, "SSK log")
    except Exception:
        pass


def load_config() -> dict:
    cfg, _ = read_json("config.json", {})
    return cfg if isinstance(cfg, dict) else {}


def jobs() -> dict:
    data, _ = read_json("queue.json", {})
    return data if isinstance(data, dict) else {}


def set_job(key: str, patch: dict):
    def mutate(d):
        d[key] = {**d.get(key, {"key": key}), **patch}
        d[key]["updated_at"] = bd_stamp()
        return d
    change("queue.json", mutate, f"SSK job {key}")


def history_sources(platform: str) -> set:
    data, _ = read_json("history.json", {"facebook": [], "youtube": []})
    return {row.get("source_id") for row in data.get(platform, [])}


def add_history(platform: str, record: dict):
    def mutate(d):
        rows = d.setdefault(platform, [])
        rows.append(record)
        d[platform] = rows[-300:]
        return d
    change("history.json", mutate, f"SSK history {platform}")


def snapshot(cfg: dict) -> dict:
    data = jobs()
    counts = {}
    for job in data.values():
        counts[job.get("state", "pending")] = counts.get(job.get("state", "pending"), 0) + 1
    prog, _ = read_json("progress.json", {})
    try:
        space = drive_space()
    except Exception as exc:
        space = {"error": clean(exc, 120)}
    return {"updated_at": bd_stamp(), "paused": bool(cfg.get("paused")),
            "counts": counts, "jobs": data, "progress": prog, "drive": space,
            "edit_window": cfg.get("edit_window_bd"), "min_lead_hours": cfg.get("min_lead_hours", 6)}


def write_status(cfg: dict):
    write_json("status.json", snapshot(cfg), "SSK status")


# ------------------------------------------------------------------ Google Drive
def drive_token() -> str:
    for need in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        if not env(need):
            raise SskError(f"{need} সেট করা নেই")
    r = requests.post("https://oauth2.googleapis.com/token", timeout=TIMEOUT, data={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("GOOGLE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
    j = r.json()
    if not j.get("access_token"):
        raise SskError("Drive টোকেন পাওয়া গেল না: " + clean(j, 200))
    return j["access_token"]


def drive_headers() -> dict:
    return {"Authorization": "Bearer " + drive_token()}


def drive_folder(name: str, headers: dict | None = None) -> str:
    headers = headers or drive_headers()
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = requests.get(DRIVE + "/files", headers=headers, timeout=TIMEOUT,
                     params={"q": q, "fields": "files(id,name)", "pageSize": 5})
    files = r.json().get("files", [])
    if files:
        return files[0]["id"]
    r = requests.post(DRIVE + "/files", headers=headers, timeout=TIMEOUT, json={
        "name": name, "mimeType": "application/vnd.google-apps.folder"})
    j = r.json()
    if not j.get("id"):
        raise SskError("Drive ফোল্ডার তৈরি হয়নি: " + clean(j, 200))
    return j["id"]


def drive_upload(local: str, name: str, folder: str, headers: dict | None = None) -> dict:
    headers = headers or drive_headers()
    size = os.path.getsize(local)
    start = requests.post(DRIVE_UP + "/files", timeout=TIMEOUT,
                          headers={**headers, "Content-Type": "application/json"},
                          params={"uploadType": "resumable", "fields": "id,name,size"},
                          json={"name": name, "parents": [folder]})
    if start.status_code >= 400:
        raise SskError("Drive আপলোড শুরু হয়নি: " + clean(start.text, 200))
    url = start.headers.get("Location")
    if not url:
        raise SskError("Drive resumable URL পাওয়া গেল না")
    chunk, sent = 8 * 1024 * 1024, 0
    with open(local, "rb") as fh:
        while sent < size:
            data = fh.read(chunk)
            r = requests.put(url, timeout=900, data=data, headers={
                **headers, "Content-Range": f"bytes {sent}-{sent + len(data) - 1}/{size}"})
            if r.status_code in (200, 201):
                return r.json()
            if r.status_code != 308:
                raise SskError(f"Drive আপলোড ব্যর্থ HTTP {r.status_code}: {clean(r.text, 200)}")
            sent += len(data)
    raise SskError("Drive আপলোড অসম্পূর্ণ")


def drive_download(file_id: str, dest: str, headers: dict | None = None) -> str:
    headers = headers or drive_headers()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with requests.get(DRIVE + f"/files/{file_id}", headers=headers, timeout=3600, stream=True,
                      params={"alt": "media"}) as r:
        if r.status_code >= 400:
            raise SskError("Drive ডাউনলোড ব্যর্থ: " + clean(r.text, 160))
        with open(dest, "wb") as fh:
            for part in r.iter_content(1024 * 1024):
                fh.write(part)
    return dest


def drive_delete(file_id: str, headers: dict | None = None):
    try:
        requests.delete(DRIVE + f"/files/{file_id}", headers=headers or drive_headers(), timeout=120)
    except Exception as exc:
        log("Drive ডিলিট ব্যর্থ: " + clean(exc, 120), "warn")


def drive_space(headers: dict | None = None) -> dict:
    r = requests.get(DRIVE + "/about", headers=headers or drive_headers(), timeout=TIMEOUT,
                     params={"fields": "storageQuota,user"})
    j = r.json()
    q = j.get("storageQuota", {})
    limit, usage = int(q.get("limit") or 0), int(q.get("usage") or 0)
    return {"limit": limit, "usage": usage, "free": max(0, limit - usage),
            "user": (j.get("user") or {}).get("emailAddress", "")}


# --------------------------------------------------------------------- Telegram
def telegram(text: str, keyboard=None, chat_id=None):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        return {}
    payload = {"chat_id": chat_id or env("TELEGRAM_CHAT_ID"), "text": str(text)[:4000]}
    if keyboard:
        payload["reply_markup"] = keyboard
    if "<b>" in str(text) or "<i>" in str(text) or "<code>" in str(text):
        payload["parse_mode"] = "HTML"
    try:
        return requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json=payload, timeout=60).json()
    except Exception as exc:
        log("Telegram পাঠানো যায়নি: " + clean(exc), "warn")
        return {}


def telegram_updates(offset=None, wait=25):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=wait + 20,
                         params={"offset": offset, "timeout": wait,
                                 "allowed_updates": '["message","callback_query"]'})
        return r.json().get("result", []) or []
    except Exception:
        return []


def public_url() -> str:
    return "https://raw.githubusercontent.com/" + REPO + "/" + BRANCH + "/"


# ----------------------------------------------------------------------- Gemini
def gemini(prompt: str, model=None, timeout=180) -> str:
    key = env("GEMINI_API_KEY")
    if not key:
        raise SskError("GEMINI_API_KEY নেই")
    for name in ([model] if model else
                 [env("GEMINI_MODEL", "gemini-3.1-flash-lite"), "gemini-2.5-flash", "gemini-2.0-flash"]):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent",
                timeout=timeout, params={"key": key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.5, "maxOutputTokens": 1200}})
            parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except Exception:
            continue
    raise SskError("Gemini উত্তর দেয়নি (কী বা মডেল পরীক্ষা করুন)")


ASSISTANT_RULES = (
    "তুমি SSK DRAMA অটোমেশনের সহকারী। বাংলায় উত্তর দাও, সর্বোচ্চ ৮ লাইন। "
    "কখনো কোড লিখবে না, কোনো টোকেন/কী দেখাবে না, বানানো তথ্য দেবে না — "
    "শুধু দেওয়া স্টেট থেকে যা বোঝা যায় তা বলবে।"
)


def ask(question: str, snap: dict) -> str:
    context = json.dumps({
        "paused": snap.get("paused"), "counts": snap.get("counts"),
        "edit_window": snap.get("edit_window"), "min_lead_hours": snap.get("min_lead_hours"),
        "drive": snap.get("drive"), "progress": snap.get("progress"),
        "jobs": [{"key": k, "platform": v.get("platform"), "state": v.get("state"),
                  "error": str(v.get("error") or "")[:160]}
                 for k, v in list(snap.get("jobs", {}).items())[:8]],
    }, ensure_ascii=False)
    return gemini(ASSISTANT_RULES + "\nসিস্টেম স্টেট: " + context + "\nপ্রশ্ন: " + question)
