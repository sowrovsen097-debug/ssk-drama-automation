"""SSK DRAMA main — রাতের এডিটিং, Drive স্টোরেজ, সময়মতো আপলোড, বট কমান্ড, ম্যানুয়াল।

APP_MODE: prepare | upload | service | manual
নিয়ম: এডিট শুধু edit_window_bd (ডিফল্ট ০১:০০–০৬:০০ বাংলাদেশ) সময়ে; কোনো সময় ঠিক করলে
কমপক্ষে min_lead_hours (৬) ঘণ্টা আগে হতে হবে — নাহলে কাজ হবে না।
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import core
import media
import publish
from bot import handle_commands
from core import env

APP_MODE = env("APP_MODE", "service")
PLATFORMS = ("facebook", "youtube")
WORK = Path(core.WORK)
BOT_NAME = "SSK DRAMA"


def slots(cfg: dict) -> list:
    out = []
    for platform in PLATFORMS:
        times = cfg.get(f"{platform}_times_bd") or []
        for i in range(len(times)):
            out.append({"key": f"{platform}-{i + 1}", "platform": platform, "slot": i,
                        "time": times[i]})
    return out


def slot_key(platform: str, stamp: str, slot: int) -> str:
    return f"{stamp}-{platform}-{slot + 1}"


def free_space_ok(need_bytes: int, cfg: dict) -> bool:
    floor = 3 * 1024 ** 3
    try:
        free = core.drive_space()["free"]
    except Exception as exc:
        core.log("Drive কোটা দেখা যায়নি: " + core.clean(exc), "warn")
        return True
    if free - need_bytes < floor:
        raise core.SskError(f"Drive-এ জায়গা কম (বাকি {free / 1024 ** 3:.1f} GB) — "
                            f"{need_bytes / 1024 ** 3:.1f} GB দরকার")
    return True


# ------------------------------------------------------------------ prepare
def prepare_slot(cfg: dict, slot: dict, date: str) -> dict:
    key = slot_key(slot["platform"], date, slot["slot"])
    folder = WORK / key
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    core.set_job(key, {"state": "preparing", "platform": slot["platform"], "slot": slot["slot"],
                       "date": date, "time": slot["time"], "started_at": core.bd_stamp(),
                       "error": None, "edits": media.EDITS})
    try:
        source = media.pick_source(cfg, slot["platform"], slot["slot"])
        core.set_job(key, {"source_id": source["id"], "source_url": source["url"],
                           "source_title": source["title"], "source_channel": source["channel"],
                           "source_age_days": source["age_days"],
                           "source_duration": round(source["duration"], 1)})
        core.log(f"{key}: সোর্স → {source['title'][:60]} ({source['age_days']} দিন পুরোনো)")
        path = media.download_source(source, folder, cfg)
        if source.get("trim_to"):
            path = media.cap_length(path, source["trim_to"])

        info = media.probe(path)
        core.log(f"{key}: ডাউনলোড শেষ — {info['duration'] / 60:.1f} মিনিট, "
                 f"{info['size'] / 1048576:.0f} MB")
        core.write_json("progress.json", {"stage": "ตัดต่อ (scene trim)", "job": key,
                                          "updated_at": core.bd_stamp()}, "SSK progress")

        cuts = media.scene_cuts(path)
        keeps = media.choose_keeps(info["duration"], cuts, float(cfg.get("keep_ratio", 0.8125)))
        joined = media.trim_video(path, keeps, folder)
        core.set_job(key, {"trim": {"cuts": len(cuts), "pieces": len(keeps),
                                    "keep_ratio_target": cfg.get("keep_ratio", 0.8125)}})
        if path.name.startswith("source") and joined != path:
            path.unlink(missing_ok=True)

        audio = folder / "audio.wav"
        media.run([media.FFMPEG, "-y", "-i", str(joined), "-vn", "-ac", "1", "-ar", "16000",
                   str(audio)], timeout=3600)
        core.write_json("progress.json", {"stage": "সাবটাইটেল (Whisper)", "job": key,
                                          "updated_at": core.bd_stamp()}, "SSK progress")
        words = media.transcribe(audio, cfg.get("whisper_model", "base"), folder / "transcript.json")

        core.write_json("progress.json", {"stage": "রেন্ডার ও পূর্ণ ২০টি এডিট", "job": key,
                                          "updated_at": core.bd_stamp()}, "SSK progress")
        music = media.freesound_music(cfg, folder)
        out = folder / f"{slot['platform']}-ready.mp4"
        report = media.render(joined, out, slot["platform"], cfg, words, music)
        if joined != out and joined.exists():
            joined.unlink(missing_ok=True)
        core.log(f"{key}: রেন্ডার শেষ — {report['duration']:.0f}s, "
                 f"{report['size_bytes'] / 1048576:.0f} MB, সাবটাইটেল {report['subtitles']} শব্দ")

        if not core.in_window(cfg):
            raise core.SskError("এডিট উইন্ডো (রাত ১টা–সকাল ৬টা) শেষ — স্টোরেজে রাখা হয়নি")

        free_space_ok(report["size_bytes"], cfg)
        core.write_json("progress.json", {"stage": "Drive স্টোরেজে আপলোড", "job": key,
                                          "updated_at": core.bd_stamp()}, "SSK progress")
        headers = core.drive_headers()
        folder_id = core.drive_folder(cfg.get("drive_buffer_folder", "SSK DRAMA BUFFER"), headers)
        seo = publish.seo_package(cfg, source, slot["platform"], slot["slot"])
        meta = {"key": key, "platform": slot["platform"], "slot": slot["slot"],
                "time_bd": slot["time"], "source_id": source["id"], "source_url": source["url"],
                "source_title": source["title"], "seo": seo, "report": report,
                "created_at": core.bd_stamp()}
        stored = core.drive_upload(str(out), f"{key}.mp4", folder_id, headers)
        core.set_job(key, {"state": "ready", "drive_file_id": stored.get("id"),
                           "size_bytes": report["size_bytes"], "seo": seo, "report": report,
                           "source_url": source["url"], "source_id": source["id"],
                           "time": slot["time"], "stored_at": core.bd_stamp()})
        core.write_json(f"jobs/{key}.json", meta, f"SSK metadata {key}")

        out.unlink(missing_ok=True)
        audio.unlink(missing_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        core.log(f"{key}: Drive-এ সংরক্ষিত ({report['size_bytes'] / 1048576:.0f} MB) — লোকাল ফাইল মুছে ফেলা হয়েছে")
        core.telegram(f"🌙 {BOT_NAME}: {slot['platform']} slot {slot['slot'] + 1} তৈরি হয়ে "
                      f"স্টোরেজে রাখা হয়েছে ({report['duration'] / 60:.1f} মিনিট, "
                      f"{report['size_bytes'] / 1048576:.0f} MB)। আপলোড সময়: {slot['time']} (BD)")
        return {"key": key, "state": "ready", "size_bytes": report["size_bytes"]}
    except Exception as exc:
        err = core.clean(exc, 400)
        core.set_job(key, {"state": "failed", "error": err})
        core.log(f"{key}: ব্যর্থ — {err}", "error")
        core.telegram(f"⚠️ {BOT_NAME}: {slot['platform']} slot {slot['slot'] + 1} ব্যর্থ\n{err}")
        shutil.rmtree(folder, ignore_errors=True)
        return {"key": key, "state": "failed", "error": err}


def prepare_cycle(cfg: dict, force: bool = False) -> dict:
    if cfg.get("paused") and not force:
        return {"skipped": "সিস্টেম পজ করা আছে"}
    if not force and not core.in_window(cfg):
        return {"skipped": f"এডিট উইন্ডোর বাইরে (এখন {core.bd_hm()} BD)"}
    date = core.bd_now().strftime("%Y-%m-%d")
    results, done_keys = [], {k for k, v in core.jobs().items() if v.get("state") == "ready"}
    for slot in slots(cfg):
        key = slot_key(slot["platform"], date, slot["slot"])
        if key in done_keys:
            results.append({"key": key, "state": "already-ready"})
            continue
        ok, note = core.lead_check(cfg, slot["time"])
        if not ok:
            core.log(f"{key}: {note}", "warn")
        results.append(prepare_slot(cfg, slot, date))
        core.write_status(cfg)
    core.write_json("progress.json", {"stage": "রাতের এডিটিং শেষ", "updated_at": core.bd_stamp()},
                    "SSK progress")
    core.write_status(cfg)
    return {"date": date, "results": results}


# ------------------------------------------------------------------- upload
def upload_slot(cfg: dict, job_key: str) -> dict:
    job = core.jobs().get(job_key, {})
    if job.get("state") != "ready":
        return {"key": job_key, "skipped": job.get("state")}
    core.set_job(job_key, {"state": "uploading"})
    folder = WORK / job_key
    folder.mkdir(parents=True, exist_ok=True)
    local = folder / "ready.mp4"
    try:
        core.drive_download(job["drive_file_id"], str(local))
        seo = job.get("seo") or {}
        platform = job["platform"]
        url = (publish.upload_youtube(str(local), seo, cfg) if platform == "youtube"
               else publish.upload_facebook(str(local), seo, cfg))
        remote_id = url.rstrip("/").split("/")[-1].split("=")[-1]
        processed = publish.confirm_processing(platform, remote_id, cfg)
        core.add_history(platform, {"source_id": job.get("source_id"), "key": job_key,
                                    "remote_url": url, "at": core.bd_stamp()})
        core.set_job(job_key, {"state": "done", "remote_url": url, "uploaded_at": core.bd_stamp(),
                               "processed": processed})
        core.drive_delete(job["drive_file_id"])
        core.set_job(job_key, {"drive_file_id": None, "deleted_at": core.bd_stamp(),
                               "storage_deleted": True})
        shutil.rmtree(folder, ignore_errors=True)
        core.log(f"{job_key}: আপলোড সম্পন্ন — {url}")
        core.telegram(f"⬆️ {BOT_NAME}: {job_key} আপলোড হয়েছে\n{url}\n"
                      + ("স্টোরেজ থেকে মুছে ফেলা হয়েছে।" if processed else
                         "প্রসেসিং নিশ্চিত হয়নি — স্টোরেজে রাখা আছে।"))
        return {"key": job_key, "state": "done", "url": url}
    except Exception as exc:
        err = core.clean(exc, 400)
        core.set_job(job_key, {"state": "ready", "last_error": err})
        core.log(f"{job_key}: আপলোড ব্যর্থ — {err}", "error")
        core.telegram(f"⚠️ {BOT_NAME}: {job_key} আপলোড ব্যর্থ\n{err}")
        shutil.rmtree(folder, ignore_errors=True)
        return {"key": job_key, "state": "failed", "error": err}


def upload_cycle(cfg: dict) -> dict:
    if cfg.get("paused"):
        return {"skipped": "সিস্টেম পজ করা আছে"}
    results, now = [], core.bd_now()
    for job_key, job in core.jobs().items():
        if job.get("state") != "ready":
            continue
        when = job.get("time") or ""
        if not core.hhmm_ok(when):
            continue
        target = now.replace(hour=int(when[:2]), minute=int(when[3:]), second=0, microsecond=0)
        delta = (now - target).total_seconds() / 60.0
        if 0 <= delta <= 90:
            results.append(upload_slot(cfg, job_key))
    core.write_status(cfg)
    return {"due": results}


# ------------------------------------------------------------------ service
def cleanup(cfg: dict) -> int:
    keep = int(cfg.get("storage_retention_days", 3))
    removed = 0
    for job_key, job in core.jobs().items():
        if job.get("state") == "done" and job.get("drive_file_id"):
            core.drive_delete(job["drive_file_id"])
            core.set_job(job_key, {"drive_file_id": None, "deleted_at": core.bd_stamp()})
            removed += 1
    return removed


def service_cycle(cfg: dict) -> dict:
    handled = handle_commands(cfg)
    removed = cleanup(cfg)
    core.write_status(cfg)
    return {"commands": handled, "drive_cleaned": removed, "at": core.bd_stamp()}


# ------------------------------------------------------------------- manual
def manual_run(cfg: dict) -> dict:
    source = env("MANUAL_SOURCE")
    platform = env("MANUAL_PLATFORM", "youtube")
    when = env("MANUAL_TIME") or None
    title = env("MANUAL_TITLE")
    desc = env("MANUAL_DESCRIPTION")
    do_edit = env("MANUAL_EDIT", "1") in ("1", "true", "yes")
    if not source:
        raise core.SskError("MANUAL_SOURCE (লিংক) দরকার")
    if when:
        ok, note = core.lead_check(cfg, when)
        if not ok:
            raise core.SskError(note)
        core.telegram("🕒 " + BOT_NAME + ": " + note)
    fake_slot = {"key": f"manual-{platform}", "platform": platform, "slot": 0, "time": when or ""}
    fake_source = {"id": source, "url": source, "title": title or "SSK DRAMA", "channel": "manual",
                   "age_days": 0, "duration": 0, "views": 0, "trim_to": 0}
    folder = WORK / fake_slot["key"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if source.startswith("http") and "drive.google.com" in source:
            file_id = ""
            for part in source.split("/"):
                if len(part) > 20 and part not in ("view", "edit", "file"):
                    file_id = part.split("?")[0]
                    break
            local = core.drive_download(file_id, str(folder / "source.mp4"))
            path = Path(local)
        else:
            path = media.download_source(fake_source, folder, cfg)
        if do_edit:
            info = media.probe(path)
            cuts = media.scene_cuts(path)
            keeps = media.choose_keeps(info["duration"], cuts, float(cfg.get("keep_ratio", 0.8125)))
            joined = media.trim_video(path, keeps, folder)
            audio = folder / "audio.wav"
            media.run([media.FFMPEG, "-y", "-i", str(joined), "-vn", "-ac", "1", "-ar", "16000",
                       str(audio)], timeout=3600)
            words = media.transcribe(audio, cfg.get("whisper_model", "base"), folder / "transcript.json")
            out = folder / f"{platform}-ready.mp4"
            media.render(joined, out, platform, cfg, words, media.freesound_music(cfg, folder))
        else:
            out = path
        seo = publish.seo_package(cfg, {**fake_source, "title": title or fake_source["title"]},
                                  platform, 0)
        if desc:
            seo["description"] = desc
        url = (publish.upload_youtube(str(out), seo, cfg) if platform == "youtube"
               else publish.upload_facebook(str(out), seo, cfg))
        core.add_history(platform, {"source_id": source, "key": fake_slot["key"],
                                    "remote_url": url, "at": core.bd_stamp()})
        core.telegram(f"✅ {BOT_NAME}: ম্যানুয়াল আপলোড সম্পন্ন\n{url}")
        return {"state": "done", "url": url}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------- main
def main() -> int:
    cfg = core.load_config()
    try:
        if APP_MODE == "prepare":
            result = prepare_cycle(cfg, force=env("APP_FORCE") in ("1", "true", "yes"))
        elif APP_MODE == "upload":
            result = upload_cycle(cfg)
        elif APP_MODE == "manual":
            result = manual_run(cfg)
        else:
            result = service_cycle(cfg)
    except Exception as exc:
        result = {"error": core.clean(exc, 500)}
        core.log("মূল রান ব্যর্থ: " + core.clean(exc, 400), "error")
        try:
            core.telegram(f"⚠️ {BOT_NAME} ব্যর্থ: " + core.clean(exc, 400))
            core.write_status(cfg)
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    if "--bot" in sys.argv:
        print(json.dumps({"commands": handle_commands(core.load_config())}, default=str))
        raise SystemExit(0)
    if "--scan" in sys.argv:
        import bot
        print(bot.scan_report(core.load_config()), flush=True)
        raise SystemExit(0)
    raise SystemExit(main())
