from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict


class TelegramAPIError(RuntimeError):
    def __init__(self, description: str):
        self.description = description
        super().__init__(f"Telegram API error: {description}")


class TelegramMethod(StrEnum):
    ANSWER_INLINE_QUERY = "answerInlineQuery"
    DELETE_MESSAGE = "deleteMessage"
    GET_CHAT = "getChat"
    GET_ME = "getMe"
    GET_UPDATES = "getUpdates"
    SEND_VIDEO = "sendVideo"


class CommentAnimationSourceFormat(StrEnum):
    PNG = "png"
    WEBP = "webp"


class RichCommentMediaKind(StrEnum):
    ANIMATION = "animation"
    PHOTO = "photo"


JsonObject = dict[str, Any]


class TelegramUser(TypedDict, total=False):
    id: int
    username: str
    first_name: str


class TelegramChat(TypedDict, total=False):
    id: str | int


class TelegramMessage(TypedDict, total=False):
    caption: str
    chat: TelegramChat
    date: int
    message_id: int
    text: str
    video: JsonObject


class TelegramInlineQuery(TypedDict, total=False):
    from_user: TelegramUser
    id: str
    offset: str
    query: str


class TelegramUpdate(TypedDict, total=False):
    inline_query: TelegramInlineQuery
    message: TelegramMessage
    update_id: int


class TikTokComment(TypedDict, total=False):
    created_at: int
    has_sticker: bool
    image_url_candidates: list[list[str]]
    image_urls: list[str]
    likes: int
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
class CachedInlineVideo:
    file_id: str
    video_id: str
    caption: str
