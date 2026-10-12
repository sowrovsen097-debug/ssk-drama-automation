"""SSK DRAMA bot — Actions-এর দিকের কাজ: সোর্স স্ক্যান রিপোর্ট ও বটের কমান্ড পালন।"""
from __future__ import annotations

import time
from pathlib import Path

import core
import media
from core import env


def scan_report(cfg: dict, probe: int | None = None) -> str:
    lines = [f"yt-dlp সংস্করণ: {_ytdlp_version()}"]
    cookies = env("YT_COOKIES")
    lines.append("কুকি: " + ("আছে" if cookies else "নেই — বট-চেকের মূল কারণ এটাই"))
    limit = int(probe or cfg.get("metadata_probe_limit", 15))
    day = int(cfg.get("day_filter", 60))
    cap = float(cfg.get("max_duration_minutes", 80)) * 60
    for platform in ("facebook", "youtube"):
        for channel in (cfg.get(f"{platform}_channels") or []):
            url = str(channel).split("?")[0].rstrip("/")
            try:
                entries = media.scan_channel(url, limit, cookies)
            except Exception as exc:
                lines.append(f"❌ {url}\n   {core.clean(exc, 200)}")
                continue
            good = long_ = young = nometa = 0
            for e in entries:
                dur = float(e.get("duration") or 0)
                stamp = e.get("timestamp") or e.get("release_timestamp")
                age = (time.time() - float(stamp)) / 86400 if stamp else None
                if age is None:
                    nometa += 1
                elif age < day:
                    young += 1
                elif dur and dur > cap:
                    long_ += 1
                else:
                    good += 1
            lines.append(f"✅ {url}\n   পাওয়া {len(entries)} · ব্যবহারযোগ্য {good} · "
                         f"বেশি লম্বা {long_} · কম পুরোনো {young} · তারিখ নেই {nometa}")
    return "\n".join(lines)


def _ytdlp_version() -> str:
    try:
        import subprocess
        return subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True,
                              timeout=120).stdout.strip() or "অজানা"
    except Exception:
        try:
            import yt_dlp
            return getattr(yt_dlp.version, "__version__", "অজানা")
        except Exception:
            return "অজানা"


def handle_commands(cfg: dict) -> bool:
    """Cloudflare Worker-এর রেখে যাওয়া কমান্ড (bot_command.json / chat/) পালন করে।"""
    handled = False
    command, sha = core.read_json("bot_command.json", {})
    if isinstance(command, dict) and command.get("state") == "pending":
        mode = command.get("mode")
        if mode == "scan":
            try:
                report = scan_report(cfg, (command.get("extra") or {}).get("probe"))
            except Exception as exc:
                report = "স্ক্যান ব্যর্থ: " + core.clean(exc)
            core.telegram("🔎 SSK DRAMA স্ক্যান\n\n" + report[:3500])
            command.update({"state": "done", "result": report[:1500], "finished_at": core.bd_stamp()})
        else:
            command.update({"state": "skipped", "finished_at": core.bd_stamp(),
                            "result": f"{mode} সরাসরি চালানো যায়নি"})
        try:
            core.write_json("bot_command.json", command, "SSK bot command", sha)
        except Exception:
            pass
        handled = True

    listing, _ = core.read_json("chat/index.json", {})
    if isinstance(listing, dict):
        for name in (listing.get("pending") or [])[:5]:
            payload, sha2 = core.read_json("chat/" + name, {})
            if not isinstance(payload, dict) or payload.get("state") not in ("pending", None):
                continue
            try:
                reply = core.ask(payload.get("message", ""), core.snapshot(cfg))
            except Exception as exc:
                reply = "উত্তর দিতে পারিনি: " + core.clean(exc)
            payload.update({"state": "done", "reply": reply, "answered_at": core.bd_stamp()})
            try:
                core.write_json("chat/" + name, payload, "SSK chat answer", sha2)
            except Exception:
                pass
            core.telegram("💬 প্রশ্ন: " + str(payload.get("message"))[:400] + "\n\n" + reply)
            handled = True
    return handled


if __name__ == "__main__":
    import sys
    cfg = core.load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        text = scan_report(cfg)
        print(text, flush=True)
        core.telegram("🔎 SSK DRAMA স্ক্যান\n\n" + text[:3500])
    else:
        print("কমান্ড পালন:", handle_commands(cfg), flush=True)
