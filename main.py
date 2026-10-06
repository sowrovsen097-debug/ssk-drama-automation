import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path

import requests

from core import (
    BD,
    Repo,
    ai,
    clean_error,
    due,
    now,
    stamp,
    telegram,
    validate,
)

QUEUE = "queue.json"
HISTORY = "history.json"


def patch(repo, key, fields):
    def update(queue):
        queue[key].update(fields)
        queue[key]["updated_at"] = stamp()
        return queue

    return repo.change(
        QUEUE,
        {},
        update,
    )


def remember(repo, platform, source_id):
    def update(history):
        if isinstance(history, list):
            history = {
                "facebook": list(history),
                "youtube": list(history),
            }

        history.setdefault(platform, [])

        if source_id not in history[platform]:
            history[platform].append(source_id)

        return history

    return repo.change(
        HISTORY,
        {
            "facebook": [],
            "youtube": [],
        },
        update,
    )


def candidate(repo, channel, platform, config):
    import yt_dlp

    history, _ = repo.read(
        HISTORY,
        {},
    )

    completed = (
        history
        if isinstance(history, list)
        else history.get(platform, [])
    )

    queue, _ = repo.read(
        QUEUE,
        {},
    )

    used = {
        item.get("source_id")
        for item in queue.values()
        if item["platform"] == platform
        and item["state"] not in (
            "failed",
            "expired",
        )
    }

    used.update(completed)

    url = channel.split("?")[0].rstrip("/")

    if "/@" in url and not url.endswith("/videos"):
        url += "/videos"

    options = {
        "quiet": True,
        "extract_flat": True,
        "playlistend": config["source_scan_limit"],
        "socket_timeout": 30,
        "retries": 2,
        "ignoreerrors": True,
        "js_runtimes": {"node": {}},
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        index = downloader.extract_info(
            url,
            download=False,
        )

    if not index:
        raise RuntimeError(
            "Channel information could not be fetched"
        )

    entries = index.get("entries") or [index]

    cutoff = now().date() - dt.timedelta(
        days=config["minimum_video_age_days"]
    )

    for entry in entries:
        if now().hour >= 6:
            raise TimeoutError(
                "Source scan exceeded the night window"
            )

        if (
            not entry
            or entry.get("id") in used
        ):
            continue

        try:
            with yt_dlp.YoutubeDL({
                "quiet": True,
                "socket_timeout": 30,
                "retries": 2,
                "js_runtimes": {"node": {}},
            }) as downloader:
                info = downloader.extract_info(
                    "https://www.youtube.com/watch?v="
                    + entry["id"],
                    False,
                )

            date = info.get("upload_date")

            if date:
                upload_date = dt.datetime.strptime(
                    date,
                    "%Y%m%d",
                ).date()

                if upload_date <= cutoff:
                    if (
                        info.get("is_live")
                        or not info.get("duration")
                    ):
                        continue

                    return info

        except Exception:
            continue

    raise RuntimeError(
        "No unused eligible video found within the scan limit"
    )


def prepare(repo, config):
    from editor import render
    import yt_dlp

    current = now()

    if not (
        dt.time(1)
        <= current.time().replace(tzinfo=None)
        < dt.time(6)
    ):
        telegram(
            "SSK: Editing can run only between "
            "01:00 and 06:00 Bangladesh time."
        )
        return

    if config["paused"]:
        return

    deadline = current.replace(
        hour=6,
        minute=0,
        second=0,
        microsecond=0,
    ).timestamp() - 90

    date = current.date().isoformat()
    release = None

    for platform in ("facebook", "youtube"):
        for slot in range(3):
            config = validate(
                repo.read("config.json")[0]
            )

            if config["paused"]:
                return

            if time.time() >= deadline - 60:
                telegram(
                    "SSK: Night window ended; incomplete "
                    "slots are reported, not force-uploaded."
                )
                return

            key = f"{date}-{platform}-{slot}"

            queue, _ = repo.read(
                QUEUE,
                {},
            )

            if (
                key in queue
                and queue[key]["state"] not in (
                    "failed",
                    "preparing",
                )
            ):
                continue

            try:
                info = candidate(
                    repo,
                    config[platform + "_channels"][slot],
                    platform,
                    config,
                )

                item = {
                    "date": date,
                    "platform": platform,
                    "slot": slot,
                    "source_id": info["id"],
                    "source_url": info["webpage_url"],
                    "source_channel": config[
                        platform + "_channels"
                    ][slot],
                    "state": "preparing",
                    "created_at": stamp(),
                    "updated_at": stamp(),
                }

                repo.change(
                    QUEUE,
                    {},
                    lambda queue: {
                        **queue,
                        key: item,
                    },
                )

                repo.write(
                    "progress.json",
                    {
                        "updated_at": stamp(),
                        "job": key,
                        "stage": "download",
                    },
                )

                telegram(
                    f"SSK: Preparing {platform} "
                    f"slot {slot + 1}: "
                    f'{info.get("title", "Video")}'
                )

                with tempfile.TemporaryDirectory(
                    prefix="ssk_"
                ) as temporary:
                    folder = Path(temporary)

                    output_template = str(
                        folder / "source.%(ext)s"
                    )

                    options = {
                        "format": (
                            "bv*[height<=720]+ba/"
                            "b[height<=720]/best"
                        ),
                        "merge_output_format": "mp4",
                        "outtmpl": output_template,
                        "quiet": True,
                        "noplaylist": True,
                        "retries": 2,
                        "socket_timeout": 30,
                        "continuedl": True,
                        "js_runtimes": {"node": {}},
                    }

                    def check_deadline(_):
                        if time.time() >= deadline:
                            raise TimeoutError(
                                "Download exceeded editing window"
                            )

                    options["progress_hooks"] = [
                        check_deadline
                    ]

                    with yt_dlp.YoutubeDL(
                        options
                    ) as downloader:
                        downloader.extract_info(
                            info["webpage_url"],
                            download=True,
                        )

                    inputs = [
                        file
                        for file in folder.glob("source.*")
                        if file.suffix in (
                            ".mp4",
                            ".mkv",
                            ".webm",
                        )
                    ]

                    if len(inputs) != 1:
                        raise RuntimeError(
                            "Download did not produce "
                            "exactly one video"
                        )

                    repo.write(
                        "progress.json",
                        {
                            "updated_at": stamp(),
                            "job": key,
                            "stage": (
                                "analysis / subtitles / rendering"
                            ),
                        },
                    )

                    video, report, transcript = render(
                        inputs[0],
                        platform,
                        config,
                        folder,
                        deadline,
                    )

                    metadata = {
                        "title": info.get(
                            "title",
                            "SSK DRAMA",
                        )[:100],
                        "description": info.get(
                            "description",
                            "",
                        )[:4500],
                    }

                    try:
                        result = ai(
                            config,
                            (
                                "Create accurate Bengali SEO "
                                "for this drama, without "
                                "invented events or claims. "
                                "Return JSON with title and "
                                "description. Title under "
                                "100 characters, description "
                                "under 4500 characters. "
                                "Include relevant hashtags. "
                                "Original title: "
                                + info.get("title", "")
                                + "\nDescription: "
                                + info.get(
                                    "description",
                                    "",
                                )[:2000]
                                + "\nTranscript:\n"
                                + transcript
                            ),
                            structured=True,
                        )

                        if (
                            not isinstance(
                                result.get("title"),
                                str,
                            )
                            or not isinstance(
                                result.get("description"),
                                str,
                            )
                        ):
                            raise ValueError(
                                "Invalid SEO response"
                            )

                        metadata = {
                            "title": result["title"][:100],
                            "description": result[
                                "description"
                            ][:4500],
                        }

                    except Exception as exc:
                        report["seo_error"] = clean_error(exc)

                    if time.time() >= deadline:
                        raise TimeoutError(
                            "Rendering finished too late "
                            "for night storage"
                        )

                    if release is None:
                        release = repo.release(date)

                    named = folder / (
                        key + ".mp4"
                    )

                    video.rename(named)

                    asset = repo.store(
                        release,
                        named,
                    )

                    patch(
                        repo,
                        key,
                        {
                            "state": "ready",
                            "asset_id": asset["id"],
                            "release_id": release["id"],
                            "size_bytes": asset["size"],
                            "metadata": metadata,
                            "report": report,
                            "error": None,
                        },
                    )

                telegram(
                    f"SSK: {platform} slot {slot + 1} "
                    "ready and stored."
                )

            except Exception as exc:
                error = clean_error(exc)

                queue, _ = repo.read(
                    QUEUE,
                    {},
                )

                if key not in queue:
                    repo.change(
                        QUEUE,
                        {},
                        lambda queue: {
                            **queue,
                            key: {
                                "date": date,
                                "platform": platform,
                                "slot": slot,
                                "state": "failed",
                                "error": error,
                                "updated_at": stamp(),
                            },
                        },
                    )

                else:
                    patch(
                        repo,
                        key,
                        {
                            "state": "failed",
                            "error": error,
                        },
                    )

                telegram(
                    f"SSK: Preparation failed: "
                    f"{platform} slot {slot + 1}\n"
                    + error
                )

    repo.write(
        "progress.json",
        {
            "updated_at": stamp(),
            "stage": "night preparation finished",
        },
    )


def credentials():
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=os.environ[
            "YOUTUBE_REFRESH_TOKEN"
        ],
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=os.environ[
            "GOOGLE_CLIENT_ID"
        ],
        client_secret=os.environ[
            "GOOGLE_CLIENT_SECRET"
        ],
    )


