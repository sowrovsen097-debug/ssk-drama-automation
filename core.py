"""SSK DRAMA — shared core.

GitHub state (queue/status/history/log), Google Drive buffer, Telegram, Gemini AI.
Every credential comes from the 10 GitHub Secrets the user already created.
"""
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
TOKEN = (os.environ.get("APP_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
TIMEOUT = 60
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UP = "https://www.googleapis.com/upload/drive/v3"


class CoreError(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers
def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def bd_now() -> datetime:
    return datetime.now(BD)


def bd_hm(dt: datetime | None = None) -> str:
    return (dt or bd_now()).strftime("%H:%M")


def bd_stamp(dt: datetime | None = None) -> str:
    return (dt or bd_now()).strftime("%Y-%m-%d %H:%M:%S")


def clean_error(exc) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for name in ("FB_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY",
                 "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "YOUTUBE_REFRESH_TOKEN",
                 "FREESOUND_API_KEY", "YOUTUBE_API_KEY", "GOOGLE_CLIENT_ID"):
        value = env(name)
        if value and len(value) > 8:
            text = text.replace(value, "[secret %s]" % name)
    return text[:600]


def hhmm_ok(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value or "").strip()))


def in_edit_window(dt: datetime | None = None) -> bool:
    """Night editing is allowed 01:00–06:00 Bangladesh time."""
    hour = (dt or bd_now()).hour
    return 1 <= hour < 6


# ------------------------------------------------------------------ GitHub state
def github(method: str, path: str, body=None, url: str | None = None, timeout: int = TIMEOUT):
    target = url or (path if path.startswith("http") else API + path)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ssk-drama"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    res = requests.request(method, target, headers=headers, json=body, timeout=timeout)
    if res.status_code >= 400:
        raise CoreError(f"GitHub {res.status_code}: {res.text[:200]}")
    return res


def read_file(path: str):
    try:
        data = github("GET", f"/repos/{REPO}/contents/{path}").json()
    except CoreError as exc:
        if "404" in str(exc):
            return None, None
        raise
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def read_json(path: str, default=None):
    text, sha = read_file(path)
    blank = default if default is not None else {}
    if text is None:
        return blank, None
    try:
        return json.loads(text), sha
    except json.JSONDecodeError:
        return blank, sha


def write_json(path: str, payload, message: str, sha: str | None = None):
    blob = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    body = {"message": message, "branch": BRANCH,
            "content": base64.b64encode(blob.encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    github("PUT", f"/repos/{REPO}/contents/{path}", body=body)


def change(path: str, mutate, message: str, default=None, attempts: int = 6):
    """Read → mutate → write, retrying on GitHub conflict (409/422)."""
    last = None
    for _ in range(attempts):
        data, sha = read_json(path, default=default)
        out = mutate(data)
        if out is None:
            out = data
        try:
            write_json(path, out, message, sha)
            return out
        except CoreError as exc:
            last = exc
            if "409" not in str(exc) and "422" not in str(exc):
                raise
            time.sleep(1.5)
    raise CoreError(f"state write failed: {clean_error(last)}")


def log(text: str, level: str = "info"):
    line = {"at": bd_stamp(), "level": level, "text": str(text)[:400]}
    try:
        change("log.json", lambda data: {
            "items": ((data or {}).get("items", []) + [line])[-400:],
            "updated_at": bd_stamp(),
        }, f"log: {line['text'][:40]}", default={"items": []})
    except Exception:
        pass
    print(f"[{line['at']}] {level.upper()} {line['text']}", flush=True)
    return line


# ---------------------------------------------------------------- Google Drive
def drive_token() -> str:
    cid, csec, rt = env("GOOGLE_CLIENT_ID"), env("GOOGLE_CLIENT_SECRET"), env("GOOGLE_REFRESH_TOKEN")
    if not (cid and csec and rt):
        raise CoreError("Drive সেটআপ অসম্পূর্ণ: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / "
                        "GOOGLE_REFRESH_TOKEN — একটি নতুন refresh token দরকার (service account নয়)")
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": csec, "refresh_token": rt,
        "grant_type": "refresh_token"}, timeout=TIMEOUT)
    if res.status_code != 200:
        raise CoreError(f"Google token {res.status_code}: {res.text[:200]}")
    return res.json()["access_token"]


def drive_ensure_folder(name: str) -> str:
    tok = drive_token()
    head = {"Authorization": f"Bearer {tok}"}
    query = "mimeType='application/vnd.google-apps.folder' and name='%s' and trashed=false" % name.replace("'", "\\'")
    found = requests.get(f"{DRIVE}/files", params={
        "q": query, "fields": "files(id,name)", "supportsAllDrives": "true"},
        headers=head, timeout=TIMEOUT).json()
    if found.get("files"):
        return found["files"][0]["id"]
    made = requests.post(f"{DRIVE}/files", json={
        "name": name, "mimeType": "application/vnd.google-apps.folder"}, headers=head, timeout=TIMEOUT)
    if made.status_code >= 400:
        raise CoreError(f"Drive folder {made.status_code}: {made.text[:200]}")
    return made.json()["id"]


def drive_upload(local_path: str, name: str, folder_id: str, chunk_mb: int = 32) -> dict:
    """Resumable, chunked upload — works for 10 MB … multiple GB files."""
    size = os.path.getsize(local_path)
    tok = drive_token()
    start = requests.post(f"{DRIVE_UP}/files", params={
        "uploadType": "resumable", "supportsAllDrives": "true",
        "fields": "id,name,size,webViewLink"}, json={"name": name, "parents": [folder_id]},
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, timeout=TIMEOUT)
    if start.status_code >= 400:
        raise CoreError(f"Drive upload start {start.status_code}: {start.text[:300]}")
    session = start.headers["Location"]
    step = chunk_mb * 1024 * 1024
    sent = 0
    with open(local_path, "rb") as fh:
        while sent < size:
            fh.seek(sent)
            blob = fh.read(step)
            end = sent + len(blob) - 1
            res = requests.put(session, data=blob, headers={
                "Authorization": f"Bearer {tok}",
                "Content-Range": f"bytes {sent}-{end}/{size}",
                "Content-Length": str(len(blob))}, timeout=900)
            if res.status_code in (200, 201):
                out = res.json()
                out["size_bytes"] = size
                return out
            if res.status_code == 308:
                rng = res.headers.get("Range")
                sent = int(rng.split("-")[1]) + 1 if rng else end + 1
                continue
            raise CoreError(f"Drive chunk {res.status_code}: {res.text[:200]}")
    raise CoreError("Drive upload finished without a response")


def drive_download(file_id: str, dest: str) -> str:
    tok = drive_token()
    with requests.get(f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                      headers={"Authorization": f"Bearer {tok}"}, stream=True, timeout=1800) as res:
        if res.status_code >= 400:
            raise CoreError(f"Drive download {res.status_code}: {res.text[:200]}")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in res.iter_content(4 * 1024 * 1024):
                if chunk:
                    fh.write(chunk)
    return dest


def drive_delete(file_id: str):
    try:
        tok = drive_token()
        res = requests.delete(f"{DRIVE}/files/{file_id}", params={"supportsAllDrives": "true"},
                              headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
        if res.status_code >= 400 and res.status_code != 404:
            raise CoreError(f"Drive delete {res.status_code}: {res.text[:160]}")
        return True
    except Exception as exc:
        log(f"Drive delete ব্যর্থ: {clean_error(exc)}", "warn")
        return False


def drive_space() -> dict:
    tok = drive_token()
    res = requests.get(f"{DRIVE}/about", params={"fields": "storageQuota"},
                       headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
    return res.json().get("storageQuota", {}) if res.status_code == 200 else {}


# --------------------------------------------------------------------- Telegram
def telegram(text: str, keyboard=None, chat_id: str | None = None):
    token, cid = env("TELEGRAM_BOT_TOKEN"), chat_id or env("TELEGRAM_CHAT_ID")
    if not token or not cid:
        return None
    payload = {"chat_id": cid, "text": str(text)[:3900], "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=40)
        return res.json() if res.ok else None
    except Exception as exc:
        log(f"Telegram পাঠানো যায়নি: {clean_error(exc)}", "warn")
        return None


def telegram_get_updates(offset: int | None = None, timeout: int = 25):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    params = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset:
        params["offset"] = offset
    res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=timeout + 20)
    if res.status_code != 200:
        return []
    return res.json().get("result", [])


def telegram_answer_callback(callback_id: str, text: str = ""):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token or not callback_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text[:200]}, timeout=20)
    except Exception:
        pass


# ----------------------------------------------------------------------- Gemini
GEMINI_FALLBACKS = ["gemini-3.1-flash-lite", "gemini-3-flash", "gemini-2.5-flash"]


def gemini(prompt: str, images=None, model: str | None = None, timeout: int = 180,
           temperature: float = 0.4, max_tokens: int = 900) -> str:
    key = env("GEMINI_API_KEY")
    if not key:
        raise CoreError("GEMINI_API_KEY সেট করা নেই")
    parts = [{"text": prompt}]
    for blob in (images or []):
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": blob}})
    order = [model] if model else []
    order += [m for m in GEMINI_FALLBACKS if m not in order]
    last = "no attempt"
    for name in order:
        if not name:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent"
        res = requests.post(url, params={"key": key}, json={
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }, timeout=timeout)
        if res.status_code == 200:
            try:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                last = "empty candidate"
                continue
        last = f"{res.status_code} {res.text[:160]}"
        if res.status_code in (400, 404):
            continue
    raise CoreError(f"Gemini ব্যর্থ: {last}")


def json_from(text: str, default=None):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return default
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return default


ASSISTANT_RULES = """তুমি "SSK DRAMA" ভিডিও অটোমেশন সিস্টেমের সহকারী।
নিয়ম:
- বাংলায় সংক্ষেপে (সর্বোচ্চ ৮ লাইন) উত্তর দাও, শুধু দেওয়া তথ্য ব্যবহার করো।
- কোনো কোড লেখো না, কোনো GitHub Secret/টোকেন/API key চাও না বা দেখাও না।
- ভিডিও কত মিনিট, কোন প্ল্যাটফর্মে কখন যাবে, কিছু fail করেছে কি না — এই প্রশ্নে দেওয়া context দেখে সোজা উত্তর দাও।
- তথ্য না থাকলে সৎভাবে বলো "এই তথ্য এখন স্টেটাসে নেই"।
- কপিরাইট/অনুমতি নিয়ে কোনো মন্তব্য করো না — সব চ্যানেল অনুমোদিত।"""


def assistant_reply(question: str, snapshot: dict, model: str | None = None) -> str:
    context = json.dumps(snapshot, ensure_ascii=False)[:18000]
    prompt = (ASSISTANT_RULES + "\n\n=== সিস্টেম স্টেটাস (JSON) ===\n" + context +
              "\n\n=== ব্যবহারকারীর প্রশ্ন ===\n" + str(question)[:1500] + "\n\nউত্তর:")
    return gemini(prompt, model=model, temperature=0.3)


# ------------------------------------------------------------------ config glue
def load_config():
    data, _ = read_json("config.json", default={})
    if not data:
        raise CoreError("config.json পড়া যায়নি")
    return data


def validate_config(cfg) -> list[str]:
    problems = []
    for platform in ("facebook", "youtube"):
        channels = cfg.get(f"{platform}_channels") or []
        times = cfg.get(f"{platform}_times_bd") or []
        if len(channels) != 3:
            problems.append(f"{platform}: ৩টি চ্যানেল দরকার (পাওয়া {len(channels)})")
        if len(times) != 3:
            problems.append(f"{platform}: ৩টি আপলোড সময় দরকার (পাওয়া {len(times)})")
        if len(set(times)) != len(times):
            problems.append(f"{platform}: সময়গুলো আলাদা হতে হবে")
        for value in times:
            if not hhmm_ok(value):
                problems.append(f"{platform}: ভুল সময় {value!r}")
            elif 1 <= int(str(value)[:2]) < 6:
                problems.append(f"{platform}: {value} — ০১:০০–০৬:০০ শুধু এডিটিং সময়")
        for url in channels:
            if not str(url).strip().startswith("https://"):
                problems.append(f"{platform}: ভুল লিংক {url!r}")
    ratio = float(cfg.get("keep_ratio", 0.8125))
    if not 0.75 <= ratio <= 0.875:
        problems.append("keep_ratio ০.৭৫–০.৮৭৫ এর মধ্যে হতে হবে")
    return problems


def set_state(key: str, patch: dict):
    def mutate(data):
        data = data or {}
        job = data.get(key, {})
        job.update(patch)
        job["key"] = key
        job["updated_at"] = bd_stamp()
        job.setdefault("created_at", bd_stamp())
        data[key] = job
        return data
    return change("queue.json", mutate, f"state: {key}", default={})


def get_state(key: str) -> dict:
    data, _ = read_json("queue.json", default={})
    return (data or {}).get(key, {})


def jobs() -> dict:
    data, _ = read_json("queue.json", default={})
    return data or {}


def append_history(platform: str, record: dict):
    def mutate(data):
        data = data or {"facebook": [], "youtube": []}
        data.setdefault(platform, []).append(record)
        data[platform] = data[platform][-300:]
        return data
    return change("history.json", mutate, f"history: {platform} upload", default={"facebook": [], "youtube": []})


def history_ids(platform: str) -> set:
    data, _ = read_json("history.json", default={"facebook": [], "youtube": []})
    out = set()
    for item in (data or {}).get(platform, []):
        if isinstance(item, dict) and item.get("source_url"):
            out.add(item["source_url"])
        elif isinstance(item, str):
            out.add(item)
    return out


def write_status(snapshot: dict):
    snapshot["updated_at"] = bd_stamp()
    try:
        write_json("status.json", snapshot, "status: snapshot")
    except CoreError:
        change("status.json", lambda _old: snapshot, "status: snapshot", default={})
    return snapshot
