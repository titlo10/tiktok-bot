import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
load_dotenv(CONFIG_DIR / "secrets.env")
SETTINGS_VALUES = dotenv_values(CONFIG_DIR / "settings.conf")


def config_value(name: str, default: str) -> str:
    return os.getenv(name) or SETTINGS_VALUES.get(name) or default


ENV_ALIASES = {
    "TIKTOK_USERNAME": "TIKTOK_USER",
    "TELEGRAM_GROUP_ID": "TELEGRAM_CHAT_ID",
}
for old_name, new_name in ENV_ALIASES.items():
    if value := os.getenv(old_name):
        os.environ.setdefault(new_name, value)

STATE_DB = CONFIG_DIR / "liked_bot.sqlite3"
POLL_SECONDS = max(30, int(os.getenv("BOT_POLL_SECONDS", "60")))
SCAN_LIMIT = max(20, int(os.getenv("BOT_SCAN_LIMIT", "200")))
KNOWN_STREAK_LIMIT = 10
TELEGRAM_UPDATE_LIMIT = max(1, min(100, int(os.getenv("TELEGRAM_UPDATE_LIMIT", "20"))))
TELEGRAM_UPDATE_TIMEOUT = max(0, int(os.getenv("TELEGRAM_UPDATE_TIMEOUT", "10")))
TELEGRAM_UPDATE_OFFSET_KEY = "telegram_update_offset"
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/")
TELEGRAM_LOCAL_MODE = os.getenv("TELEGRAM_LOCAL_MODE", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TELEGRAM_UPLOAD_LIMIT = 2000 * 1024 * 1024 if TELEGRAM_LOCAL_MODE else 50 * 1024 * 1024 - 4096
MAX_VIDEO_BYTES = max(1, int(os.getenv("MAX_VIDEO_BYTES", str(1500 * 1024 * 1024))))
MIN_FREE_DISK_BYTES = max(1, int(os.getenv("MIN_FREE_DISK_BYTES", str(512 * 1024 * 1024))))
POLL_ERROR_BACKOFF_MAX_SECONDS = 60
COMMENT_FETCH_LIMIT = 20
TOP_COMMENTS_LIMIT = 5
MAX_COMMENT_ATTEMPTS = 5
MAX_COMMENT_MEDIA_ATTEMPTS = 5
MAX_COMMENT_IMAGES = 6
MAX_COMMENT_IMAGE_BYTES = 10 * 1024 * 1024
COMMENT_ANIMATION_MAX_DIMENSION = 256
MAX_RICH_TEXT_LENGTH = 32_000
CONVERTAPI_TOKEN = os.getenv("CONVERTAPI_TOKEN", "")
CONVERTAPI_WEBP_TO_GIF_URL = "https://v2.convertapi.com/convert/webp/to/gif"
CONVERTAPI_TIMEOUT_SECONDS = 30
RICH_MEDIA_SUFFIXES = {".gif", ".jpg", ".jpeg", ".mp4", ".png", ".webp"}
RICH_MEDIA_PUBLIC_BASE_URL = config_value("RICH_MEDIA_PUBLIC_BASE_URL", "").rstrip("/")
RICH_MEDIA_DIR_RAW = config_value("RICH_MEDIA_DIR", "")
RICH_MEDIA_DIR = Path(RICH_MEDIA_DIR_RAW).expanduser() if RICH_MEDIA_DIR_RAW else None
RICH_MEDIA_TTL_SECONDS = max(60, int(config_value("RICH_MEDIA_TTL_SECONDS", "900")))
KEEP_DOWNLOADS = config_value("KEEP_DOWNLOADS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
UNAVAILABLE_STICKER_TEXTS = {
    "[sticker]",
    "[стикер]",
    "[наклейка]",
    "[ステッカー]",
}
STICKER_MARKER_RE = re.compile(
    r"^\s*\[(?:sticker|стикер|наклейка|ステッカー)\]\s*",
    re.IGNORECASE,
)
TIKTOK_WEB_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
)
TIKTOK_SINGLE_URL_RE = re.compile(
    r"\s*(?P<url>(?:https?://)?(?:(?:www|m|vm|vt)\.)?tiktok\.com/[^\s<>]+)\s*",
    re.IGNORECASE,
)
TIKTOK_VIDEO_ID_RE = re.compile(r"/(?:video|photo)/(?P<video_id>\d+)(?:[/?#]|$)")
TIKTOK_QUERY_VIDEO_ID_RE = re.compile(r"[?&](?:item_id|video_id|aweme_id)=(?P<video_id>\d+)")
