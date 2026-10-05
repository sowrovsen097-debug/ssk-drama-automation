# -*- coding: utf-8 -*-
"""SSK DRAMA — Gemini (REST, কোনো SDK লাগে না) + Freesound CC0 + SEO"""
import json, os, random, re, requests

GEMINI_KEY    = os.getenv("GEMINI_API_KEY", "")
FREESOUND_KEY = os.getenv("FREESOUND_API_KEY", "")
GEN_URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"


def _extract_json(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def gemini(prompt, models, temperature=0.9, max_tokens=2048, timeout=120):
    """মডেল লিস্ট একটার পর একটা ট্রাই করে যেটা কাজ করে সেটাই ফেরত দেয়।"""
    if not GEMINI_KEY:
        return None, None
    for m in models:
        try:
            r = requests.post(
                GEN_URL.format(m=m),
                params={"key": GEMINI_KEY},
                json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": temperature,
                                           "maxOutputTokens": max_tokens}},
                timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                cand = (d.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts).strip()
                if txt:
                    print(f"[gemini] OK → {m}")
                    return txt, m
            else:
                print(f"[gemini] {m} HTTP {r.status_code}")
        except Exception as e:
            print(f"[gemini] {m} error: {e}")
    return None, None


def generate_seo(video, cfg, total_seconds, ranges, words):
    """ভিডিওর টাইটেল/ডেসক্রিপশন/ট্যাগ/হ্যাশট্যাগ/চ্যাপ্টার — বাংলায় SEO ফ্রেন্ডলি।"""
    # চ্যাপ্টার তৈরি (কাট করা টাইমলাইন অনুযায়ী)
    chapters, off = [], 0.0
    for i, (s, e) in enumerate(ranges[:20]):
        chapters.append({"at": off, "label": f"অংশ {i+1}"})
        off += (e - s)
    chap_text = "\n".join(
        f"{int(c['at']//3600):02d}:{int((c['at']%3600)//60):02d}:{int(c['at']%60):02d} {c['label']}"
        for c in chapters if c["at"] > 10)

    sample = " ".join(w.get("text", "") for w in words[:120]) if words else ""
    prompt = f"""তুমি একজন বাংলাদেশি ইউটিউব/ফেসবুক কনটেন্ট SEO এক্সপার্ট।
নিচের ভিডিওর জন্য একদম নতুন, ভাইরাল হওয়ার মতো বাংলা মেটাডাটা বানাও।

মূল টাইটেল: {video.get('title','')}
মূল ডেসক্রিপশন: {(video.get('description') or '')[:1200]}
ভিডিওর দৈর্ঘ্য: {total_seconds/60:.1f} মিনিট
ডায়ালগের নমুনা: {sample[:600]}
চ্যাপ্টার: {chap_text}

শুধু এই JSON ফরম্যাটে উত্তর দাও, এর বাইরে একটি অক্ষরও লিখবে না:
{{
 "title": "৯৫ অক্ষরের কম, কৌতূহল জাগানো বাংলা টাইটেল",
 "description": "৪-৬ লাইনের গোছানো বাংলা বর্ণনা, শেষে চ্যাপ্টার টাইমস্ট্যাম্প, তারপর ১২টি হ্যাশট্যাগ",
 "tags": ["১২টির কম নয়, বাংলা+হিন্দি মিক্স ট্যাগ"],
 "hashtags": ["#১২টিStart"],
 "thumbnail_text": "থাম্বনেইলে ছোট ৩-৪ শব্দ"
}}"""
    txt, model = gemini(prompt, cfg.get("gemini_models", ["gemini-2.5-flash"]))
    data = _extract_json(txt) if txt else None
    if not data:
        data = {
            "title": (video.get("title") or "নতুন নাটক")[:90],
            "description": f"{video.get('title','')}\n\nSSK DRAMA\n" + chap_text
                           + "\n\n#BanglaDrama #SSKDrama #CrimeStory #BanglaNatok",
            "tags": ["Bangla Drama", "SSK Drama", "Crime Story", "Bangla Natok"],
            "hashtags": ["#BanglaDrama", "#SSKDrama"],
            "thumbnail_text": "নতুন পর্ব"}
    if chap_text:
        if "অংশ ১" not in data.get("description", ""):
            data["description"] = data["description"].rstrip() + "\n\n" + chap_text
    data["_model"] = model
    return data


def ask_ai(question, context=""):
    """অ্যাপ/টেলিগ্রামের AI সহকারী।"""
    prompt = f"""তুমি "SSK DRAMA" নামের একটি অটোমেশন সিস্টেমের বন্ধুত্বপূর্ণ বাংলা AI অ্যাসিস্ট্যান্ট।
সিস্টেম সম্পর্কে তথ্য:
{context[:4000]}

ইউজার লিখেছে: {question}

নিয়ম: সবসময় সহজ বাংলায়, ছোট ও কাজের মতো উত্তর দাও। কোড লাগলে কোড ব্লকে দাও।"""
    txt, model = gemini(prompt, ["gemini-3-flash", "gemini-2.5-flash",
                                 "gemini-flash-latest", "gemini-2.0-flash"],
                        temperature=0.7)
    return txt or "❌ এখন AI উত্তর দিতে পারছে না (API কী বা কোটা চেক করুন)।"


def fetch_cc0_music(query, out_path, timeout=60):
    """Freesound থেকে শুধু CC0 (কপিরাইট-ফ্রি) মিউজিক নামায়।"""
    if not FREESOUND_KEY:
        return None
    try:
        r = requests.get("https://freesound.org/apiv2/search/text/",
                         params={"query": query,
                                 "filter": 'license:"Creative Commons 0"',
                                 "fields": "id,name,duration,previews,license",
                                 "page_size": 20,
                                 "token": FREESOUND_KEY},
                         timeout=timeout)
        if r.status_code != 200:
            print("[music] search failed", r.status_code)
            return None
        res = r.json().get("results", [])
        random.shuffle(res)
        for s in res:
            if (s.get("duration") or 0) < 30:
                continue
            url = (s.get("previews") or {}).get("preview-hq-mp3")
            if not url:
                continue
            a = requests.get(url, timeout=timeout)
            if a.status_code == 200 and len(a.content) > 50000:
                with open(out_path, "wb") as f:
                    f.write(a.content)
                print(f"[music] {s.get('name')}")
                return {"path": out_path, "name": s.get("name"), "license": "CC0"}
    except Exception as e:
        print("[music] error", e)
    return None