def youtube_client():
    from googleapiclient.discovery import build

    return build(
        "youtube",
        "v3",
        credentials=credentials(),
        cache_discovery=False,
    )


def upload_youtube(file, metadata):
    from googleapiclient.http import MediaFileUpload

    api = youtube_client()

    media = MediaFileUpload(
        str(file),
        mimetype="video/mp4",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )

    request = api.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": metadata["title"][:100],
                "description": metadata["description"],
                "categoryId": "24",
            },
            "status": {
                "privacyStatus": "public"
            },
        },
        media_body=media,
    )

    response = None

    while response is None:
        _, response = request.next_chunk(
            num_retries=2
        )

    if not response.get("id"):
        raise RuntimeError(
            "YouTube did not return a video ID"
        )

    return (
        response["id"],
        "https://www.youtube.com/watch?v="
        + response["id"],
    )


def facebook_call(
    config,
    endpoint,
    data,
    files=None,
):
    response = requests.post(
        "https://graph-video.facebook.com/"
        + config["facebook_graph_version"]
        + "/"
        + endpoint,
        data={
            **data,
            "access_token": os.environ[
                "FB_ACCESS_TOKEN"
            ],
        },
        files=files,
        timeout=(30, 900),
    )

    response.raise_for_status()
    result = response.json()

    if result.get("error"):
        raise RuntimeError(
            json.dumps(result["error"])
        )

    return result


