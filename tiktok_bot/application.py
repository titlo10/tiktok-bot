from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from contextlib import closing
from datetime import datetime
from html import escape
from itertools import islice
from pathlib import Path

import requests

from tiktok_bot.config import (
    COMMENT_FETCH_LIMIT,
    KEEP_DOWNLOADS,
    KNOWN_STREAK_LIMIT,
    MAX_COMMENT_ATTEMPTS,
    MAX_COMMENT_IMAGE_BYTES,
    MAX_COMMENT_IMAGES,
    MAX_COMMENT_MEDIA_ATTEMPTS,
    MAX_RICH_TEXT_LENGTH,
    MAX_VIDEO_BYTES,
    MIN_FREE_DISK_BYTES,
    POLL_ERROR_BACKOFF_MAX_SECONDS,
    RICH_MEDIA_DIR,
    RICH_MEDIA_PUBLIC_BASE_URL,
    RICH_MEDIA_SUFFIXES,
    RICH_MEDIA_TTL_SECONDS,
    SCAN_LIMIT,
    STATE_DB,
    STICKER_MARKER_RE,
    TELEGRAM_API_BASE_URL,
    TELEGRAM_LOCAL_MODE,
    TELEGRAM_UPDATE_LIMIT,
    TELEGRAM_UPDATE_OFFSET_KEY,
    TELEGRAM_UPDATE_TIMEOUT,
    TELEGRAM_UPLOAD_LIMIT,
    TIKTOK_QUERY_VIDEO_ID_RE,
    TIKTOK_SINGLE_URL_RE,
    TIKTOK_VIDEO_ID_RE,
    TIKTOK_WEB_USER_AGENT,
    TOP_COMMENTS_LIMIT,
)

from atp import settings
from atp.models import Video, VideoStatus
from atp.tiktok import download_video, get_user_liked_videos, yt_dlp_request
from tiktok_bot.domain import (
    DeliveredTikTokVideo,
    DeliveryStatus,
    JsonObject,
    PublishedRichMedia,
    RichMediaSource,
    TelegramAPIError,
    TelegramMethod,
    TelegramMessage,
    TelegramUpdate,
    TikTokComment,
)

logger = logging.getLogger("liked_bot")


def normalize_comment_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    ).strip()


def strip_sticker_marker(text: str) -> tuple[str, bool]:
    normalized = normalize_comment_text(text)
    cleaned = STICKER_MARKER_RE.sub("", normalized, count=1).strip()
    if cleaned != normalized:
        return cleaned or "Стикер", True
    return normalized, False


