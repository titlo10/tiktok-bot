from types import SimpleNamespace

import requests

from tiktok_bot import application as liked_bot


def test_extract_single_tiktok_url_accepts_only_bare_link() -> None:
    assert (
        liked_bot.extract_single_tiktok_url(
            " https://www.tiktok.com/@user/video/7658587349265255694 "
        )
        == "https://www.tiktok.com/@user/video/7658587349265255694"
    )
    assert (
        liked_bot.extract_single_tiktok_url("vm.tiktok.com/ZMh123/")
        == "https://vm.tiktok.com/ZMh123/"
    )
    assert liked_bot.extract_single_tiktok_url("look https://www.tiktok.com/@u/video/1") is None


def test_extract_tiktok_url_finds_link_inside_inline_query() -> None:
    assert (
        liked_bot.extract_tiktok_url("look https://www.tiktok.com/@u/video/1 please")
        == "https://www.tiktok.com/@u/video/1"
    )
    assert liked_bot.extract_tiktok_url("vm.tiktok.com/ZMh123/") == "https://vm.tiktok.com/ZMh123/"


def test_extract_tiktok_video_id_from_url_supports_canonical_and_query_urls() -> None:
    assert (
        liked_bot.extract_tiktok_video_id_from_url(
            "https://www.tiktok.com/@user/video/7658587349265255694?lang=en"
        )
        == "7658587349265255694"
    )
    assert (
        liked_bot.extract_tiktok_video_id_from_url(
            "https://www.tiktok.com/share/video?item_id=7658587349265255694"
        )
        == "7658587349265255694"
    )


