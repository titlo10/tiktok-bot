from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict


class TelegramAPIError(RuntimeError):
    def __init__(self, description: str):
        self.description = description
        super().__init__(f"Telegram API error: {description}")


class TelegramMethod(StrEnum):
    DELETE_MESSAGE = "deleteMessage"
    GET_CHAT = "getChat"
    GET_ME = "getMe"
    GET_UPDATES = "getUpdates"
    SEND_CHAT_ACTION = "sendChatAction"
    SEND_MEDIA_GROUP = "sendMediaGroup"
    SEND_MESSAGE = "sendMessage"
    SEND_PHOTO = "sendPhoto"
    SEND_RICH_MESSAGE = "sendRichMessage"
    SEND_VIDEO = "sendVideo"


class DeliveryStatus(StrEnum):
    EMPTY = "empty"
    FAILED = "failed"
    PENDING = "pending"
    SENT = "sent"
    SKIPPED = "skipped"


class RichCommentMediaKind(StrEnum):
    ANIMATION = "animation"
    PHOTO = "photo"


JsonObject = dict[str, Any]


class TelegramChat(TypedDict, total=False):
    id: str | int


class TelegramMessage(TypedDict, total=False):
    caption: str
    chat: TelegramChat
    date: int
    message_id: int
    text: str


class TelegramUpdate(TypedDict, total=False):
    message: TelegramMessage
    update_id: int


class TikTokComment(TypedDict, total=False):
    created_at: int
    has_sticker: bool
    image_url_candidates: list[list[str]]
    image_urls: list[str]
    likes: int
    rich_media: list["RichCommentMedia"]
    text: str
    username: str


class RichCommentMedia(TypedDict):
    kind: RichCommentMediaKind
    url: str


@dataclass
class DownloadedCommentMedia:
    content_type: str
    data: bytes
    kind: RichCommentMediaKind
    suffix: str


@dataclass
class RichMediaSource:
    video_url: str | None = None
    audio_url: str | None = None
    webpage_url: str | None = None
    description: str | None = None
    uploader: str | None = None
    duration: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None


@dataclass
class PublishedRichMedia:
    url: str
    path: Path


@dataclass
class DeliveredTikTokVideo:
    message_id: int
    comments_status: str
    comment_media_status: str