def upload_facebook(file, metadata, config):
    endpoint = os.environ[
        "FB_PAGE_ID"
    ] + "/videos"

    start = facebook_call(
        config,
        endpoint,
        {
            "upload_phase": "start",
            "file_size": file.stat().st_size,
        },
    )

    session = start["upload_session_id"]
    video_id = start["video_id"]

    begin = int(start["start_offset"])
    end = int(start["end_offset"])

    with file.open("rb") as stream:
        while begin < end:
            stream.seek(begin)
            chunk = stream.read(end - begin)

            if len(chunk) != end - begin:
                raise RuntimeError(
                    "Invalid Facebook transfer offset"
                )

            result = facebook_call(
                config,
                endpoint,
                {
                    "upload_phase": "transfer",
                    "upload_session_id": session,
                    "start_offset": begin,
                },
                {
                    "video_file_chunk": (
                        "chunk.mp4",
                        chunk,
                        "application/octet-stream",
                    )
                },
            )

            new_begin = int(
                result["start_offset"]
            )

            new_end = int(
                result["end_offset"]
            )

            if (
                new_begin <= begin
                and new_end != new_begin
            ):
                raise RuntimeError(
                    "Facebook transfer stalled"
                )

            begin = new_begin
            end = new_end

    result = facebook_call(
        config,
        endpoint,
        {
            "upload_phase": "finish",
            "upload_session_id": session,
            "title": metadata["title"],
            "description": metadata["description"],
            "published": "true",
        },
    )

    if result.get("success") is not True:
        raise RuntimeError(
            "Facebook did not confirm upload completion"
        )

    return (
        str(video_id),
        "https://www.facebook.com/" + str(video_id),
    )


