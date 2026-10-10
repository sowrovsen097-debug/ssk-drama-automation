# config.py — সেটিংস, হিস্টোরি, কিউ এবং GitHub API হেল্পার
import os, io, json, base64, datetime, requests

API = "https://api.github.com"
REPO = os.getenv("GITHUB_REPOSITORY", "")
GH_TOKEN = os.getenv("GITHUB_TOKEN", "") or os.getenv("GH_PAT", "")
BRANCH = os.getenv("GITHUB_REF_NAME") or "main"
BST = datetime.timezone(datetime.timedelta(hours=6), "BST")
UTC = datetime.timezone.utc

CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"
QUEUE_FILE = "queue.json"
STATUS_FILE = "status.json"
CHAT_FILE = "ai_chat.json"

DEFAULT_CONFIG = {
    "version": 3,
    "facebook_channels": ["", "", ""],
    "youtube_channels": ["", "", ""],
    "fb_upload_times_bst": ["10:00", "15:00", "20:00"],
    "yt_upload_times_bst": ["08:00", "15:00", "22:00"],
    "edit_window_bst": ["01:00", "06:00"],
    "min_edit_lead_hours": 6,
    "min_age_days": 60,
    "max_duration_min": 80,
    "keep_ratio": 0.8125,
    "yt_width": 1920,
    "yt_height": 1080,
    "fb_width": 1080,
    "fb_height": 1080,
    "mirror_facebook": True,
    "subtitles": True,
    "subtitle_colorful": True,
    "bg_music": True,
    "music_volume": 0.12,
    "whoosh": True,
    "whisper_model": "base",
    "whisper_language": "auto",
    "quality_crf": 23,
    "quality_preset": "veryfast",
    "max_output_mb": 1700,
    "privacy_status": "public",
    "category_id": "24",
    "brand_name": "SSK DRAMA",
    "progress_bar": True,
    "dry_run": False
}

def load_json(path, default):
    if os.path.exists(path):
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, data):
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    disk = load_json(CONFIG_FILE, {})
    if isinstance(disk, dict):
        cfg.update(disk)
    return cfg

def load_history():
    h = load_json(HISTORY_FILE, {})
    if not isinstance(h, dict): h = {}
    h.setdefault("done", {})
    h.setdefault("uploaded", [])
    return h

def save_history(h):
    return save_json(HISTORY_FILE, h)

def mark_done(video_id, info):
    h = load_history()
    h["done"][str(video_id)] = info
    save_history(h)
    return h

def mark_uploaded(record):
    h = load_history()
    h["uploaded"].append(record)
    h["uploaded"] = h["uploaded"][-400:]
    save_history(h)
    return h

def load_queue():
    q = load_json(QUEUE_FILE, [])
    return q if isinstance(q, list) else []

def save_queue(q):
    return save_json(QUEUE_FILE, q)

def load_status():
    s = load_json(STATUS_FILE, {})
    if not isinstance(s, dict): s = {}
    for k in ("log", "queue", "stats", "bot", "run"):
        s.setdefault(k, [] if k in ("log", "queue") else {})
    s.setdefault("counters", {})
    return s

def now_utc():
    return datetime.datetime.now(tz=UTC)

def status_patch(patch):
    s = load_status()
    s.update(patch or {})
    s["updated_at"] = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    s["updated_at_bst"] = now_utc().astimezone(BST).strftime("%Y-%m-%d %H:%M:%S BST")
    gh_put_json(STATUS_FILE, s, "status: update")
    return s

def status_log(msg, level="info"):
    s = load_status()
    s.setdefault("log", [])
    s["log"].append({"t": now_utc().astimezone(BST).strftime("%d %b %H:%M:%S"), "msg": str(msg)[:300], "level": level})
    s["log"] = s["log"][-60:]
    s["updated_at_bst"] = now_utc().astimezone(BST).strftime("%Y-%m-%d %H:%M:%S BST")
    gh_put_json(STATUS_FILE, s, "status: log")
    return s

def _gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

def gh_put_json(path, data, message="update"):
    return gh_put_file(path, json.dumps(data, ensure_ascii=False, indent=2), message)

def gh_put_file(path, text, message="update"):
    if not (REPO and GH_TOKEN):
        io.open(path, "w", encoding="utf-8").write(text)
        return True
    url = f"{API}/repos/{REPO}/contents/{path}"
    for attempt in range(4):
        sha = None
        try:
            g = requests.get(url, headers=_gh_headers(), params={"ref": BRANCH}, timeout=30)
            if g.status_code == 200:
                sha = g.json().get("sha")
        except Exception:
            pass
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "branch": BRANCH
        }
        if sha: body["sha"] = sha
        try:
            r = requests.put(url, headers=_gh_headers(), json=body, timeout=60)
            if r.status_code in (200, 201): return True
            if r.status_code in (409, 422): continue
        except Exception:
            continue
    return False

def dispatch(event_type, payload):
    if not (REPO and GH_TOKEN): return False
    try:
        r = requests.post(f"{API}/repos/{REPO}/dispatches", headers=_gh_headers(), json={"event_type": event_type, "client_payload": payload or {}}, timeout=30)
        return r.status_code == 204
    except Exception:
        return False

def hhmm(t):
    try:
        h, m = str(t).strip().split(":")[:2]
        return int(h), int(m)
    except Exception:
        return None

def next_upload_slot(times_bst, after_utc, min_lead_h=6.0):
    if not times_bst: return None
    earliest = after_utc + datetime.timedelta(hours=float(min_lead_h))
    base = now_utc().date(); best = None
    for d in range(0, 4):
        day = base + datetime.timedelta(days=d)
        for t in times_bst:
            hm = hhmm(t)
            if not hm: continue
            cand = datetime.datetime.combine(day, datetime.time(hm[0], hm[1])).replace(tzinfo=BST).astimezone(UTC)
            if cand and cand >= earliest and cand > now_utc() and (best is None or cand < best):
                best = cand
    return best

def validate_times(cfg):
    win = cfg.get("edit_window_bst", ["01:00", "06:00"])
    ws, we = hhmm(win[0]) or (1, 0), hhmm(win[1]) or (6, 0)
    start, end = ws[0] * 60 + ws[1], we[0] * 60 + we[1]
    limit = (start + int(cfg.get("min_edit_lead_hours", 6)) * 60) % 1440
    bad, good = [], []
    for t in cfg.get("fb_upload_times_bst", []) + cfg.get("yt_upload_times_bst", []):
        hm = hhmm(t)
        if not hm: bad.append(t); continue
        mins = hm[0] * 60 + hm[1]
        inside = (start <= mins < end) if start < end else (mins >= start or mins < end)
        (bad if (inside or mins < limit) else good).append(t)
    return good, bad
