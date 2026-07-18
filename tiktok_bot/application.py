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
from pathlib import Path

import requests

from tiktok_bot.config import (
    COMMENT_ANIMATION_MAX_DIMENSION,
    COMMENT_FETCH_LIMIT,
    CONVERTAPI_TIMEOUT_SECONDS,
    CONVERTAPI_TOKEN,
    CONVERTAPI_WEBP_TO_GIF_URL,
    EXTERNAL_MEDIA_TTL,
    EXTERNAL_MEDIA_UPLOAD_URL,
    INLINE_CACHE_TIME_SECONDS,
    INLINE_RESULT_ID_MAX_LENGTH,
    KEEP_DOWNLOADS,
    MAX_COMMENT_IMAGE_BYTES,
    MAX_COMMENT_IMAGES,
    MAX_RICH_TEXT_LENGTH,
    MAX_VIDEO_BYTES,
    MIN_FREE_DISK_BYTES,
    POLL_ERROR_BACKOFF_MAX_SECONDS,
    STATE_DB,
    STICKER_MARKER_RE,
    TELEGRAM_API_BASE_URL,
    TELEGRAM_UPDATE_LIMIT,
    TELEGRAM_UPDATE_OFFSET_KEY,
    TELEGRAM_UPDATE_TIMEOUT,
    TELEGRAM_UPLOAD_LIMIT,
    TIKTOK_QUERY_VIDEO_ID_RE,
    TIKTOK_SINGLE_URL_RE,
    TIKTOK_URL_SEARCH_RE,
    TIKTOK_VIDEO_ID_RE,
    TIKTOK_WEB_USER_AGENT,
    TOP_COMMENTS_LIMIT,
)

from atp import settings
from atp.models import Video, VideoStatus
from atp.tiktok import download_video
from tiktok_bot.domain import (
    CachedInlineVideo,
    CommentAnimationSourceFormat,
    DownloadedCommentMedia,
    JsonObject,
    RichCommentMedia,
    RichCommentMediaKind,
    TelegramAPIError,
    TelegramInlineQuery,
    TelegramMessage,
    TelegramMethod,
    TelegramUpdate,
    TikTokComment,
)