def publish(repo, config):
    if config["paused"]:
        return

    queue, _ = repo.read(
        QUEUE,
        {},
    )

    for key, item in sorted(queue.items()):
        if (
            item["state"] != "ready"
            or not due(item, config)
        ):
            continue

        claimed = [False]

        def claim(queue):
            claimed[0] = False

            if queue[key]["state"] == "ready":
                queue[key].update({
                    "state": "uploading",
                    "updated_at": stamp(),
                })

                claimed[0] = True

            return queue

        # Claim persistently before contacting the upload platform.
        repo.change(
            QUEUE,
            {},
            claim,
        )

        if not claimed[0]:
            continue

        try:
            with tempfile.TemporaryDirectory(
                prefix="ssk_upload_"
            ) as temporary:
                file = Path(temporary) / "ready.mp4"

                repo.fetch_asset(
                    item["asset_id"],
                    file,
                )

                if (
                    file.stat().st_size
                    != item["size_bytes"]
                ):
                    raise RuntimeError(
                        "Stored video size validation failed"
                    )

                if item["platform"] == "youtube":
                    video_id, link = upload_youtube(
                        file,
                        item["metadata"],
                    )

                else:
                    video_id, link = upload_facebook(
                        file,
                        item["metadata"],
                        config,
                    )

            patch(
                repo,
                key,
                {
                    "state": "uploaded",
                    "remote_id": video_id,
                    "remote_url": link,
                    "uploaded_at": stamp(),
                    "error": None,
                },
            )

            remember(
                repo,
                item["platform"],
                item["source_id"],
            )

            telegram(
                "SSK: Upload accepted; "
                "platform processing pending.\n"
                + link
            )

        except Exception as exc:
            error = clean_error(exc)

            # Do not repeat an ambiguous upload automatically.
            patch(
                repo,
                key,
                {
                    "state": "review",
                    "error": error,
                },
            )

            telegram(
                "SSK: Upload needs review; "
                "automatic repeat blocked.\n"
                + error
            )


def confirm_processing(repo, config):
    queue, _ = repo.read(
        QUEUE,
        {},
    )

    for key, item in queue.items():
        if item["state"] != "uploaded":
            continue

        try:
            if item["platform"] == "youtube":
                data = youtube_client().videos().list(
                    part="processingDetails,status",
                    id=item["remote_id"],
                ).execute()

                if not data.get("items"):
                    continue

                status = data["items"][0].get(
                    "processingDetails",
                    {},
                ).get("processingStatus")

                done = status == "succeeded"
                failed = status in (
                    "failed",
                    "terminated",
                )

            else:
                response = requests.get(
                    "https://graph.facebook.com/"
                    + config["facebook_graph_version"]
                    + "/"
                    + item["remote_id"],
                    params={
                        "fields": "status",
                        "access_token": os.environ[
                            "FB_ACCESS_TOKEN"
                        ],
                    },
                    timeout=40,
                )

                response.raise_for_status()

                status = response.json().get(
                    "status",
                    {},
                ).get("video_status")

                done = status == "ready"
                failed = status == "error"

            if done:
                patch(
                    repo,
                    key,
                    {
                        "state": "done",
                        "finished_at": stamp(),
                    },
                )

                telegram(
                    "SSK: Platform processing complete.\n"
                    + item["remote_url"]
                )

            elif failed:
                patch(
                    repo,
                    key,
                    {
                        "state": "review",
                        "error": "Platform processing failed",
                    },
                )

            else:
                patch(
                    repo,
                    key,
                    {
                        "processing_status": (
                            status or "pending"
                        )
                    },
                )

        except Exception as exc:
            patch(
                repo,
                key,
                {
                    "processing_check_error": (
                        clean_error(exc)
                    )
                },
            )


