import base64
import copy
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BD = ZoneInfo("Asia/Dhaka")


def now():
    return dt.datetime.now(BD)


def stamp():
    return now().isoformat()


def clean_error(exc):
    text = str(exc)

    for key, value in os.environ.items():
        if value and (
            "TOKEN" in key
            or "SECRET" in key
            or "API_KEY" in key
        ):
            text = text.replace(value, "[hidden]")

    return text[:800]


def validate(c):
    for platform in ("facebook", "youtube"):
        urls = c[platform + "_channels"]
        times = c[platform + "_times_bd"]

        if len(urls) != 3 or len(times) != 3:
            raise ValueError(
                "Each platform requires exactly 3 channels and 3 times"
            )

        for url in urls:
            if not re.fullmatch(
                r"https://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s]+",
                url,
            ):
                raise ValueError("Use a valid YouTube HTTPS URL")

        if len(set(times)) != 3:
            raise ValueError(
                "Upload times must be different within each platform"
            )

        for value in times:
            if not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d",
                value,
            ):
                raise ValueError("Invalid time: " + value)

            if "01:00" <= value <= "06:00":
                raise ValueError(
                    "01:00 through 06:00 BD is reserved for editing"
                )

    editing = c["editing"]

    if not 0.75 <= float(editing["keep_ratio"]) <= 0.875:
        raise ValueError(
            "keep_ratio must be between 0.75 and 0.875"
        )

    if editing["whisper_model"] not in ("tiny", "base", "small"):
        raise ValueError("Unsupported Whisper model")

    return c


def due(item, config, at=None):
    at = at or now()

    if dt.time(1) <= at.time().replace(tzinfo=None) <= dt.time(6):
        return False

    value = config[item["platform"] + "_times_bd"][item["slot"]]

    target = dt.datetime.fromisoformat(
        item["date"] + "T" + value
    ).replace(tzinfo=BD)

    return at >= target


class Repo:
    def __init__(self):
        self.name = os.environ["APP_REPOSITORY"]
        self.base = "https://api.github.com/repos/" + self.name
        self.s = requests.Session()

        self.s.headers.update({
            "Authorization": "Bearer " + os.environ["APP_TOKEN"],
            "Accept": "application/vnd.github+json",
        })

    def api(self, method, path, **kwargs):
        url = (
            path
            if path.startswith("https://")
            else self.base + path
        )

        response = self.s.request(
            method,
            url,
            timeout=kwargs.pop("timeout", 60),
            **kwargs,
        )

        if not response.ok:
            raise RuntimeError(
                f"GitHub {response.status_code}: "
                f"{response.text[:250]}"
            )

        return response

    def read(self, path, default=None):
        response = self.s.get(
            self.base + "/contents/" + path,
            timeout=40,
        )

        if response.status_code == 404:
            return copy.deepcopy(default), None

        response.raise_for_status()
        data = response.json()

        content = json.loads(
            base64.b64decode(data["content"])
        )

        return content, data["sha"]

    def change(self, path, default, fn):
        # Retry state conflicts only, not publishing operations.
        for attempt in range(6):
            data, sha = self.read(path, default)
            updated = fn(copy.deepcopy(data))

            if updated == data and sha:
                return updated

            payload = {
                "message": "SSK state: " + path,
                "content": base64.b64encode(
                    json.dumps(
                        updated,
                        ensure_ascii=False,
                        indent=2,
                    ).encode()
                ).decode(),
            }

            if sha:
                payload["sha"] = sha

            response = self.s.put(
                self.base + "/contents/" + path,
                json=payload,
                timeout=50,
            )

            if response.status_code in (409, 422):
                time.sleep(attempt + 1)
                continue

            response.raise_for_status()
            return updated

        raise RuntimeError("State conflict: " + path)

    def write(self, path, data):
        return self.change(
            path,
            {},
            lambda _: copy.deepcopy(data),
        )

    def release(self, date):
        tag = "ssk-" + date
        page = 1

        # Draft releases must be located by listing releases.
        while True:
            items = self.api(
                "GET",
                "/releases",
                params={
                    "per_page": 100,
                    "page": page,
                },
            ).json()

            for item in items:
                if item["tag_name"] == tag:
                    return item

            if len(items) < 100:
                break

            page += 1

        return self.api(
            "POST",
            "/releases",
            json={
                "tag_name": tag,
                "target_commitish": os.getenv(
                    "APP_BRANCH",
                    "main",
                ),
                "name": "SSK temporary videos " + date,
                "draft": True,
                "body": (
                    "Temporary daily rendering queue; "
                    "automatically cleaned."
                ),
            },
        ).json()

    def store(self, release, file):
        file = Path(file)

        if file.stat().st_size >= 2 * 1024**3:
            raise RuntimeError(
                "Rendered file exceeds the 2 GiB asset limit"
            )

        url = release["upload_url"].split("{")[0]

        with file.open("rb") as stream:
            return self.api(
                "POST",
                url,
                params={"name": file.name},
                data=stream,
                headers={
                    "Content-Type": "application/octet-stream"
                },
                timeout=1200,
            ).json()

    def fetch_asset(self, asset_id, path):
        response = self.s.get(
            self.base + f"/releases/assets/{asset_id}",
            headers={
                "Accept": "application/octet-stream"
            },
            allow_redirects=False,
            timeout=60,
            stream=True,
        )

        # Do not forward the GitHub token to a redirected blob host.
        if response.status_code in (301, 302, 303, 307, 308):
            url = response.headers["Location"]
            response.close()

            response = requests.get(
                url,
                timeout=(30, 300),
                stream=True,
            )

        response.raise_for_status()

        with response, open(path, "wb") as stream:
            for chunk in response.iter_content(1024 * 1024):
                stream.write(chunk)

    def delete_asset(self, asset_id):
        response = self.s.delete(
            self.base + f"/releases/assets/{asset_id}",
            timeout=60,
        )

        if response.status_code not in (204, 404):
            response.raise_for_status()


def telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat:
        return

    try:
        response = requests.post(
            "https://api.telegram.org/bot"
            + token
            + "/sendMessage",
            json={
                "chat_id": chat,
                "text": text[:4000],
            },
            timeout=25,
        )

        if not response.ok or not response.json().get("ok"):
            print(
                "Telegram send failed:",
                response.status_code,
            )

    except requests.RequestException:
        print("Telegram temporarily unavailable")


def ai(config, prompt, images=(), structured=False):
    model = config["gemini_model"]
    parts = [{"text": prompt}]

    for image in images:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image).decode(),
            }
        })

    generation = {
        "temperature": 0.25,
        "maxOutputTokens": 4096,
    }

    if structured:
        generation["responseMimeType"] = "application/json"

    response = requests.post(
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        + model
        + ":generateContent",
        headers={
            "x-goog-api-key": os.environ["GEMINI_API_KEY"]
        },
        json={
            "contents": [{"parts": parts}],
            "generationConfig": generation,
        },
        timeout=150,
    )

    response.raise_for_status()
    body = response.json()

    text = "".join(
        part.get("text", "")
        for part in body["candidates"][0]["content"]["parts"]
    )

    return json.loads(text) if structured else text