def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(STATE_DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS liked_videos (
            video_id TEXT PRIMARY KEY,
            liked_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT,
            video_message_id INTEGER,
            comments_status TEXT NOT NULL DEFAULT 'pending',
            comments_attempts INTEGER NOT NULL DEFAULT 0,
            comments_last_error TEXT,
            comments_message_id INTEGER,
            comment_media_status TEXT NOT NULL DEFAULT 'pending',
            comment_media_attempts INTEGER NOT NULL DEFAULT 0,
            comment_media_last_error TEXT
        )
        """
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(liked_videos)")}
    comments_feature_is_new = "comments_status" not in columns
    comment_media_feature_is_new = "comment_media_status" not in columns
    migrations = {
        "video_message_id": "INTEGER",
        "comments_status": "TEXT NOT NULL DEFAULT 'pending'",
        "comments_attempts": "INTEGER NOT NULL DEFAULT 0",
        "comments_last_error": "TEXT",
        "comments_message_id": "INTEGER",
        "comment_media_status": "TEXT NOT NULL DEFAULT 'pending'",
        "comment_media_attempts": "INTEGER NOT NULL DEFAULT 0",
        "comment_media_last_error": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            db.execute(f"ALTER TABLE liked_videos ADD COLUMN {column} {definition}")
    if comments_feature_is_new:
        db.execute("UPDATE liked_videos SET comments_status = 'skipped'")
    if comment_media_feature_is_new:
        db.execute("UPDATE liked_videos SET comment_media_status = 'skipped'")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.commit()
    return db


def get_metadata(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return str(row["value"])


def set_metadata(db: sqlite3.Connection, key: str, value: str | int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    db.commit()


def is_initialized(db: sqlite3.Connection) -> bool:
    return get_metadata(db, "initialized") == "1"


def parse_entry(entry: JsonObject) -> tuple[str, int] | None:
    video_id = str(entry.get("id") or "").strip()
    timestamp = entry.get("timestamp")
    if not video_id.isdigit() or timestamp is None:
        return None
    try:
        return video_id, int(timestamp)
    except (TypeError, ValueError):
        return None


def scan_likes(db: sqlite3.Connection) -> list[tuple[str, int]]:
    entries = get_user_liked_videos(settings.TIKTOK_USER)
    result: list[tuple[str, int]] = []
    initialized = is_initialized(db)
    known_streak = 0
    try:
        for entry in islice(entries, SCAN_LIMIT):
            parsed = parse_entry(entry)
            if parsed:
                result.append(parsed)
                if initialized:
                    row = db.execute(
                        "SELECT 1 FROM liked_videos WHERE video_id = ?", (parsed[0],)
                    ).fetchone()
                    known_streak = known_streak + 1 if row else 0
                    if known_streak >= KNOWN_STREAK_LIMIT:
                        break
    finally:
        close_entries = getattr(entries, "close", None)
        if callable(close_entries):
            close_entries()
    return result


def seed_baseline(db: sqlite3.Connection, likes: list[tuple[str, int]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    db.executemany(
        """
        INSERT OR IGNORE INTO liked_videos
            (
                video_id,
                liked_at,
                status,
                first_seen_at,
                comments_status,
                comment_media_status
            )
        VALUES (?, ?, 'baseline', ?, 'skipped', 'skipped')
        """,
        [(video_id, liked_at, now) for video_id, liked_at in likes],
    )
    db.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('initialized', '1')")
    db.commit()
    logger.info("Baseline initialized with %d current liked videos", len(likes))


def register_new_likes(
    db: sqlite3.Connection, likes: list[tuple[str, int]]
) -> list[tuple[str, int]]:
    new_likes: list[tuple[str, int]] = []
    known_streak = 0
    now = datetime.now().isoformat(timespec="seconds")

    for video_id, liked_at in likes:
        row = db.execute(
            "SELECT status FROM liked_videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        if row:
            known_streak += 1
            if known_streak >= KNOWN_STREAK_LIMIT:
                break
            continue

        known_streak = 0
        db.execute(
            """
            INSERT INTO liked_videos
                (video_id, liked_at, status, first_seen_at)
            VALUES (?, ?, 'pending', ?)
            """,
            (video_id, liked_at, now),
        )
        new_likes.append((video_id, liked_at))

    db.commit()
    return list(reversed(new_likes))


def pending_likes(db: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = db.execute(
        """
        SELECT video_id, liked_at
        FROM liked_videos
        WHERE status IN ('pending', 'failed')
        ORDER BY liked_at ASC
        """
    ).fetchall()
    return [(row["video_id"], row["liked_at"]) for row in rows]


def pending_comments(db: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = db.execute(
        """
        SELECT video_id, video_message_id
        FROM liked_videos
        WHERE status = 'sent'
          AND comments_status IN ('pending', 'failed')
          AND comments_attempts < ?
          AND video_message_id IS NOT NULL
        ORDER BY sent_at ASC
        """,
        (MAX_COMMENT_ATTEMPTS,),
    ).fetchall()
    return [(row["video_id"], row["video_message_id"]) for row in rows]


def pending_comment_media(db: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = db.execute(
        """
        SELECT video_id, COALESCE(comments_message_id, video_message_id) AS parent_message_id
        FROM liked_videos
        WHERE comments_status = 'sent'
          AND comment_media_status IN ('pending', 'failed')
          AND comment_media_attempts < ?
          AND (comments_message_id IS NOT NULL OR video_message_id IS NOT NULL)
        ORDER BY sent_at ASC
        """,
        (MAX_COMMENT_MEDIA_ATTEMPTS,),
    ).fetchall()
    return [(row["video_id"], row["parent_message_id"]) for row in rows]


def telegram_call(
    method: TelegramMethod, data: JsonObject, files: JsonObject | None = None
) -> JsonObject:
    url = f"{TELEGRAM_API_BASE_URL}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, data=data, files=files, timeout=180)
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram request {method} failed: {exc.__class__.__name__}") from None
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram returned HTTP {response.status_code}") from exc
    if response.status_code != 200 or not payload.get("ok"):
        description = payload.get("description", f"HTTP {response.status_code}")
        raise TelegramAPIError(description)
    return payload["result"]


def telegram_chat_data() -> dict[str, str | int]:
    return {"chat_id": settings.TELEGRAM_CHAT_ID}


def telegram_get_updates(offset: int | None, timeout: int) -> list[TelegramUpdate]:
    data: dict[str, str | int] = {
        "limit": TELEGRAM_UPDATE_LIMIT,
        "timeout": timeout,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        data["offset"] = offset

    result = telegram_call(TelegramMethod.GET_UPDATES, data)
    if not isinstance(result, list):
        raise RuntimeError("Telegram returned invalid updates payload")
    return result


def delete_telegram_message(chat_id: str | int, message_id: int) -> None:
    try:
        telegram_call(
            TelegramMethod.DELETE_MESSAGE,
            {
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Could not delete Telegram message %s in chat %s: %s",
            message_id,
            chat_id,
            exc.description,
        )


def send_upload_action(chat_id: str | int) -> None:
    try:
        telegram_call(
            TelegramMethod.SEND_CHAT_ACTION,
            {
                "chat_id": chat_id,
                "action": "upload_video",
            },
        )
    except Exception as exc:
        logger.debug("Could not send Telegram upload action: %s", exc)


def validate_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", settings.TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", settings.TELEGRAM_CHAT_ID),
            ("COOKIES_FILE", settings.COOKIES_FILE),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing configuration: {', '.join(missing)}")

    bot = telegram_call(TelegramMethod.GET_ME, {})
    chat = telegram_call(TelegramMethod.GET_CHAT, {"chat_id": settings.TELEGRAM_CHAT_ID})
    logger.info(
        "Telegram access verified for bot @%s and chat %s",
        bot.get("username", "unknown"),
        chat.get("title") or chat.get("username") or chat.get("id"),
    )


def compressed_copy(source: Path) -> Path:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(probe.stdout.strip())
    if duration <= 0:
        raise RuntimeError("Cannot determine video duration")

    target = source.with_suffix(".telegram.mp4")
    target_bits = 46 * 1024 * 1024 * 8
    audio_bitrate = 64_000
    video_bitrate = max(200_000, int(target_bits / duration) - audio_bitrate)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            str(video_bitrate),
            "-maxrate",
            str(video_bitrate),
            "-bufsize",
            str(video_bitrate * 2),
            "-c:a",
            "aac",
            "-b:a",
            str(audio_bitrate),
            "-movflags",
            "+faststart",
            str(target),
        ],
        check=True,
    )
    if target.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
        target.unlink(missing_ok=True)
        raise RuntimeError("Compressed video still exceeds Telegram upload limit")
    return target


def send_video(path: Path, reply_to_message_id: int | None = None) -> int:
    upload_path = path
    temporary = False
    if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
        logger.info("Compressing %s for Telegram", path.name)
        upload_path = compressed_copy(path)
        temporary = True

    try:
        data = {**telegram_chat_data(), "supports_streaming": "true"}
        if reply_to_message_id is not None:
            data["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
        if TELEGRAM_LOCAL_MODE:
            data["video"] = upload_path.resolve().as_uri()
            result = telegram_call(TelegramMethod.SEND_VIDEO, data)
            return int(result["message_id"])
        with upload_path.open("rb") as video_file:
            result = telegram_call(
                TelegramMethod.SEND_VIDEO,
                data,
                {"video": video_file},
            )
        return int(result["message_id"])
    finally:
        if temporary:
            upload_path.unlink(missing_ok=True)


def cleanup_rich_media() -> None:
    if not RICH_MEDIA_DIR or not RICH_MEDIA_DIR.is_dir():
        return

    cutoff = time.time() - RICH_MEDIA_TTL_SECONDS
    for path in RICH_MEDIA_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() not in RICH_MEDIA_SUFFIXES:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:
            logger.warning("Failed to remove expired rich media %s: %s", path, exc)


def publish_rich_bytes(
    content: io.BytesIO,
    video_id: str,
    kind: str,
    index: int,
    suffix: str,
) -> PublishedRichMedia | None:
    if not RICH_MEDIA_DIR or not RICH_MEDIA_PUBLIC_BASE_URL:
        return None

    RICH_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    safe_video_id = (
        "".join(char for char in str(video_id) if char.isalnum() or char in {"-", "_"})[:80]
        or "video"
    )
    filename = f"{safe_video_id}-{timestamp}-{kind}-{index}{suffix}"
    destination = RICH_MEDIA_DIR / filename
    temporary = destination.with_name(f"{destination.name}.tmp")

    content.seek(0)
    with temporary.open("wb") as output:
        shutil.copyfileobj(content, output)
    os.chmod(temporary, 0o644)
    temporary.replace(destination)

    url = f"{RICH_MEDIA_PUBLIC_BASE_URL}/{filename}"
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"Published rich media is missing or empty: {destination}")

    logger.info("Published rich comment image staging file %s", url)
    return PublishedRichMedia(url=url, path=destination)


def rich_image_is_publicly_available(published: PublishedRichMedia) -> bool:
    """Checks that a staged comment image can be fetched through nginx."""
    if not published.path.is_file() or published.path.stat().st_size == 0:
        logger.warning("Published comment image is missing or empty: %s", published.path)
        return False

    response = None
    try:
        response = requests.get(
            published.url,
            headers={"User-Agent": TIKTOK_WEB_USER_AGENT},
            stream=True,
            timeout=20,
        )
        if response.status_code != 200:
            logger.warning(
                "Published comment image is not publicly available (%s): %s",
                response.status_code,
                published.url,
            )
            return False
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            logger.warning(
                "Published comment image has unexpected content type %r: %s",
                response.headers.get("content-type"),
                published.url,
            )
            return False
        if not next(response.iter_content(chunk_size=1), b""):
            logger.warning("Published comment image is empty over HTTP: %s", published.url)
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Could not verify published comment image %s: %s", published.url, exc)
        return False
    finally:
        if response is not None:
            response.close()


def cleanup_download(path: Path) -> None:
    if KEEP_DOWNLOADS:
        return

    try:
        path.unlink(missing_ok=True)
        logger.info("Removed downloaded video %s", path.name)
    except OSError as exc:
        logger.warning("Failed to remove downloaded video %s: %s", path, exc)


def validate_upload_capacity(path: Path) -> None:
    file_size = path.stat().st_size
    if file_size > MAX_VIDEO_BYTES:
        raise RuntimeError(f"Video size {file_size} exceeds configured maximum")
    free_bytes = shutil.disk_usage(path.parent).free
    required_bytes = file_size + MIN_FREE_DISK_BYTES
    if free_bytes < required_bytes:
        raise RuntimeError(
            f"Insufficient disk space: {free_bytes} available, {required_bytes} required"
        )


def validate_download_capacity() -> None:
    downloads_dir = Path(settings.DOWNLOADS_DIR)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(downloads_dir).free
    if free_bytes < MIN_FREE_DISK_BYTES:
        raise RuntimeError(
            f"Insufficient disk space: {free_bytes} available, {MIN_FREE_DISK_BYTES} required"
        )


def is_http_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def extract_single_tiktok_url(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    match = TIKTOK_SINGLE_URL_RE.fullmatch(text)
    if not match:
        return None

    url = match.group("url")
    if not url.startswith(("https://", "http://")):
        url = f"https://{url}"
    return url


def extract_tiktok_video_id_from_url(url: str) -> str | None:
    for pattern in (TIKTOK_VIDEO_ID_RE, TIKTOK_QUERY_VIDEO_ID_RE):
        match = pattern.search(url)
        if match:
            return match.group("video_id")
    return None


def resolve_tiktok_url(url: str) -> str:
    response = requests.get(
        url,
        allow_redirects=True,
        headers={"User-Agent": TIKTOK_WEB_USER_AGENT},
        stream=True,
        timeout=20,
    )
    try:
        return response.url
    finally:
        response.close()


def extract_tiktok_video_id(url: str) -> str:
    video_id = extract_tiktok_video_id_from_url(url)
    if video_id:
        return video_id

    resolved_url = resolve_tiktok_url(url)
    video_id = extract_tiktok_video_id_from_url(resolved_url)
    if video_id:
        if resolved_url != url:
            logger.info("Resolved TikTok URL %s to %s", url, resolved_url)
        return video_id

    raise RuntimeError("Could not extract TikTok video id from URL")


def pick_best_video_url(info: JsonObject) -> str | None:
    candidates = []
    for media_format in info.get("formats") or []:
        url = media_format.get("url")
        if not is_http_url(url):
            continue
        if media_format.get("ext") != "mp4":
            continue
        if media_format.get("vcodec") in (None, "none"):
            continue
        candidates.append(media_format)

    if not candidates and is_http_url(info.get("url")) and info.get("ext") == "mp4":
        return str(info["url"])

    if not candidates:
        return None

    def score(media_format: JsonObject) -> tuple[int, int, int, int]:
        format_id = str(media_format.get("format_id") or "")
        has_audio = media_format.get("acodec") not in (None, "none")
        return (
            1 if format_id == "download" else 0,
            1 if has_audio else 0,
            int(media_format.get("height") or 0),
            int(media_format.get("tbr") or 0),
        )

    return str(max(candidates, key=score)["url"])


def pick_best_audio_url(info: JsonObject) -> str | None:
    candidates = []
    for media_format in info.get("formats") or []:
        url = media_format.get("url")
        if not is_http_url(url):
            continue
        if media_format.get("acodec") in (None, "none"):
            continue
        if media_format.get("vcodec") not in (None, "none"):
            continue
        candidates.append(media_format)

    if not candidates and is_http_url(info.get("url")):
        return str(info["url"])

    if not candidates:
        return None

    return str(candidates[-1]["url"])


def fetch_rich_media_source(video_id: str) -> RichMediaSource:
    info = yt_dlp_request(
        {
            "format": "best",
            "quiet": True,
            "no_warnings": True,
        },
        url=f"https://www.tiktok.com/@/video/{video_id}",
        download=False,
        always_retry=True,
    )
    if not isinstance(info, dict):
        raise RuntimeError("TikTok returned invalid video metadata")

    return RichMediaSource(
        video_url=pick_best_video_url(info),
        audio_url=pick_best_audio_url(info),
        webpage_url=str(info.get("webpage_url") or f"https://www.tiktok.com/@/video/{video_id}"),
        description=str(info.get("description") or info.get("title") or "").strip(),
        uploader=str(info.get("uploader") or "").strip(),
        duration=info.get("duration"),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        comment_count=info.get("comment_count"),
    )


def html_text(value: object, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        text = f"{text[: max(0, limit - 3)].rstrip()}..."
    return escape(text)


def html_paragraph(value: object, limit: int | None = None) -> str:
    return html_text(" ".join(str(value or "").splitlines()), limit)


def format_count(value: object) -> str | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return f"{number:,}".replace(",", " ")


def format_duration(seconds: object) -> str | None:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    minutes, remaining = divmod(total, 60)
    return f"{minutes}:{remaining:02d}"


def comment_details_html(comments: list[TikTokComment]) -> str:
    if not comments:
        return ""

    blocks = [
        "<details>",
        "<summary>Комментарии</summary>",
    ]
    embedded_images = 0
    for comment in comments:
        username = str(comment.get("username") or "").strip()
        if username:
            blocks.append(f"<p><b>@{html_text(username)}</b></p>")

        text = comment.get("text") or ""
        if text:
            blocks.append(f"<p>{html_paragraph(text, 1500)}</p>")
            if comment.get("image_publish_failed"):
                blocks.append("<p>Изображение не удалось встроить.</p>")
        elif comment.get("image_publish_failed"):
            blocks.append("<p>Изображение не удалось встроить.</p>")

        for image_url in comment.get("image_urls") or []:
            if embedded_images >= MAX_COMMENT_IMAGES:
                break
            if not is_http_url(image_url):
                continue
            embedded_images += 1
            escaped_url = escape(str(image_url), quote=True)
            blocks.append(f'<figure><img src="{escaped_url}"></figure>')

    blocks.append("</details>")
    return "\n".join(blocks)


def build_rich_message_html(
    _video_id: str,
    _liked_at: int,
    source: RichMediaSource,
    comments: list[TikTokComment],
) -> str:
    del _video_id, _liked_at, source
    blocks = []

    comments_html = comment_details_html(comments)
    if comments_html:
        blocks.append(comments_html)

    html = "\n".join(blocks)
    if len(html) > MAX_RICH_TEXT_LENGTH:
        html = html[:MAX_RICH_TEXT_LENGTH].rsplit("\n", 1)[0]
    return html


def send_rich_tiktok_message(
    video_id: str,
    liked_at: int,
    source: RichMediaSource,
    comments: list[TikTokComment],
    reply_to_message_id: int,
) -> int:
    html = build_rich_message_html(video_id, liked_at, source, comments)
    result = telegram_call(
        TelegramMethod.SEND_RICH_MESSAGE,
        {
            **telegram_chat_data(),
            "rich_message": json.dumps({"html": html}, ensure_ascii=False),
            "reply_parameters": json.dumps({"message_id": reply_to_message_id}),
        },
    )
    return int(result["message_id"])


def is_rich_media_missing(error: Exception) -> bool:
    return (
        isinstance(error, TelegramAPIError)
        and "RICH_MESSAGE_" in error.description
        and "_NO_MEDIA_FOUND" in error.description
    )


def strip_comment_images(comments: list[TikTokComment]) -> list[TikTokComment]:
    stripped = []
    for comment in comments:
        stripped_comment = dict(comment)
        if stripped_comment.get("image_urls"):
            stripped_comment["image_publish_failed"] = True
        stripped_comment["image_urls"] = []
        stripped.append(stripped_comment)
    return stripped


def rich_media_missing_kind(error: Exception) -> str | None:
    if not isinstance(error, TelegramAPIError):
        return None

    description = error.description
    if "RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND" in description:
        return "photo"
    if "RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND" in description:
        return "video"
    if "RICH_MESSAGE_AUDIO_NO_MEDIA_FOUND" in description:
        return "audio"
    if "RICH_MESSAGE_" in description and "_NO_MEDIA_FOUND" in description:
        return "media"
    return None


def send_rich_tiktok_message_with_fallbacks(
    video_id: str,
    liked_at: int,
    source: RichMediaSource,
    comments: list[TikTokComment],
    reply_to_message_id: int,
) -> tuple[int, bool, bool]:
    del source
    rich_source = RichMediaSource()
    rich_comments = comments
    embedded_video = False
    embedded_comment_images = bool(comment_images(comments))

    while True:
        try:
            message_id = send_rich_tiktok_message(
                video_id,
                liked_at,
                rich_source,
                rich_comments,
                reply_to_message_id,
            )
            return message_id, embedded_video, embedded_comment_images
        except Exception as exc:
            missing_kind = rich_media_missing_kind(exc)
            if not missing_kind:
                raise

            if missing_kind == "photo" and embedded_comment_images:
                logger.warning(
                    "Telegram could not fetch comment media for video %s: %s. "
                    "Retrying RichMessage without embedded comment images.",
                    video_id,
                    exc,
                )
                rich_comments = strip_comment_images(rich_comments)
                embedded_comment_images = False
                continue

            if embedded_comment_images:
                logger.warning(
                    "Telegram could not fetch rich media for video %s: %s. "
                    "Retrying RichMessage without embedded comment images.",
                    video_id,
                    exc,
                )
                rich_comments = strip_comment_images(rich_comments)
                embedded_comment_images = False
                continue

            raise


def collect_media_url_groups(media_items: object) -> list[list[str]]:
    groups = []
    for media in media_items or []:
        if not isinstance(media, dict):
            continue
        candidates = []
        for variant_name in ("origin_url", "crop_url", "display_image", "animated_url"):
            variant = media.get(variant_name) or {}
            if not isinstance(variant, dict):
                continue
            for candidate in variant.get("url_list") or []:
                if not is_http_url(candidate):
                    continue
                if candidate not in candidates:
                    candidates.append(candidate)
        if candidates:
            groups.append(candidates)
    return groups


def collect_sticker_url_groups(item: JsonObject) -> list[list[str]]:
    groups = []

    def walk(value: object, in_sticker_context: bool = False) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key).casefold()
                next_context = in_sticker_context or "sticker" in key_text
                if next_context and key_text == "url_list" and isinstance(nested, list):
                    candidates = []
                    for candidate in nested:
                        if is_http_url(candidate) and candidate not in candidates:
                            candidates.append(candidate)
                    if candidates:
                        groups.append(candidates)
                walk(nested, next_context)
        elif isinstance(value, list):
            for nested in value:
                walk(nested, in_sticker_context)

    walk(item)
    return groups


def fetch_top_comments(video_id: str) -> list[TikTokComment]:
    cookie_jar = http.cookiejar.MozillaCookieJar()
    cookie_jar.load(settings.COOKIES_FILE, ignore_discard=True, ignore_expires=True)

    with requests.Session() as session:
        session.cookies.update(cookie_jar)
        with session.get(
            "https://www.tiktok.com/api/comment/list/",
            params={
                "aid": "1988",
                "aweme_id": video_id,
                "count": str(COMMENT_FETCH_LIMIT),
                "cursor": "0",
            },
            headers={
                "User-Agent": TIKTOK_WEB_USER_AGENT,
                "Referer": f"https://www.tiktok.com/@_/video/{video_id}",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30,
        ) as response:
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("TikTok returned invalid comment data") from exc
    if payload.get("status_code") != 0:
        raise RuntimeError(
            f"TikTok comment API error: {payload.get('status_msg') or 'unknown error'}"
        )

    comments = []
    for item in payload.get("comments") or []:
        text, has_sticker = strip_sticker_marker(str(item.get("text") or ""))
        if has_sticker:
            continue
        image_url_candidates = collect_media_url_groups(item.get("image_list"))
        image_url_candidates.extend(collect_sticker_url_groups(item))
        image_urls = [candidates[0] for candidates in image_url_candidates if candidates]
        if not text and not image_urls:
            continue
        user = item.get("user") or {}
        comments.append(
            {
                "text": text,
                "has_sticker": has_sticker,
                "likes": int(item.get("digg_count") or 0),
                "created_at": int(item.get("create_time") or 0),
                "username": str(user.get("unique_id") or "").strip(),
                "image_urls": image_urls,
                "image_url_candidates": image_url_candidates,
            }
        )

    comments.sort(key=lambda comment: (-comment["likes"], -comment["created_at"]))
    return comments[:TOP_COMMENTS_LIMIT]


def format_top_comments(comments: list[TikTokComment]) -> str:
    blocks = ["Топ-3 комментария:"]
    for index, comment in enumerate(comments, start=1):
        author = f" · @{comment['username']}" if comment["username"] else ""
        text = comment["text"][:1000] or "🖼 Фото"
        blocks.append(f"{index}. ❤️ {comment['likes']}{author}\n{text}")
    return "\n\n".join(blocks)[:4096]


def download_comment_image(url: str, index: int) -> tuple[str, io.BytesIO, str]:
    with requests.get(
        url,
        headers={"User-Agent": TIKTOK_WEB_USER_AGENT},
        stream=True,
        timeout=30,
    ) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("content-length") or 0)
        if content_length > MAX_COMMENT_IMAGE_BYTES:
            raise RuntimeError("TikTok comment image exceeds the 10 MB limit")
        content = io.BytesIO()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            content.write(chunk)
            if content.tell() > MAX_COMMENT_IMAGE_BYTES:
                raise RuntimeError("TikTok comment image exceeds the 10 MB limit")
        content.seek(0)
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
    if content_type != "image/jpeg":
        converted = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "pipe:1",
            ],
            input=content.getvalue(),
            check=True,
            capture_output=True,
        ).stdout
        content.close()
        content = io.BytesIO(converted)
        content_type = "image/jpeg"
        if len(converted) > MAX_COMMENT_IMAGE_BYTES:
            raise RuntimeError("Converted comment image exceeds the 10 MB limit")
        content.seek(0)
    return f"comment-{index}.jpg", content, content_type


def publish_rich_comment_images(
    comments: list[TikTokComment], video_id: str
) -> list[TikTokComment]:
    if not RICH_MEDIA_DIR or not RICH_MEDIA_PUBLIC_BASE_URL:
        return comments

    published_comments = []
    image_index = 0
    for comment in comments:
        published_comment = dict(comment)
        published_urls = []
        image_url_groups = comment.get("image_url_candidates") or [
            [url] for url in comment.get("image_urls") or []
        ]
        for candidates in image_url_groups:
            if image_index >= MAX_COMMENT_IMAGES:
                break

            image_index += 1
            last_error = None
            for url in candidates:
                if not is_http_url(url):
                    continue
                try:
                    _, content, _ = download_comment_image(url, image_index)
                    try:
                        published = publish_rich_bytes(
                            content,
                            video_id,
                            "comment",
                            image_index,
                            ".jpg",
                        )
                    finally:
                        content.close()
                except Exception as exc:
                    last_error = exc
                    continue

                if published:
                    if rich_image_is_publicly_available(published):
                        published_urls.append(published.url)
                        break
                    try:
                        published.path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "Failed to remove unpublished comment image %s: %s",
                            published.path,
                            exc,
                        )
            else:
                if last_error is not None:
                    logger.warning(
                        "Failed to publish comment image %d for video %s: %s",
                        image_index,
                        video_id,
                        last_error,
                    )

        published_comment["image_urls"] = published_urls
        if image_url_groups and not published_urls:
            published_comment["image_publish_failed"] = True
        published_comments.append(published_comment)

    return published_comments


def comment_images(comments: list[TikTokComment]) -> list[tuple[int, str]]:
    images = []
    for comment_index, comment in enumerate(comments, start=1):
        for url in comment.get("image_urls") or []:
            images.append((comment_index, url))
            if len(images) >= MAX_COMMENT_IMAGES:
                return images
    return images


def send_comment_media(comments_message_id: int, images: list[tuple[int, str]]) -> list[int]:
    downloaded = []
    try:
        for index, (comment_index, url) in enumerate(images, start=1):
            downloaded.append((comment_index, download_comment_image(url, index)))
        reply = json.dumps({"message_id": comments_message_id})
        if len(downloaded) == 1:
            comment_index, (filename, content, content_type) = downloaded[0]
            result = telegram_call(
                TelegramMethod.SEND_PHOTO,
                {
                    **telegram_chat_data(),
                    "caption": f"Медиа из комментария №{comment_index}",
                    "reply_parameters": reply,
                },
                {"photo": (filename, content, content_type)},
            )
            return [int(result["message_id"])]

        files = {}
        media = []
        for index, (comment_index, file_info) in enumerate(downloaded):
            filename, content, content_type = file_info
            attachment = f"photo{index}"
            files[attachment] = (filename, content, content_type)
            media.append(
                {
                    "type": "photo",
                    "media": f"attach://{attachment}",
                    "caption": f"Комментарий №{comment_index}",
                }
            )
        result = telegram_call(
            TelegramMethod.SEND_MEDIA_GROUP,
            {
                **telegram_chat_data(),
                "media": json.dumps(media),
                "reply_parameters": reply,
            },
            files,
        )
        return [int(message["message_id"]) for message in result]
    finally:
        for _, (_, content, _) in downloaded:
            content.close()


def process_comment_media(
    db: sqlite3.Connection,
    video_id: str,
    comments_message_id: int,
    comments: list[TikTokComment] | None = None,
) -> None:
    try:
        comments = comments if comments is not None else fetch_top_comments(video_id)
        images = comment_images(comments)
        if images:
            message_ids = send_comment_media(comments_message_id, images)
            logger.info(
                "Sent %d comment images for video %s as Telegram messages %s",
                len(images),
                video_id,
                message_ids,
            )
            media_status = "sent"
        else:
            logger.info("No comment images found for video %s", video_id)
            media_status = "empty"

        db.execute(
            """
            UPDATE liked_videos
            SET comment_media_status = ?, comment_media_last_error = NULL
            WHERE video_id = ?
            """,
            (media_status, video_id),
        )
        db.commit()
    except Exception as exc:
        db.execute(
            """
            UPDATE liked_videos
            SET comment_media_status = 'failed',
                comment_media_attempts = comment_media_attempts + 1,
                comment_media_last_error = ?
            WHERE video_id = ?
            """,
            (str(exc)[:1000], video_id),
        )
        db.commit()
        logger.exception("Failed to process comment media for video %s", video_id)


def process_comments(db: sqlite3.Connection, video_id: str, video_message_id: int) -> None:
    logger.info("Loading top comments for video %s", video_id)
    try:
        comments = fetch_top_comments(video_id)
        if comments:
            result = telegram_call(
                TelegramMethod.SEND_MESSAGE,
                {
                    **telegram_chat_data(),
                    "text": format_top_comments(comments),
                    "reply_parameters": json.dumps({"message_id": video_message_id}),
                },
            )
            logger.info(
                "Top comments for video %s sent as Telegram message %s",
                video_id,
                result["message_id"],
            )
            comments_status = "sent"
            comments_message_id = int(result["message_id"])
            media_status = "pending" if comment_images(comments) else "empty"
        else:
            logger.info("No comments found for video %s", video_id)
            comments_status = "empty"
            comments_message_id = None
            media_status = "empty"

        db.execute(
            """
            UPDATE liked_videos
            SET comments_status = ?,
                comments_message_id = ?,
                comments_last_error = NULL,
                comment_media_status = ?,
                comment_media_attempts = 0,
                comment_media_last_error = NULL
            WHERE video_id = ?
            """,
            (comments_status, comments_message_id, media_status, video_id),
        )
        db.commit()
        if comments_message_id and media_status == "pending":
            process_comment_media(db, video_id, comments_message_id, comments)
    except Exception as exc:
        db.execute(
            """
            UPDATE liked_videos
            SET comments_status = 'failed',
                comments_attempts = comments_attempts + 1,
                comments_last_error = ?
            WHERE video_id = ?
            """,
            (str(exc)[:1000], video_id),
        )
        db.commit()
        logger.exception("Failed to process comments for video %s", video_id)


def mark_failed(db: sqlite3.Connection, video_id: str, error: Exception) -> None:
    db.execute(
        """
        UPDATE liked_videos
        SET status = 'failed', attempts = attempts + 1, last_error = ?
        WHERE video_id = ?
        """,
        (str(error)[:1000], video_id),
    )
    db.commit()


def deliver_tiktok_video(video_id: str, liked_at: int) -> DeliveredTikTokVideo:
    validate_download_capacity()
    video = Video(
        id=video_id,
        date=datetime.fromtimestamp(liked_at),
        status=VideoStatus.NEW,
        liked=True,
    )
    result = download_video(video)
    if result is None:
        raise RuntimeError("TikTok download returned no result")
    if result.deleted_reason:
        raise RuntimeError(result.deleted_reason)

    path = Path(settings.DOWNLOADS_DIR) / f"{video_id}.mp4"
    if not path.is_file():
        raise RuntimeError(f"Downloaded file not found: {path.name}")
    try:
        validate_upload_capacity(path)
        video_message_id = send_video(path)
        logger.info(
            "Video %s uploaded directly to Telegram as message %s",
            video_id,
            video_message_id,
        )
        comments_status = DeliveryStatus.EMPTY
        media_status = DeliveryStatus.EMPTY
        try:
            logger.info("Loading top comments for video %s", video_id)
            comments = fetch_top_comments(video_id)
            original_comment_images = comment_images(comments)
            rich_comments = publish_rich_comment_images(comments, video_id)
            embedded_comment_images = False
            if comments:
                _, _, embedded_comment_images = send_rich_tiktok_message_with_fallbacks(
                    video_id,
                    liked_at,
                    RichMediaSource(),
                    rich_comments,
                    video_message_id,
                )
                comments_status = DeliveryStatus.SENT
            if original_comment_images:
                media_status = (
                    DeliveryStatus.SENT if embedded_comment_images else DeliveryStatus.FAILED
                )
        except Exception:
            comments_status = DeliveryStatus.FAILED
            media_status = DeliveryStatus.FAILED
            logger.exception("Video %s was sent but comment delivery failed", video_id)
    finally:
        cleanup_download(path)
    return DeliveredTikTokVideo(
        message_id=video_message_id,
        comments_status=str(comments_status),
        comment_media_status=str(media_status),
    )


def process_video(db: sqlite3.Connection, video_id: str, liked_at: int) -> None:
    logger.info("Processing liked video %s", video_id)
    try:
        delivery = deliver_tiktok_video(video_id, liked_at)
        db.execute(
            """
            UPDATE liked_videos
            SET status = 'sent',
                sent_at = ?,
                last_error = NULL,
                video_message_id = ?,
                comments_status = ?,
                comments_attempts = 0,
                comments_last_error = NULL,
                comments_message_id = NULL,
                comment_media_status = ?,
                comment_media_attempts = 0,
                comment_media_last_error = NULL
            WHERE video_id = ?
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                delivery.message_id,
                delivery.comments_status,
                delivery.comment_media_status,
                video_id,
            ),
        )
        db.commit()
        logger.info(
            "Video %s sent as Telegram rich message %s",
            video_id,
            delivery.message_id,
        )
    except Exception as exc:
        mark_failed(db, video_id, exc)
        logger.exception("Failed to process video %s", video_id)


