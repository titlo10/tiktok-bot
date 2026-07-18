# TikTok Bot

Гибридный Telegram-бот:

1. В чате: `@имя_бота` + ссылка на TikTok → **видео уходит от вашего имени** (inline).
2. Бот, состоя в том же чате, видит сообщение `via_bot` и отвечает **RichMessage**
   с топ-комментариями (текст + картинки/GIF).

## Как пользоваться

1. BotFather: **inline mode** (`/setinline`) — обязательно.
2. Бот должен **видеть сообщения в группе**:
   - либо **админ**,
   - либо BotFather → `/setprivacy` → **Disable**.
3. В чате: `@your_bot https://www.tiktok.com/@user/video/…`
4. Выберите результат — видео от вас; следом reply от бота с комментариями.

Под видео caption пустой. Для RichMessage бот находит `video_id` по `file_id`
из кэша после inline-отправки.

## RichMessage и медиа

`sendRichMessage` с `<img>`/`<video>` требует **публичный HTTPS URL**.
Свой nginx больше не используется.

Картинки/GIF из комментариев перезаливаются на
[litterbox.catbox.moe](https://litterbox.catbox.moe) (TTL 24h по умолчанию):

- `EXTERNAL_MEDIA_UPLOAD_URL`
- `EXTERNAL_MEDIA_TTL` (`1h` / `12h` / `24h` / `72h`)

Если RichMessage недоступен или медиа не подтянулось — fallback: обычный
текстовый reply с топ-комментариями.

## Конфигурация

Конфиг только на сервере в `/opt/tiktok-bot/shared/config`.

| Параметр | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `TELEGRAM_CHAT_ID` | служебный чат для кэша `file_id` (сообщение сразу удаляется) |
| `COOKIES_FILE` | опционально: cookies TikTok (комментарии / login wall) |
| `KEEP_DOWNLOADS` | оставлять ли скачанные mp4 |
| `CONVERTAPI_TOKEN` | опционально: animated WebP → GIF |

Локальный Bot API (`TELEGRAM_API_BASE_URL` + `TELEGRAM_LOCAL_MODE`) — для
файлов > 50 МБ. Нужен для `sendRichMessage`.

## Деплой

Прод: `144.31.156.46` (SSH `4242`).

Push в `main` → CI → Deploy (`BER_HOST`, `BER_SSH_PORT`, `BER_DEPLOY_KEY`).

```bash
systemctl restart tiktok-liked-bot.service
journalctl -u tiktok-liked-bot.service -f
```
