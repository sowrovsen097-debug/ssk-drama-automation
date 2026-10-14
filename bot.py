"""SSK DRAMA bot — Actions-এর দিকের কাজ: স্ক্যান রিপোর্ট, রেট্রাই/বাতিল/পরিষ্কার, চ্যাটের উত্তর।

কমান্ড ফাইল: bot_command.json → {"mode": "scan|retry|cancel|cleanup", "state": "pending", "key": "..."}
চ্যাট ফাইল: chat/<id>.json + chat/index.json
"""
from __future__ import annotations

import json
import sys
import time

import core
import media
from core import env


def _ytdlp_version() -> str:
    try:
        import subprocess
        out = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=120)
        return (out.stdout or "").strip() or "অজানা"
    except Exception:
        try:
            import yt_dlp
            return getattr(yt_dlp.version, "__version__", "অজানা")
        except Exception:
            return "অজানা"


def scan_report(cfg: dict, probe: int | None = None) -> str:
    """প্রতিটি সোর্স চ্যানেল স্ক্যান করে বলে: কতটি ব্যবহারযোগ্য, আর কেন বাদ পড়ছে।"""
    lines = [f"yt-dlp: {_ytdlp_version()}"]
    cookies = env("YT_COOKIES")
    lines.append("কুকি: " + (f"আছে ({len(cookies)} অক্ষর)" if cookies else "নেই — বট-চেকের মূল কারণ"))
    limit = int(probe or cfg.get("metadata_probe_limit", 15))
    day = 0 if cfg.get("allow_any_age") else int(cfg.get("day_filter", 60))
    cap = float(cfg.get("max_duration_minutes", 80)) * 60
    floor = float(cfg.get("min_duration_seconds", 60))
    used = set()
    for platform in ("facebook", "youtube"):
        used |= core.history_sources(platform)
        try:
            for job in core.jobs().values():
                if job.get("source_id"):
                    used.add(job["source_id"])
        except Exception:
            pass

    for platform in ("facebook", "youtube"):
        channels = media.clean_channels(cfg, platform)
        raw_count = len(cfg.get(f"{platform}_channels") or [])
        lines.append(f"\n— {platform.upper()} ({len(channels)}টি বৈধ চ্যানেল) —")
        if raw_count > len(channels):
            lines.append(f"⚠️ {raw_count - len(channels)}টি লিংক বোঝা যায়নি / খালি ছিল — ঠিক করো")
        if not channels:
            lines.append("⚠️ কোনো বৈধ চ্যানেল সেট করা নেই")
            continue
        for idx, url in enumerate(channels):
            try:
                entries = media.scan_channel(url, limit, cookies)
            except Exception as exc:
                lines.append(f"❌ স্লট {idx + 1} · {url}\n   {core.clean(exc, 220)}")
                continue
            if not entries:
                lines.append(f"⚠️ স্লট {idx + 1} · {url}\n   তালিকা খালি — কুকি/লিংক দেখো")
                continue
            good = long_ = young = nometa = already = 0
            newest, oldest = None, None
            for e in entries:
                dur = float(e.get("duration") or 0)
                stamp = e.get("timestamp") or e.get("release_timestamp")
                age = (time.time() - float(stamp)) / 86400 if stamp else None
                if e.get("id") in used:
                    already += 1
                    continue
                if age is None:
                    nometa += 1
                elif age < day:
                    young += 1
                elif dur and dur < floor:
                    young += 1
                elif dur and dur > cap and not cfg.get("trim_long_sources"):
                    long_ += 1
                else:
                    good += 1
                if age is not None:
                    newest = age if newest is None else min(newest, age)
                    oldest = age if oldest is None else max(oldest, age)
            span = ""
            if newest is not None:
                span = f" · বয়স {newest:.0f}–{oldest:.0f} দিন"
            lines.append(f"✅ স্লট {idx + 1} · {url}\n   পাওয়া {len(entries)} · ব্যবহারযোগ্য {good}"
                         f" · বেশি লম্বা {long_} · কম পুরোনো/খাটো {young}"
                         f" · তারিখ নেই {nometa} · আগেই ব্যবহৃত {already}{span}")
    lines.append("\nমনে রাখো: ব্যবহারযোগ্য ০ হলে ভিডিও তৈরি হবে না — "
                 f"day_filter ({day or 'যেকোনো'} দিন) কমাও, trim_long_sources চালু করো, "
                 "বা metadata_probe_limit বাড়াও।")
    return "\n".join(lines)


def do_retry(key: str) -> str:
    job = core.jobs().get(key)
    if not job:
        return f"{key}: এমন কোনো কাজ নেই"
    if job.get("state") == "uploading":
        return f"{key}: এখন আপলোড চলছে — শেষ হতে দাও"
    core.set_job(key, {"state": "pending", "error": None, "retry_at": core.bd_stamp()})
    core.log(f"{key}: আবার চালানোর জন্য তৈরি করা হলো")
    return f"{key}: আবার চালানোর জন্য তৈরি (পরের রাতে/রানে এডিট হবে)"