def configured_chat_matches(chat_id: object) -> bool:
    return str(chat_id) == str(settings.TELEGRAM_CHAT_ID)


def initialize_telegram_update_offset(db: sqlite3.Connection) -> int:
    raw_offset = get_metadata(db, TELEGRAM_UPDATE_OFFSET_KEY)
    if raw_offset is not None:
        try:
            return int(raw_offset)
        except ValueError:
            logger.warning("Ignoring invalid Telegram update offset value %r", raw_offset)

    updates = telegram_get_updates(offset=None, timeout=0)
    offset = max((int(update["update_id"]) + 1 for update in updates), default=0)
    set_metadata(db, TELEGRAM_UPDATE_OFFSET_KEY, offset)
    logger.info("Initialized Telegram update offset at %s", offset)
    return offset


def handle_tiktok_link_message(message: TelegramMessage) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not configured_chat_matches(chat_id):
        return

    text = message.get("text") or message.get("caption")
    url = extract_single_tiktok_url(text)
    if not url:
        return

    message_id = int(message["message_id"])
    try:
        video_id = extract_tiktok_video_id(url)
    except Exception:
        logger.exception("Failed to parse TikTok link from Telegram message %s", message_id)
        return

    send_upload_action(chat_id)

    liked_at = int(message.get("date") or time.time())
    logger.info(
        "Processing TikTok link from Telegram message %s as video %s",
        message_id,
        video_id,
    )
    delivery = deliver_tiktok_video(video_id, liked_at)
    delete_telegram_message(chat_id, message_id)
    logger.info(
        "TikTok link video %s sent to Telegram as video message %s",
        video_id,
        delivery.message_id,
    )


