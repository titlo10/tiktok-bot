from contextlib import suppress

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


def test_send_video_uses_file_uri_in_local_mode(monkeypatch, tmp_path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls: list[tuple[liked_bot.TelegramMethod, liked_bot.JsonObject]] = []
    monkeypatch.setattr(liked_bot, "TELEGRAM_LOCAL_MODE", True)
    monkeypatch.setattr(liked_bot, "telegram_chat_data", lambda: {"chat_id": "123"})
    monkeypatch.setattr(
        liked_bot,
        "telegram_call",
        lambda method, data: calls.append((method, data)) or {"message_id": 99},
    )

    message_id = liked_bot.send_video(video)

    assert message_id == 99
    assert calls == [
        (
            liked_bot.TelegramMethod.SEND_VIDEO,
            {
                "chat_id": "123",
                "supports_streaming": "true",
                "video": video.resolve().as_uri(),
            },
        )
    ]


def test_handle_tiktok_link_message_delivers_for_configured_chat(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(liked_bot.settings, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(liked_bot, "extract_tiktok_video_id", lambda _url: "765")
    monkeypatch.setattr(
        liked_bot,
        "delete_telegram_message",
        lambda chat_id, message_id: calls.append(("delete", chat_id, message_id)),
    )
    monkeypatch.setattr(
        liked_bot,
        "send_upload_action",
        lambda chat_id: calls.append(("action", chat_id)),
    )
    monkeypatch.setattr(
        liked_bot,
        "deliver_tiktok_video",
        lambda video_id, liked_at: calls.append(("deliver", video_id, liked_at))
        or liked_bot.DeliveredTikTokVideo(
            message_id=99,
            comments_status="empty",
            comment_media_status="empty",
        ),
    )

    liked_bot.handle_tiktok_link_message(
        {
            "message_id": 7,
            "date": 1783425000,
            "chat": {"id": "123"},
            "text": "https://www.tiktok.com/@user/video/765",
        }
    )

    assert calls == [
        ("action", "123"),
        ("deliver", "765", 1783425000),
        ("delete", "123", 7),
    ]


def test_handle_tiktok_link_message_preserves_source_on_failure(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(liked_bot.settings, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(liked_bot, "extract_tiktok_video_id", lambda _url: "765")
    monkeypatch.setattr(
        liked_bot,
        "delete_telegram_message",
        lambda chat_id, message_id: calls.append(("delete", (chat_id, message_id))),
    )
    monkeypatch.setattr(liked_bot, "send_upload_action", lambda _chat_id: None)
    monkeypatch.setattr(
        liked_bot,
        "deliver_tiktok_video",
        lambda _video_id, _liked_at: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    with suppress(RuntimeError):
        liked_bot.handle_tiktok_link_message(
            {
                "message_id": 7,
                "date": 1783425000,
                "chat": {"id": "123"},
                "text": "https://www.tiktok.com/@user/video/765",
            }
        )

    assert calls == []


def test_process_updates_commits_offset_after_success(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(liked_bot, "STATE_DB", db_path)
    db = liked_bot.connect_db()
    liked_bot.set_metadata(db, liked_bot.TELEGRAM_UPDATE_OFFSET_KEY, 10)
    monkeypatch.setattr(
        liked_bot,
        "telegram_get_updates",
        lambda offset, timeout: [{"update_id": 10, "message": {"message_id": 1}}],
    )
    monkeypatch.setattr(
        liked_bot,
        "handle_tiktok_link_message",
        lambda _message: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    with suppress(RuntimeError):
        liked_bot.process_telegram_updates(db, timeout=0)

    assert liked_bot.get_metadata(db, liked_bot.TELEGRAM_UPDATE_OFFSET_KEY) == "10"


def test_handle_tiktok_link_message_ignores_other_chats(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(liked_bot.settings, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(liked_bot, "deliver_tiktok_video", lambda *_args: calls.append("deliver"))

    liked_bot.handle_tiktok_link_message(
        {
            "message_id": 7,
            "date": 1783425000,
            "chat": {"id": "456"},
            "text": "https://www.tiktok.com/@user/video/765",
        }
    )

    assert calls == []


def test_rich_message_contains_only_comment_content(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(liked_bot, "telegram_chat_data", lambda: {"chat_id": "123"})
    monkeypatch.setattr(
        liked_bot,
        "telegram_call",
        lambda method, data: calls.append((method, data)) or {"message_id": 99},
    )

    message_id = liked_bot.send_rich_tiktok_message(
        "765",
        0,
        liked_bot.RichMediaSource(video_url="https://example.test/video.mp4"),
        [{"username": "alice", "text": "Комментарий", "image_urls": []}],
        reply_to_message_id=77,
    )

    assert message_id == 99
    method, data = calls[0]
    html = data["rich_message"]
    assert method == "sendRichMessage"
    assert "<video" not in html
    assert "Комментарий" in html
    assert data["reply_parameters"] == '{"message_id": 77}'


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


def test_classify_comment_media_converts_animated_webp(monkeypatch) -> None:
    source = b"RIFF\x10\x00\x00\x00WEBPVP8XANIM"
    converted = b"GIF89aconverted"
    calls: list[tuple[bytes, str]] = []
    monkeypatch.setattr(
        liked_bot,
        "convert_comment_animation",
        lambda data, target: calls.append((data, target)) or converted,
    )

    media = liked_bot.classify_comment_media(source, "image/webp")

    assert calls == [(source, "webp")]
    assert media.data == converted
    assert media.kind == liked_bot.RichCommentMediaKind.ANIMATION
    assert media.suffix == ".gif"


def test_rich_message_uses_video_tag_for_animation() -> None:
    html = liked_bot.build_rich_message_html(
        "765",
        0,
        liked_bot.RichMediaSource(),
        [
            {
                "username": "alice",
                "text": "animation",
                "rich_media": [
                    {
                        "kind": liked_bot.RichCommentMediaKind.ANIMATION,
                        "url": "https://example.test/sticker.gif",
                    }
                ],
            }
        ],
    )

    assert '<video src="https://example.test/sticker.gif"></video>' in html
    assert "<img" not in html


def test_publish_rich_media_reuses_identical_content(monkeypatch, tmp_path) -> None:
    source = b"GIF89a-content"
    published_path = tmp_path / "comment.gif"
    published_path.write_bytes(source)
    classifications: list[bytes] = []
    original_classifier = liked_bot.classify_comment_media
    monkeypatch.setattr(liked_bot, "RICH_MEDIA_DIR", tmp_path)
    monkeypatch.setattr(liked_bot, "RICH_MEDIA_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(
        liked_bot,
        "download_comment_media_bytes",
        lambda _url: (source, "image/gif"),
    )
    monkeypatch.setattr(
        liked_bot,
        "classify_comment_media",
        lambda data, content_type: classifications.append(data)
        or original_classifier(data, content_type),
    )
    monkeypatch.setattr(
        liked_bot,
        "publish_rich_bytes",
        lambda *_args: liked_bot.PublishedRichMedia(
            url="https://example.test/comment.gif",
            path=published_path,
        ),
    )
    monkeypatch.setattr(liked_bot, "rich_image_is_publicly_available", lambda _media: True)

    comments = liked_bot.publish_rich_comment_media(
        [
            {"image_url_candidates": [["https://one.test/a"]]},
            {"image_url_candidates": [["https://two.test/b"]]},
        ],
        "765",
    )

    assert classifications == [source]
    assert comments[0]["rich_media"] == comments[1]["rich_media"]


def test_run_cycle_does_not_scan_likes(monkeypatch) -> None:
    monkeypatch.setattr(
        liked_bot,
        "scan_likes",
        lambda _db: (_ for _ in ()).throw(AssertionError("likes must not be scanned")),
    )
    calls = []
    monkeypatch.setattr(liked_bot, "cleanup_rich_media", lambda: calls.append("cleanup"))

    liked_bot.run_cycle(object())

    assert calls == ["cleanup"]