def do_cancel(key: str) -> str:
    job = core.jobs().get(key)
    if not job:
        return f"{key}: এমন কোনো কাজ নেই"
    if job.get("state") == "uploading":
        return f"{key}: আপলোড চলছে — বাতিল করলে অসম্পূর্ণ আপলোড হতে পারে, তাই থামানো হলো না"
    if job.get("drive_file_id"):
        core.drive_delete(job["drive_file_id"])
    core.set_job(key, {"state": "cancelled", "cancelled_at": core.bd_stamp(), "drive_file_id": None})
    core.log(f"{key}: বাতিল করা হলো")
    return f"{key}: বাতিল হয়েছে (স্টোরেজ থেকেও মুছে ফেলা হয়েছে)"


def do_cleanup(cfg: dict) -> str:
    """যে ফাইলগুলো আপলোড হয়ে গেছে বা বাতিল হয়েছে, সেগুলো Drive থেকে মুছে দেয়।"""
    removed, kept = 0, 0
    for key, job in core.jobs().items():
        if not job.get("drive_file_id"):
            continue
        if job.get("state") in ("done", "cancelled"):
            core.drive_delete(job["drive_file_id"])
            core.set_job(key, {"drive_file_id": None, "deleted_at": core.bd_stamp(),
                               "storage_deleted": True})
            removed += 1
        else:
            kept += 1
    return f"{removed}টি ফাইল Drive থেকে মুছে ফেলা হলো · {kept}টি এখনো দরকার, রাখা হলো"


def scan_and_report(cfg: dict, probe=None) -> str:
    """একবারে স্ক্যান + টেলিগ্রামে পাঠানো।"""
    text = scan_report(cfg, probe)
    core.telegram("🔎 SSK DRAMA স্ক্যান\n\n" + text[:3500])
    return text


def handle_commands(cfg: dict) -> bool:
    handled = False

    command, sha = core.read_json("bot_command.json", {})
    if isinstance(command, dict) and command.get("state") == "pending":
        mode = str(command.get("mode") or "")
        extra = command.get("extra") or {}
        key = str(command.get("key") or extra.get("key") or "")
        try:
            if mode == "scan":
                result = scan_report(cfg, extra.get("probe"))
                core.telegram("🔎 SSK DRAMA স্ক্যান\n\n" + result[:3500])
            elif mode == "retry":
                result = do_retry(key)
                core.telegram("🔁 " + result)
            elif mode == "cancel":
                result = do_cancel(key)
                core.telegram("✕ " + result)
            elif mode == "cleanup":
                result = do_cleanup(cfg)
                core.telegram("🧹 " + result)
            else:
                result = f"{mode}: অজানা কমান্ড"
        except Exception as exc:
            result = "ব্যর্থ: " + core.clean(exc)
            core.log("বট কমান্ড ব্যর্থ (" + mode + "): " + core.clean(exc), "error")
            core.telegram("⚠️ " + result)
        command.update({"state": "done", "result": str(result)[:1500],
                        "finished_at": core.bd_stamp()})
        try:
            core.write_json("bot_command.json", command, "SSK bot command result", sha)
        except Exception as exc:
            core.log("bot_command.json লেখা যায়নি: " + core.clean(exc), "warn")
        handled = True

    listing, lsha = core.read_json("chat/index.json", {})
    if isinstance(listing, dict):
        answered = []
        for name in (listing.get("pending") or []):
            payload, psha = core.read_json("chat/" + name, {})
            if not isinstance(payload, dict) or payload.get("reply"):
                answered.append(name)
                continue
            try:
                reply = core.ask(str(payload.get("message") or ""), core.snapshot(cfg))
            except Exception as exc:
                reply = "উত্তর দিতে পারিনি: " + core.clean(exc)
            payload.update({"state": "done", "reply": reply, "answered_at": core.bd_stamp()})
            try:
                core.write_json("chat/" + name, payload, "SSK chat answer", psha)
            except Exception:
                pass
            core.telegram("💬 প্রশ্ন: " + str(payload.get("message"))[:400] + "\n\n" + reply)
            answered.append(name)
            handled = True
        if answered:
            rest = [n for n in (listing.get("pending") or []) if n not in answered]
            listing["pending"] = rest
            listing["done"] = ((listing.get("done") or []) + answered)[-40:]
            try:
                core.write_json("chat/index.json", listing, "SSK chat index", lsha)
            except Exception:
                pass

    return handled


if __name__ == "__main__":
    cfg = core.load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        print(scan_and_report(cfg), flush=True)
    else:
        print(json.dumps({"commands_handled": handle_commands(cfg)}, ensure_ascii=False), flush=True)