def process_telegram_updates(db: sqlite3.Connection, timeout: int) -> None:
    offset = initialize_telegram_update_offset(db)
    updates = telegram_get_updates(offset=offset, timeout=timeout)
    for update in updates:
        update_id = int(update["update_id"])
        message = update.get("message")
        if isinstance(message, dict):
            handle_tiktok_link_message(message)
        set_metadata(db, TELEGRAM_UPDATE_OFFSET_KEY, update_id + 1)


def run_cycle(db: sqlite3.Connection) -> None:
    del db
    cleanup_rich_media()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process TikTok links sent to Telegram")
    parser.add_argument("--once", action="store_true", help="Poll Telegram once and exit")
    parser.add_argument(
        "--check-config", action="store_true", help="Validate Telegram and configuration"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    validate_configuration()
    if args.check_config:
        return

    with closing(connect_db()) as db:
        error_backoff_seconds = 1
        while True:
            try:
                process_telegram_updates(db, timeout=TELEGRAM_UPDATE_TIMEOUT)
                error_backoff_seconds = 1
            except Exception:
                logger.exception("Telegram update polling failed")
                time.sleep(error_backoff_seconds)
                error_backoff_seconds = min(
                    error_backoff_seconds * 2, POLL_ERROR_BACKOFF_MAX_SECONDS
                )
            run_cycle(db)
            if args.once:
                return


if __name__ == "__main__":
    main()