logger = logging.getLogger("liked_bot")
HEIF_CONTENT_TYPES = {"image/heic", "image/heif"}
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevm", b"mif1", b"msf1"}
BOT_INFO: JsonObject | None = None


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
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS inline_video_cache (
            video_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            cached_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS rich_replies (
            chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            rich_message_id INTEGER,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
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


def get_cached_inline_video(db: sqlite3.Connection, video_id: str) -> CachedInlineVideo | None:
    row = db.execute(
        """
        SELECT video_id, file_id, caption
        FROM inline_video_cache
        WHERE video_id = ?
        """,
        (video_id,),
    ).fetchone()
    if not row:
        return None
    return CachedInlineVideo(
        video_id=str(row["video_id"]),
        file_id=str(row["file_id"]),
        caption=str(row["caption"] or ""),
    )


def store_cached_inline_video(db: sqlite3.Connection, cached: CachedInlineVideo) -> None:
    db.execute(
        """
        INSERT OR REPLACE INTO inline_video_cache (video_id, file_id, caption, cached_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            cached.video_id,
            cached.file_id,
            cached.caption,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()


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
        "allowed_updates": json.dumps(["inline_query", "message", "channel_post"]),
    }
    if offset is not None:
        data["offset"] = offset

    result = telegram_call(TelegramMethod.GET_UPDATES, data)
    if not isinstance(result, list):
        raise RuntimeError("Telegram returned invalid updates payload")
    return result


def our_bot_id() -> int | None:
    if not BOT_INFO:
        return None
    try:
        return int(BOT_INFO["id"])
    except (KeyError, TypeError, ValueError):
        return None


def message_is_via_our_bot(message: TelegramMessage) -> bool:
    via_bot = message.get("via_bot") or {}
    bot_id = our_bot_id()
    if bot_id is not None and via_bot.get("id") is not None:
        try:
            return int(via_bot["id"]) == bot_id
        except (TypeError, ValueError):
            pass
    bot_username = str((BOT_INFO or {}).get("username") or "").casefold()
    via_username = str(via_bot.get("username") or "").casefold()
    return bool(bot_username and via_username and bot_username == via_username)


def rich_reply_already_sent(db: sqlite3.Connection, chat_id: str | int, message_id: int) -> bool:
    row = db.execute(
        """
        SELECT 1 FROM rich_replies
        WHERE chat_id = ? AND message_id = ? AND status = 'sent'
        """,
        (str(chat_id), int(message_id)),
    ).fetchone()
    return row is not None


def mark_rich_reply(
    db: sqlite3.Connection,
    chat_id: str | int,
    message_id: int,
    video_id: str,
    status: str,
    rich_message_id: int | None = None,
) -> None:
    db.execute(
        """
        INSERT OR REPLACE INTO rich_replies
            (chat_id, message_id, video_id, rich_message_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(chat_id),
            int(message_id),
            video_id,
            rich_message_id,
            status,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()


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


def validate_configuration() -> None:
    global BOT_INFO

    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", settings.TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", settings.TELEGRAM_CHAT_ID),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing configuration: {', '.join(missing)}")

    if settings.COOKIES_FILE:
        logger.info("TikTok cookies loaded from %s", settings.COOKIES_FILE)
    else:
        logger.info(
            "TikTok cookies not configured — public video download still works; "
            "comments and some restricted videos may fail"
        )

    BOT_INFO = telegram_call(TelegramMethod.GET_ME, {})
    can_join_groups = BOT_INFO.get("can_join_groups")
    supports_inline = BOT_INFO.get("supports_inline_queries")
    logger.info(
        "Telegram bot verified: @%s (inline=%s, groups=%s)",
        BOT_INFO.get("username", "unknown"),
        supports_inline,
        can_join_groups,
    )
    if supports_inline is False:
        logger.error(
            "Inline mode is OFF for @%s. Enable it in BotFather (/setinline), "
            "otherwise @bot queries will not work.",
            BOT_INFO.get("username", "unknown"),
        )
    try:
        chat = telegram_call(TelegramMethod.GET_CHAT, {"chat_id": settings.TELEGRAM_CHAT_ID})
        logger.info(
            "Storage chat verified: %s",
            chat.get("title") or chat.get("username") or chat.get("id"),
        )
    except TelegramAPIError as exc:
        logger.error(
            "Storage chat %s is not accessible (%s). Add the bot to that chat "
            "(admin in channels) so it can cache file_id for inline results.",
            settings.TELEGRAM_CHAT_ID,
            exc.description,
        )
    logger.info(
        "Hybrid mode: inline video from the user, then RichMessage comments "
        "as a bot reply in the same chat (bot must see group messages: admin "
        "or BotFather /setprivacy Disable)"
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
    """Accepts a bare TikTok URL (optionally with surrounding whitespace)."""
    if not isinstance(text, str):
        return None
    match = TIKTOK_SINGLE_URL_RE.fullmatch(text)
    if not match:
        return None

    url = match.group("url")
    if not url.startswith(("https://", "http://")):
        url = f"https://{url}"
    return url


def extract_tiktok_url(text: object) -> str | None:
    """Finds a TikTok URL inside free-form inline query text."""
    bare = extract_single_tiktok_url(text)
    if bare:
        return bare
    if not isinstance(text, str):
        return None
    match = TIKTOK_URL_SEARCH_RE.search(text)
    if not match:
        return None
    url = match.group(0)
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
    seen_groups: set[tuple[str, ...]] = set()

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
                    candidate_group = tuple(candidates)
                    if candidate_group and candidate_group not in seen_groups:
                        groups.append(candidates)
                        seen_groups.add(candidate_group)
                walk(nested, next_context)
        elif isinstance(value, list):
            for nested in value:
                walk(nested, in_sticker_context)

    walk(item)
    return groups


def fetch_top_comments(video_id: str) -> list[TikTokComment]:
    with requests.Session() as session:
        if settings.COOKIES_FILE:
            cookie_jar = http.cookiejar.MozillaCookieJar()
            cookie_jar.load(settings.COOKIES_FILE, ignore_discard=True, ignore_expires=True)
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
        image_url_candidates = collect_media_url_groups(item.get("image_list"))
        image_url_candidates.extend(collect_sticker_url_groups(item))
        image_urls = [candidates[0] for candidates in image_url_candidates if candidates]
        if has_sticker and not image_urls:
            continue
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


def ensure_comment_media_size(data: bytes) -> bytes:
    if len(data) > MAX_COMMENT_IMAGE_BYTES:
        raise RuntimeError("Converted comment media exceeds the size limit")
    return data


def convert_comment_media_to_jpeg(data: bytes) -> bytes:
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
        input=data,
        check=True,
        capture_output=True,
    ).stdout
    return ensure_comment_media_size(converted)


def convert_webp_animation_remotely(data: bytes) -> bytes:
    response = requests.post(
        CONVERTAPI_WEBP_TO_GIF_URL,
        headers={
            "Authorization": f"Bearer {CONVERTAPI_TOKEN}",
            "Accept": "application/octet-stream",
        },
        data={
            "StoreFile": "false",
            "Timeout": str(CONVERTAPI_TIMEOUT_SECONDS),
            "ImageWidth": str(COMMENT_ANIMATION_MAX_DIMENSION),
            "ScaleImage": "true",
            "ScaleProportions": "true",
        },
        files={"Files[0]": ("comment.webp", data, "image/webp")},
        timeout=CONVERTAPI_TIMEOUT_SECONDS + 10,
    )
    response.raise_for_status()
    converted = response.content
    if not converted.startswith((b"GIF87a", b"GIF89a")):
        raise RuntimeError("ConvertAPI returned invalid GIF data")
    return ensure_comment_media_size(converted)


def convert_comment_animation(data: bytes, source_format: CommentAnimationSourceFormat) -> bytes:
    if source_format == CommentAnimationSourceFormat.WEBP and CONVERTAPI_TOKEN:
        try:
            return convert_webp_animation_remotely(data)
        except (requests.RequestException, RuntimeError) as exc:
            logger.warning("Remote WebP conversion failed, using local fallback: %s", exc)

    converted = subprocess.run(
        [
            "convert",
            f"{source_format}:-",
            "-coalesce",
            "-resize",
            f"{COMMENT_ANIMATION_MAX_DIMENSION}x{COMMENT_ANIMATION_MAX_DIMENSION}>",
            "-layers",
            "Optimize",
            "gif:-",
        ],
        input=data,
        check=True,
        capture_output=True,
    ).stdout
    return ensure_comment_media_size(converted)


def classify_comment_media(data: bytes, content_type: str) -> DownloadedCommentMedia:
    normalized_type = content_type.casefold()
    if data.startswith((b"GIF87a", b"GIF89a")) or normalized_type == "image/gif":
        return DownloadedCommentMedia(
            content_type="image/gif",
            data=data,
            kind=RichCommentMediaKind.ANIMATION,
            suffix=".gif",
        )
    if normalized_type in HEIF_CONTENT_TYPES or (
        len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS
    ):
        converted = convert_comment_media_to_jpeg(data)
        return DownloadedCommentMedia(
            content_type="image/jpeg",
            data=converted,
            kind=RichCommentMediaKind.PHOTO,
            suffix=".jpg",
        )
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return DownloadedCommentMedia(
            content_type="video/mp4",
            data=data,
            kind=RichCommentMediaKind.ANIMATION,
            suffix=".mp4",
        )
    if data.startswith(b"\xff\xd8\xff"):
        return DownloadedCommentMedia(
            content_type="image/jpeg",
            data=data,
            kind=RichCommentMediaKind.PHOTO,
            suffix=".jpg",
        )
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if b"acTL" in data:
            converted = convert_comment_animation(data, CommentAnimationSourceFormat.PNG)
            return DownloadedCommentMedia(
                content_type="image/gif",
                data=converted,
                kind=RichCommentMediaKind.ANIMATION,
                suffix=".gif",
            )
        return DownloadedCommentMedia(
            content_type="image/png",
            data=data,
            kind=RichCommentMediaKind.PHOTO,
            suffix=".png",
        )
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and b"ANIM" in data:
        converted = convert_comment_animation(data, CommentAnimationSourceFormat.WEBP)
        return DownloadedCommentMedia(
            content_type="image/gif",
            data=converted,
            kind=RichCommentMediaKind.ANIMATION,
            suffix=".gif",
        )
    converted = convert_comment_media_to_jpeg(data)
    return DownloadedCommentMedia(
        content_type="image/jpeg",
        data=converted,
        kind=RichCommentMediaKind.PHOTO,
        suffix=".jpg",
    )


def download_comment_media_bytes(url: str) -> tuple[bytes, str]:
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
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
    return content.getvalue(), content_type


def upload_media_to_external_host(data: bytes, filename: str, content_type: str) -> str:
    """Uploads bytes to a temporary third-party host (litterbox by default).

    RichMessage / caption links need a public HTTPS URL. Telegram file links require the
    bot token, so we deliberately avoid self-hosting and Telegram CDN URLs here.
    """
    response = requests.post(
        EXTERNAL_MEDIA_UPLOAD_URL,
        data={
            "reqtype": "fileupload",
            "time": EXTERNAL_MEDIA_TTL,
        },
        files={"fileToUpload": (filename, data, content_type)},
        timeout=60,
    )
    response.raise_for_status()
    url = response.text.strip()
    if not is_http_url(url):
        raise RuntimeError(f"External media host returned invalid URL: {url[:200]!r}")
    return url


def enrich_comments_with_external_media(comments: list[TikTokComment]) -> list[TikTokComment]:
    """Rehosts comment images/GIFs externally and attaches them as rich_media."""
    enriched: list[TikTokComment] = []
    published_by_url: dict[str, RichCommentMedia] = {}
    embedded = 0

    for comment in comments:
        published_comment = dict(comment)
        published_media: list[RichCommentMedia] = []
        groups = comment.get("image_url_candidates") or [
            [url] for url in comment.get("image_urls") or []
        ]
        had_candidates = bool(groups)
        for candidates in groups:
            if embedded >= MAX_COMMENT_IMAGES:
                break
            last_error: Exception | None = None
            for source_url in candidates:
                if not is_http_url(source_url):
                    continue
                if cached := published_by_url.get(source_url):
                    published_media.append(cached)
                    embedded += 1
                    break
                try:
                    data, content_type = download_comment_media_bytes(source_url)
                    media = classify_comment_media(data, content_type)
                    public_url = upload_media_to_external_host(
                        media.data,
                        f"comment{media.suffix}",
                        media.content_type,
                    )
                    rich: RichCommentMedia = {"kind": media.kind, "url": public_url}
                    published_by_url[source_url] = rich
                    published_media.append(rich)
                    embedded += 1
                    break
                except Exception as exc:
                    last_error = exc
                    continue
            else:
                if last_error is not None:
                    logger.warning(
                        "Failed to rehost comment media for @%s: %s",
                        comment.get("username") or "anon",
                        last_error,
                    )

        published_comment["rich_media"] = published_media
        published_comment["image_urls"] = [
            media["url"]
            for media in published_media
            if media["kind"] == RichCommentMediaKind.PHOTO
        ]
        if had_candidates and not published_media:
            published_comment["image_publish_failed"] = True
        enriched.append(published_comment)
    return enriched


def publish_comment_media_externally(comments: list[TikTokComment]) -> list[str]:
    enriched = enrich_comments_with_external_media(comments)
    urls: list[str] = []
    for comment in enriched:
        for media in comment.get("rich_media") or []:
            urls.append(media["url"])
    return urls


def format_inline_caption(video_id: str) -> str:
    del video_id
    return ""


def html_text(value: object, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        text = f"{text[: max(0, limit - 3)].rstrip()}..."
    return escape(text)


def html_paragraph(value: object, limit: int | None = None) -> str:
    return html_text(" ".join(str(value or "").splitlines()), limit)


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

        rich_media = comment.get("rich_media") or [
            {"kind": RichCommentMediaKind.PHOTO, "url": url}
            for url in comment.get("image_urls") or []
        ]
        for media in rich_media:
            if embedded_images >= MAX_COMMENT_IMAGES:
                break
            media_url = media["url"]
            if not is_http_url(media_url):
                continue
            embedded_images += 1
            escaped_url = escape(media_url, quote=True)
            tag = "video" if media["kind"] == RichCommentMediaKind.ANIMATION else "img"
            blocks.append(f'<figure><{tag} src="{escaped_url}"></{tag}></figure>')

    blocks.append("</details>")
    return "\n".join(blocks)


def build_rich_message_html(comments: list[TikTokComment]) -> str:
    html = comment_details_html(comments)
    if len(html) > MAX_RICH_TEXT_LENGTH:
        html = html[:MAX_RICH_TEXT_LENGTH].rsplit("\n", 1)[0]
    return html


def send_rich_comments(
    chat_id: str | int,
    reply_to_message_id: int,
    comments: list[TikTokComment],
) -> int:
    html = build_rich_message_html(comments)
    if not html.strip():
        raise RuntimeError("No comment HTML to send")
    result = telegram_call(
        TelegramMethod.SEND_RICH_MESSAGE,
        {
            "chat_id": chat_id,
            "rich_message": json.dumps({"html": html}, ensure_ascii=False),
            "reply_parameters": json.dumps({"message_id": reply_to_message_id}),
        },
    )
    return int(result["message_id"])


def strip_comment_images(comments: list[TikTokComment]) -> list[TikTokComment]:
    stripped = []
    for comment in comments:
        stripped_comment = dict(comment)
        if stripped_comment.get("image_urls") or stripped_comment.get("rich_media"):
            stripped_comment["image_publish_failed"] = True
        stripped_comment["image_urls"] = []
        stripped_comment["rich_media"] = []
        stripped.append(stripped_comment)
    return stripped


def strip_comment_media_kind(
    comments: list[TikTokComment], kind: RichCommentMediaKind
) -> list[TikTokComment]:
    stripped: list[TikTokComment] = []
    for comment in comments:
        stripped_comment = dict(comment)
        remaining_media = [
            media for media in comment.get("rich_media") or [] if media["kind"] != kind
        ]
        stripped_comment["rich_media"] = remaining_media
        stripped_comment["image_urls"] = [
            media["url"] for media in remaining_media if media["kind"] == RichCommentMediaKind.PHOTO
        ]
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
    if "RICH_MESSAGE_VIDEO_INVALID" in description:
        return "animation"
    if "RICH_MESSAGE_ANIMATION_INVALID" in description:
        return "animation"
    if "RICH_MESSAGE_PHOTO_INVALID" in description:
        return "photo"
    if "RICH_MESSAGE_" in description and "_NO_MEDIA_FOUND" in description:
        return "media"
    return None


def send_plain_comments(
    chat_id: str | int,
    reply_to_message_id: int,
    comments: list[TikTokComment],
) -> int:
    blocks = ["Топ-комментарии:"]
    for index, comment in enumerate(comments, start=1):
        author = f" · @{comment['username']}" if comment.get("username") else ""
        text = (comment.get("text") or "")[:1000] or "🖼 Фото"
        likes = comment.get("likes") or 0
        blocks.append(f"{index}. ❤️ {likes}{author}\n{text}")
    text = "\n\n".join(blocks)[:4096]
    result = telegram_call(
        TelegramMethod.SEND_MESSAGE,
        {
            "chat_id": chat_id,
            "text": text,
            "reply_parameters": json.dumps({"message_id": reply_to_message_id}),
        },
    )
    return int(result["message_id"])


def send_rich_comments_with_fallbacks(
    chat_id: str | int,
    reply_to_message_id: int,
    comments: list[TikTokComment],
) -> int:
    rich_comments = comments
    embedded_comment_images = any(comment.get("rich_media") for comment in comments)

    while True:
        try:
            return send_rich_comments(chat_id, reply_to_message_id, rich_comments)
        except Exception as exc:
            missing_kind = rich_media_missing_kind(exc)
            if not missing_kind:
                raise

            if missing_kind == "animation" and any(
                media["kind"] == RichCommentMediaKind.ANIMATION
                for comment in rich_comments
                for media in comment.get("rich_media") or []
            ):
                rich_comments = strip_comment_media_kind(
                    rich_comments, RichCommentMediaKind.ANIMATION
                )
                embedded_comment_images = any(
                    comment.get("rich_media") for comment in rich_comments
                )
                continue

            if missing_kind == "photo" and any(
                media["kind"] == RichCommentMediaKind.PHOTO
                for comment in rich_comments
                for media in comment.get("rich_media") or []
            ):
                rich_comments = strip_comment_media_kind(rich_comments, RichCommentMediaKind.PHOTO)
                embedded_comment_images = any(
                    comment.get("rich_media") for comment in rich_comments
                )
                continue

            if embedded_comment_images:
                logger.warning(
                    "Telegram could not fetch rich media: %s. Retrying without images.",
                    exc,
                )
                rich_comments = strip_comment_images(rich_comments)
                embedded_comment_images = False
                continue

            raise


def upload_video_for_file_id(path: Path) -> str:
    """Uploads a video to the storage chat to obtain a Telegram file_id, then deletes it."""
    upload_path = path
    temporary = False
    if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
        logger.info("Compressing %s for Telegram", path.name)
        upload_path = compressed_copy(path)
        temporary = True

    try:
        data = {**telegram_chat_data(), "supports_streaming": "true"}
        with upload_path.open("rb") as video_file:
            result = telegram_call(
                TelegramMethod.SEND_VIDEO,
                data,
                {"video": (upload_path.name, video_file, "video/mp4")},
            )

        video = result.get("video") or {}
        file_id = video.get("file_id")
        if not file_id:
            raise RuntimeError("Telegram did not return a video file_id")

        message_id = int(result["message_id"])
        delete_telegram_message(settings.TELEGRAM_CHAT_ID, message_id)
        return str(file_id)
    finally:
        if temporary:
            upload_path.unlink(missing_ok=True)


def download_tiktok_video_file(video_id: str, liked_at: int) -> Path:
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
    validate_upload_capacity(path)
    return path


def prepare_inline_video(db: sqlite3.Connection, video_id: str) -> CachedInlineVideo:
    cached = get_cached_inline_video(db, video_id)
    if cached:
        logger.info("Using cached Telegram file_id for video %s", video_id)
        return cached

    liked_at = int(time.time())
    path = download_tiktok_video_file(video_id, liked_at)
    try:
        file_id = upload_video_for_file_id(path)
        caption = format_inline_caption(video_id)
        cached = CachedInlineVideo(video_id=video_id, file_id=file_id, caption=caption)
        store_cached_inline_video(db, cached)
        logger.info("Prepared inline video %s with file_id cache", video_id)
        return cached
    finally:
        cleanup_download(path)


def extract_video_id_from_via_bot_message(message: TelegramMessage) -> str | None:
    text = message.get("caption") or message.get("text") or ""
    url = extract_tiktok_url(text)
    if not url:
        match = TIKTOK_VIDEO_ID_RE.search(text)
        if match:
            return match.group("video_id")
        return None
    try:
        return extract_tiktok_video_id(url)
    except Exception:
        logger.exception("Could not parse TikTok id from via_bot caption")
        return None


def handle_via_bot_message(db: sqlite3.Connection, message: TelegramMessage) -> None:
    if not message_is_via_our_bot(message):
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return

    if rich_reply_already_sent(db, chat_id, int(message_id)):
        return

    video_id = extract_video_id_from_via_bot_message(message)
    if not video_id:
        video = message.get("video") or {}
        file_id = str(video.get("file_id") or "")
        if file_id:
            row = db.execute(
                "SELECT video_id FROM inline_video_cache WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row:
                video_id = str(row["video_id"])

    if not video_id:
        logger.info(
            "via_bot message %s in chat %s has no TikTok video id; skip RichMessage",
            message_id,
            chat_id,
        )
        return

    logger.info(
        "Posting RichMessage comments for video %s as reply to message %s in chat %s",
        video_id,
        message_id,
        chat_id,
    )
    try:
        comments = fetch_top_comments(video_id)
        if not comments:
            mark_rich_reply(db, chat_id, int(message_id), video_id, "empty")
            logger.info("No comments for video %s", video_id)
            return

        rich_comments = enrich_comments_with_external_media(comments)
        try:
            rich_message_id = send_rich_comments_with_fallbacks(
                chat_id, int(message_id), rich_comments
            )
        except Exception:
            logger.exception(
                "RichMessage failed for video %s; falling back to plain text comments",
                video_id,
            )
            rich_message_id = send_plain_comments(chat_id, int(message_id), comments)

        mark_rich_reply(
            db, chat_id, int(message_id), video_id, "sent", rich_message_id=rich_message_id
        )
        logger.info(
            "Comments for video %s sent as message %s in chat %s",
            video_id,
            rich_message_id,
            chat_id,
        )
    except Exception:
        mark_rich_reply(db, chat_id, int(message_id), video_id, "failed")
        logger.exception(
            "Failed to deliver comments for video %s (message %s chat %s)",
            video_id,
            message_id,
            chat_id,
        )


def answer_inline_query(
    inline_query_id: str,
    results: list[JsonObject],
    *,
    cache_time: int | None = None,
    is_personal: bool = True,
) -> None:
    telegram_call(
        TelegramMethod.ANSWER_INLINE_QUERY,
        {
            "inline_query_id": inline_query_id,
            "results": json.dumps(results, ensure_ascii=False),
            "cache_time": INLINE_CACHE_TIME_SECONDS if cache_time is None else cache_time,
            "is_personal": "true" if is_personal else "false",
        },
    )


def article_result(result_id: str, title: str, description: str, message_text: str) -> JsonObject:
    return {
        "type": "article",
        "id": result_id[:INLINE_RESULT_ID_MAX_LENGTH],
        "title": title,
        "description": description,
        "input_message_content": {
            "message_text": message_text,
        },
    }


def cached_video_result(cached: CachedInlineVideo) -> JsonObject:
    result: JsonObject = {
        "type": "video",
        "id": f"video-{cached.video_id}"[:INLINE_RESULT_ID_MAX_LENGTH],
        "video_file_id": cached.file_id,
        "title": f"TikTok {cached.video_id}",
        "description": "Отправить видео от своего имени",
    }
    if cached.caption:
        result["caption"] = cached.caption
    return result


def handle_inline_query(db: sqlite3.Connection, query: TelegramInlineQuery) -> None:
    query_id = str(query.get("id") or "")
    if not query_id:
        return

    text = str(query.get("query") or "").strip()
    if not text:
        answer_inline_query(
            query_id,
            [
                article_result(
                    "help",
                    "Вставьте ссылку на TikTok",
                    "После загрузки выберите результат — видео уйдёт от вашего имени",
                    "Отправьте inline-запрос вида:\n@bot https://www.tiktok.com/@user/video/…",
                )
            ],
            cache_time=10,
        )
        return

    url = extract_tiktok_url(text)
    if not url:
        answer_inline_query(
            query_id,
            [
                article_result(
                    "invalid-url",
                    "Не похоже на ссылку TikTok",
                    "Нужна ссылка вида tiktok.com/@…/video/… или vm.tiktok.com/…",
                    f"Не удалось распознать ссылку TikTok в запросе:\n{text}",
                )
            ],
            cache_time=5,
        )
        return

    try:
        video_id = extract_tiktok_video_id(url)
        cached = prepare_inline_video(db, video_id)
        answer_inline_query(query_id, [cached_video_result(cached)])
    except Exception as exc:
        logger.exception("Failed to prepare inline TikTok result for query %s", query_id)
        answer_inline_query(
            query_id,
            [
                article_result(
                    f"error-{int(time.time())}",
                    "Не удалось скачать видео",
                    str(exc)[:120],
                    f"Не удалось обработать {url}:\n{exc}",
                )
            ],
            cache_time=0,
        )


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


def process_telegram_updates(db: sqlite3.Connection, timeout: int) -> None:
    offset = initialize_telegram_update_offset(db)
    updates = telegram_get_updates(offset=offset, timeout=timeout)
    for update in updates:
        update_id = int(update["update_id"])
        inline_query = update.get("inline_query")
        if isinstance(inline_query, dict):
            if "from" in inline_query and "from_user" not in inline_query:
                inline_query = {**inline_query, "from_user": inline_query["from"]}
            try:
                handle_inline_query(db, inline_query)
            except Exception:
                logger.exception("Failed to handle inline_query update %s", update_id)

        for key in ("message", "channel_post"):
            message = update.get(key)
            if isinstance(message, dict):
                try:
                    handle_via_bot_message(db, message)
                except Exception:
                    logger.exception("Failed to handle %s update %s", key, update_id)

        set_metadata(db, TELEGRAM_UPDATE_OFFSET_KEY, update_id + 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid TikTok bot: @bot + link → video from your name; "
            "bot replies with RichMessage comments in the same chat"
        )
    )
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
            if args.once:
                return


if __name__ == "__main__":
    main()