def test_upload_video_for_file_id_uses_multipart_and_deletes_storage_message(
    monkeypatch, tmp_path
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls: list[tuple] = []
    monkeypatch.setattr(liked_bot, "telegram_chat_data", lambda: {"chat_id": "123"})
    monkeypatch.setattr(liked_bot.settings, "TELEGRAM_CHAT_ID", "123")

    def fake_call(method, data, files=None):
        calls.append((method, data, files is not None))
        if method == liked_bot.TelegramMethod.SEND_VIDEO:
            assert files is not None
            assert "video" in files
            return {"message_id": 99, "video": {"file_id": "file-abc"}}
        return {}

    monkeypatch.setattr(liked_bot, "telegram_call", fake_call)

    file_id = liked_bot.upload_video_for_file_id(video)

    assert file_id == "file-abc"
    assert calls[0][0] == liked_bot.TelegramMethod.SEND_VIDEO
    assert calls[0][1] == {
        "chat_id": "123",
        "supports_streaming": "true",
    }
    assert calls[0][2] is True
    assert calls[1][0] == liked_bot.TelegramMethod.DELETE_MESSAGE


def test_handle_inline_query_returns_help_when_empty(monkeypatch) -> None:
    answers: list[tuple] = []
    monkeypatch.setattr(
        liked_bot,
        "answer_inline_query",
        lambda query_id, results, **kwargs: answers.append((query_id, results, kwargs)),
    )

    liked_bot.handle_inline_query(object(), {"id": "q1", "query": "  "})

    assert answers[0][0] == "q1"
    assert answers[0][1][0]["id"] == "help"


def test_handle_inline_query_returns_cached_video(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(liked_bot, "STATE_DB", db_path)
    db = liked_bot.connect_db()
    answers: list[list] = []
    monkeypatch.setattr(
        liked_bot,
        "answer_inline_query",
        lambda _query_id, results, **_kwargs: answers.append(results),
    )
    monkeypatch.setattr(
        liked_bot,
        "prepare_inline_video",
        lambda _db, video_id: liked_bot.CachedInlineVideo(
            video_id=video_id,
            file_id="file-1",
            caption="caption",
        ),
    )

    liked_bot.handle_inline_query(
        db,
        {
            "id": "q2",
            "query": "https://www.tiktok.com/@user/video/7658587349265255694",
        },
    )

    assert answers[0][0]["type"] == "video"
    assert answers[0][0]["video_file_id"] == "file-1"
    assert answers[0][0]["caption"] == "caption"


def test_process_updates_handles_inline_query(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(liked_bot, "STATE_DB", db_path)
    db = liked_bot.connect_db()
    liked_bot.set_metadata(db, liked_bot.TELEGRAM_UPDATE_OFFSET_KEY, 10)
    handled: list[dict] = []
    monkeypatch.setattr(
        liked_bot,
        "telegram_get_updates",
        lambda offset, timeout: [
            {
                "update_id": 10,
                "inline_query": {
                    "id": "iq",
                    "query": "https://vm.tiktok.com/ZMh/",
                    "from": {"id": 1},
                },
            }
        ],
    )
    monkeypatch.setattr(
        liked_bot,
        "handle_inline_query",
        lambda _db, query: handled.append(query),
    )

    liked_bot.process_telegram_updates(db, timeout=0)

    assert liked_bot.get_metadata(db, liked_bot.TELEGRAM_UPDATE_OFFSET_KEY) == "11"
    assert handled[0]["from_user"]["id"] == 1


def test_process_updates_keeps_offset_on_handler_failure(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(liked_bot, "STATE_DB", db_path)
    db = liked_bot.connect_db()
    liked_bot.set_metadata(db, liked_bot.TELEGRAM_UPDATE_OFFSET_KEY, 10)
    monkeypatch.setattr(
        liked_bot,
        "telegram_get_updates",
        lambda offset, timeout: [{"update_id": 10, "inline_query": {"id": "iq", "query": "x"}}],
    )
    monkeypatch.setattr(
        liked_bot,
        "handle_inline_query",
        lambda _db, _query: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    liked_bot.process_telegram_updates(db, timeout=0)

    assert liked_bot.get_metadata(db, liked_bot.TELEGRAM_UPDATE_OFFSET_KEY) == "11"


def test_format_inline_caption_includes_comments_and_media_links() -> None:
    caption = liked_bot.format_inline_caption(
        "765",
        [{"username": "alice", "text": "hi", "likes": 3}],
        ["https://example.test/a.gif"],
    )
    assert "https://www.tiktok.com/@/video/765" in caption
    assert "@alice" in caption
    assert "https://example.test/a.gif" in caption


def test_classify_comment_media_preserves_supported_formats() -> None:
    gif = b"GIF89a" + b"content"
    jpeg = b"\xff\xd8\xff" + b"content"
    png = b"\x89PNG\r\n\x1a\n" + b"content"
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"content"

    cases = [
        (gif, "image/gif", liked_bot.RichCommentMediaKind.ANIMATION, ".gif"),
        (jpeg, "image/jpeg", liked_bot.RichCommentMediaKind.PHOTO, ".jpg"),
        (png, "image/png", liked_bot.RichCommentMediaKind.PHOTO, ".png"),
        (mp4, "video/mp4", liked_bot.RichCommentMediaKind.ANIMATION, ".mp4"),
    ]
    for data, content_type, kind, suffix in cases:
        media = liked_bot.classify_comment_media(data, content_type)
        assert media.data == data
        assert media.kind == kind
        assert media.suffix == suffix


def test_classify_comment_media_converts_heic_to_jpeg(monkeypatch) -> None:
    source = b"\x00\x00\x00\x1cftypheic" + b"content"
    converted = b"\xff\xd8\xffconverted"
    calls: list[bytes] = []
    monkeypatch.setattr(
        liked_bot,
        "convert_comment_media_to_jpeg",
        lambda data: calls.append(data) or converted,
    )

    media = liked_bot.classify_comment_media(source, "image/heic")

    assert calls == [source]
    assert media.data == converted
    assert media.kind == liked_bot.RichCommentMediaKind.PHOTO
    assert media.suffix == ".jpg"


def test_classify_comment_media_converts_animated_webp(monkeypatch) -> None:
    source = b"RIFF\x10\x00\x00\x00WEBPVP8XANIM"
    converted = b"GIF89aconverted"
    calls: list[tuple[bytes, liked_bot.CommentAnimationSourceFormat]] = []
    monkeypatch.setattr(
        liked_bot,
        "convert_comment_animation",
        lambda data, source_format: calls.append((data, source_format)) or converted,
    )

    media = liked_bot.classify_comment_media(source, "image/webp")

    assert calls == [(source, liked_bot.CommentAnimationSourceFormat.WEBP)]
    assert media.data == converted
    assert media.kind == liked_bot.RichCommentMediaKind.ANIMATION
    assert media.suffix == ".gif"


def test_classify_comment_media_uses_remote_webp_converter(monkeypatch) -> None:
    source = b"RIFF\x10\x00\x00\x00WEBPVP8XANIM"
    converted = b"GIF89aremote"
    response = requests.Response()
    response.status_code = 200
    response._content = converted
    response.headers["content-type"] = "application/octet-stream"
    monkeypatch.setattr(liked_bot, "CONVERTAPI_TOKEN", "token", raising=False)
    monkeypatch.setattr(liked_bot.requests, "post", lambda *_args, **_kwargs: response)

    media = liked_bot.classify_comment_media(source, "image/webp")

    assert media.data == converted
    assert media.kind == liked_bot.RichCommentMediaKind.ANIMATION
    assert media.suffix == ".gif"


def test_classify_comment_media_falls_back_when_remote_converter_fails(monkeypatch) -> None:
    source = b"RIFF\x10\x00\x00\x00WEBPVP8XANIM"
    converted = b"GIF89alocal"
    response = requests.Response()
    response.status_code = 500
    response.url = "https://example.test/convert"
    monkeypatch.setattr(liked_bot, "CONVERTAPI_TOKEN", "token", raising=False)
    monkeypatch.setattr(liked_bot.requests, "post", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        liked_bot.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=converted),
    )

    media = liked_bot.classify_comment_media(source, "image/webp")

    assert media.data == converted
    assert media.kind == liked_bot.RichCommentMediaKind.ANIMATION
    assert media.suffix == ".gif"


def test_collect_sticker_url_groups_deduplicates_identical_groups() -> None:
    url = "https://example.test/sticker.webp"

    groups = liked_bot.collect_sticker_url_groups(
        {
            "sticker": {
                "display": {"url_list": [url]},
                "animated": {"url_list": [url]},
            }
        }
    )

    assert groups == [[url]]


def test_fetch_top_comments_keeps_sticker_with_media(monkeypatch) -> None:
    response = requests.Response()
    response.status_code = 200
    response._content = b"""{
        "status_code": 0,
        "comments": [{
            "text": "[sticker]",
            "digg_count": 7,
            "create_time": 123,
            "user": {"unique_id": "alice"},
            "sticker": {"url_list": ["https://example.test/sticker.webp"]}
        }]
    }"""
    session = requests.Session()
    monkeypatch.setattr(
        liked_bot.http.cookiejar.MozillaCookieJar,
        "load",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(session, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(liked_bot.requests, "Session", lambda: session)

    comments = liked_bot.fetch_top_comments("765")

    assert len(comments) == 1
    assert comments[0]["text"] == "Стикер"
    assert comments[0]["image_urls"] == ["https://example.test/sticker.webp"]


def test_upload_media_to_external_host(monkeypatch) -> None:
    response = requests.Response()
    response.status_code = 200
    response._content = b"https://litter.catbox.moe/abc.gif"
    monkeypatch.setattr(liked_bot.requests, "post", lambda *_args, **_kwargs: response)

    url = liked_bot.upload_media_to_external_host(b"GIF89a", "x.gif", "image/gif")

    assert url == "https://litter.catbox.moe/abc.gif"


def test_prepare_inline_video_uses_cache(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(liked_bot, "STATE_DB", db_path)
    db = liked_bot.connect_db()
    liked_bot.store_cached_inline_video(
        db,
        liked_bot.CachedInlineVideo(video_id="765", file_id="cached", caption="c"),
    )
    monkeypatch.setattr(
        liked_bot,
        "download_tiktok_video_file",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should use cache")),
    )

    cached = liked_bot.prepare_inline_video(db, "765")

    assert cached.file_id == "cached"
