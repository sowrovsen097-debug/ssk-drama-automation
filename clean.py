"""SSK DRAMA clean — পুরোনো/ব্যর্থ কাজের হিস্ট্রি মুছে অ্যাপ আবার একদম নতুনের মতো করে দেয়।

কী করে
  • ব্যর্থ (failed) ও বাতিল (cancelled) কাজের রেকর্ড মুছে ফেলে।
  • অনেক পুরোনো pending কাজ মুছে ফেলে (--keep-days 0 = শুধু আজকের কাজ থাকবে)।
  • ready / uploading / done কাজ কখনো মোছে না — নাহলে আপলোড হারিয়ে যেত।
  • progress.json-এর "রাতের এডিটিং শেষ" ফ্ল্যাগ মুছে দেয়, তাই অ্যাপ আবার নতুন দেখায়।
  • history.json (সফল আপলোডের তালিকা) শুধু --purge-history দিলে মোছে।

চালানো:  python clean.py [--keep-days 0] [--wipe-all] [--purge-history] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from collections import Counter
from pathlib import Path

KEEP_STATES = {"ready", "uploading", "done"}
DROP_STATES = {"failed", "cancelled"}
STALE_STATE = "preparing"
STALE_HOURS = 3
BD = dt.timezone(dt.timedelta(hours=6))

try:                      # Actions-এ core থাকে; লোকাল টেস্টে না থাকলেও চলে
    import core
except Exception:
    core = None


# ------------------------------------------------------------------ helpers
def bd_now() -> dt.datetime:
    return dt.datetime.now(BD)


def bd_stamp() -> str:
    return bd_now().isoformat(timespec="seconds")


def load(name: str, default):
    path = Path(name)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8") or "null") or default
        except Exception:
            return default
    if core is not None:
        try:
            data = core.read_json(name)
            if data:
                return data
        except Exception:
            pass
    return default


def save(name: str, data, message: str, dry_run: bool = False) -> str:
    if dry_run:
        return "dry-run"
    Path(name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if core is not None:
        try:
            core.write_json(name, data, message)
            return "github(api)"
        except Exception as exc:
            print(f"  ! core.write_json ব্যর্থ ({exc}) — git দিয়ে চেষ্টা করা হচ্ছে")
    for cmd in (["git", "add", name],
                ["git", "-c", "user.name=SSK clean", "-c", "user.email=ssk@local",
                 "commit", "-m", message],
                ["git", "push"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception:
            return "local"
        if out.returncode != 0 and cmd[1] != "commit":
            return "local"
    return "git"


def job_date(key, job) -> str:
    d = str((job or {}).get("date") or "")[:10]
    if len(d) == 10 and d[4] == "-":
        return d
    k = str(key)[:10]
    return k if len(k) == 10 and k[4] == "-" else ""


def stuck(job: dict, hours: int = STALE_HOURS) -> bool:
    """'preparing' অবস্থায় আটকে থাকা পুরোনো রেকর্ড কি না (টাইমআউটে রান মরে গেলে এটাই থাকে)।"""
    stamp = (job or {}).get("updated_at") or (job or {}).get("started_at") or (job or {}).get("at")
    ts = None
    if stamp:
        try:
            ts = dt.datetime.fromisoformat(str(stamp))
        except Exception:
            ts = None
    if ts is None:
        d = job_date("", job)
        if not d:
            return True
        try:
            ts = dt.datetime.fromisoformat(d + "T00:00:00+06:00")
        except Exception:
            return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=BD)
    return (bd_now() - ts).total_seconds() > max(1, hours) * 3600


def prune(jobs: dict, cutoff: str, wipe_all: bool = False, hours: int = STALE_HOURS):
    """(রাখা, ব্যর্থ-বাতিল, পুরোনো pending) — ready/done কখনো ছোঁয় না।"""
    keep, bad, old = {}, [], []
    for key, job in (jobs or {}).items():
        if not isinstance(job, dict):
            continue
        if wipe_all:
            old.append(key)
            continue
        state = str(job.get("state") or "")
        if state in DROP_STATES:
            bad.append(key)
            continue
        if state == STALE_STATE:
            if stuck(job, hours):
                old.append(key)
                continue
            keep[key] = job
            continue
        if state in KEEP_STATES:
            keep[key] = job
            continue
        date = job_date(key, job)
        if date and date < cutoff:
            old.append(key)
            continue
        keep[key] = job
    return keep, bad, old


def counts(jobs: dict) -> dict:
    return {k: v for k, v in sorted(Counter(str((j or {}).get("state"))
                                                for j in (jobs or {}).values()).items())}


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=0,
                    help="০ = শুধু আজকের কাজ থাকবে; ১ = গতকালও থাকবে")
    ap.add_argument("--wipe-all", action="store_true", help="সব কাজের রেকর্ড মুছে একদম নতুন")
    ap.add_argument("--purge-history", action="store_true", help="history.json-ও খালি করে")
    ap.add_argument("--stale-hours", type=int, default=STALE_HOURS,
                    help="'preparing' কত ঘণ্টা আটকে থাকলে বাসি বলে গণ্য হবে")
    ap.add_argument("--dry-run", action="store_true", help="কিছু না লিখে শুধু দেখায়")
    a = ap.parse_args()

    today = bd_now().date()
    cutoff = (today - dt.timedelta(days=max(0, a.keep_days))).isoformat()
    print(f"আজ {today.isoformat()} (BD) · cutoff {cutoff} · wipe_all={a.wipe_all} · dry_run={a.dry_run}")

    # ১) queue.json
    queue = load("queue.json", {})
    wrapped = isinstance(queue, dict) and isinstance(queue.get("jobs"), dict)
    jobs = (queue.get("jobs") if wrapped else queue) or {}
    before = counts(jobs)
    keep, bad, old = prune(jobs, cutoff, a.wipe_all, a.stale_hours)
    print(f"queue.json: আগে {len(jobs)}টি কাজ {before} → এখন {len(keep)}টি {counts(keep)}")
    print(f"  মুছে ফেলা — ব্যর্থ/বাতিল {len(bad)}টি: {bad[:14]}")
    print(f"  মুছে ফেলা — পুরোনো/আটকে-থাকা {len(old)}টি: {old[:14]}")
    if not a.dry_run:
        if wrapped:
            queue["jobs"] = keep
            queue["cleaned_at"] = bd_stamp()
            save("queue.json", queue, "SSK clean: পুরোনো কাজ সরানো")
        else:
            save("queue.json", keep, "SSK clean: পুরোনো কাজ সরানো")

    # ২) status.json (ড্যাশবোর্ড এটাই পড়ে)
    status = load("status.json", {})
    if isinstance(status, dict) and isinstance(status.get("jobs"), dict):
        sjobs = status["jobs"]
        skeep = prune(sjobs, cutoff, a.wipe_all, a.stale_hours)[0]
        status["jobs"] = skeep
        status["counts"] = {**{"pending": 0, "ready": 0, "done": 0, "failed": 0}, **counts(skeep)}
        status["cleaned_at"] = bd_stamp()
        status["note"] = "পরিষ্কার করা হয়েছে — পুরোনো/ব্যর্থ কাজের রেকর্ড মুছে ফেলা হয়েছে"
        print(f"status.json: আগে {len(sjobs)} → এখন {len(skeep)} · counts {status['counts']}")
        save("status.json", status, "SSK clean: status পরিষ্কার")

    # ৩) progress.json — "রাতের এডিটিং শেষ" ফ্ল্যাগ মুছে ফ্রেশ স্টেট
    prog = load("progress.json", {})
    was = str((prog or {}).get("stage") or "-")
    if not a.dry_run:
        save("progress.json", {"stage": "প্রস্তুত — নতুন করে শুরুর অপেক্ষায়",
                               "updated_at": bd_stamp(), "cleaned_at": bd_stamp(),
                               "cleared": was}, "SSK clean: progress রিসেট")
    print(f"progress.json: ছিল '{was}' → এখন 'প্রস্তুত — নতুন করে শুরুর অপেক্ষায়'")

    # ৪) history.json
    hist = load("history.json", {})
    if a.purge_history:
        hcount = sum(len(v) if isinstance(v, list) else 1 for v in (hist or {}).values())
        if not a.dry_run:
            save("history.json", {"facebook": [], "youtube": []}, "SSK clean: history খালি")
        print(f"history.json: {hcount}টি সফল আপলোডের রেকর্ড মুছে ফেলা হলো (--purge-history)")
    else:
        print("history.json: সফল আপলোডের তালিকা রাখা হলো (মুছতে --purge-history)")

    dropped = len(bad) + len(old)
    msg = (f"🧹 SSK DRAMA পরিষ্কার: {dropped}টি পুরোনো/ব্যর্থ কাজের রেকর্ড মুছে ফেলা হলো "
           f"(ব্যর্থ/বাতিল {len(bad)}, পুরোনো/আটকে-থাকা {len(old)})। এখন {len(keep)}টি কাজ বাকি।")
    print("\n" + msg)
    if core is not None and not a.dry_run:
        try:
            core.telegram(msg)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