def cleanup(repo, config):
    queue, _ = repo.read(
        QUEUE,
        {},
    )

    dates = {
        item["date"]
        for item in queue.values()
    }

    for date in dates:
        items = [
            (key, item)
            for key, item in queue.items()
            if item["date"] == date
        ]

        all_done = (
            len(items) == 6
            and all(
                item["state"] == "done"
                for _, item in items
            )
        )

        expired = (
            now().date()
            - dt.date.fromisoformat(date)
        ).days >= config["storage_retention_days"]

        if not all_done and not expired:
            continue

        for key, item in items:
            if (
                expired
                and item["state"] not in (
                    "done",
                    "expired",
                )
            ):
                patch(
                    repo,
                    key,
                    {
                        "state": "expired",
                        "error": (
                            "Unfinished storage retention expired"
                        ),
                    },
                )

            if (
                item.get("asset_id")
                and not item.get("storage_deleted")
            ):
                repo.delete_asset(
                    item["asset_id"]
                )

                fields = {
                    "storage_deleted": True
                }

                if (
                    expired
                    and item["state"] != "done"
                ):
                    fields.update({
                        "state": "expired",
                        "error": (
                            "Unfinished storage retention expired"
                        ),
                    })

                patch(
                    repo,
                    key,
                    fields,
                )

        releases = {
            item.get("release_id")
            for _, item in items
        } - {None}

        for release_id in releases:
            response = repo.s.delete(
                repo.base
                + f"/releases/{release_id}",
                timeout=40,
            )

            if response.status_code not in (204, 404):
                response.raise_for_status()

    # Remove orphaned temporary draft releases after retention.
    page = 1

    while True:
        releases = repo.api(
            "GET",
            "/releases",
            params={
                "per_page": 100,
                "page": page,
            },
        ).json()

        for release in releases:
            tag = release.get(
                "tag_name",
                "",
            )

            if (
                not tag.startswith("ssk-")
                or not release.get("draft")
            ):
                continue

            try:
                age = (
                    now().date()
                    - dt.date.fromisoformat(tag[4:])
                ).days

            except ValueError:
                continue

            if age >= config["storage_retention_days"]:
                repo.api(
                    "DELETE",
                    "/releases/" + str(release["id"]),
                )

        if len(releases) < 100:
            break

        page += 1

    # Keep recent queue records; preserve the long-term history.
    cutoff = (
        now().date()
        - dt.timedelta(days=14)
    ).isoformat()

    repo.change(
        QUEUE,
        {},
        lambda queue: {
            key: item
            for key, item in queue.items()
            if (
                item["date"] >= cutoff
                or not item.get(
                    "storage_deleted",
                    True,
                )
            )
        },
    )


def main():
    repo = Repo()

    config, _ = repo.read(
        "config.json"
    )

    validate(config)

    mode = os.getenv(
        "APP_MODE",
        "service",
    )

    if mode == "prepare":
        prepare(repo, config)

    elif mode == "upload":
        publish(repo, config)

    else:
        from monitor import service

        service(
            repo,
            config,
            confirm_processing,
            cleanup,
        )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        error = clean_error(exc)
        telegram("SSK workflow error: " + error)
        print(error)
        raise SystemExit(1)
