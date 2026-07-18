# TikTok Bot

Inline-only Telegram-бот: в любом чате пишете `@имя_бота` и ссылку на TikTok.
Бот скачивает ролик и отдаёт **inline-результат**; после выбора видео уходит
**от вашего имени** (как обычное сообщение через inline).

## Как пользоваться

1. В BotFather у бота должен быть включён inline mode (`/setinline`).
2. В чате: `@your_bot https://www.tiktok.com/@user/video/…` (или `vm.tiktok.com/…`).
3. Дождитесь результата и нажмите на него — видео отправится от вашего имени.
4. В подписи к видео: исходная ссылка, топ-комментарии текстом и ссылки на
   картинки/GIF из комментариев (если удалось перезалить).

## Медиа из комментариев

`sendRichMessage` с `<img>`/`<video>` требует **публичный HTTPS URL**.
Свой nginx/хостинг на сервере больше не используется.

Картинки и GIF из комментариев перезаливаются на временный сторонний хост
[litterbox.catbox.moe](https://litterbox.catbox.moe) (по умолчанию TTL 24h).
Ссылки попадают в caption. Переопределение:

- `EXTERNAL_MEDIA_UPLOAD_URL`
- `EXTERNAL_MEDIA_TTL` (`1h` / `12h` / `24h` / `72h`)

URL файлов Telegram (`/file/bot<token>/…`) не используются: они светят токен.

## Конфигурация

Конфиг и cookies только на сервере в `/opt/tiktok-bot/shared/config`.

| Параметр | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `TELEGRAM_CHAT_ID` | служебный чат для кэша `file_id` (сообщение сразу удаляется) |
| `COOKIES_FILE` | опционально: cookies TikTok (для комментариев / если yt-dlp упирается в login wall) |
| `KEEP_DOWNLOADS` | оставлять ли скачанные mp4 на диске |
| `CONVERTAPI_TOKEN` | опционально: удалённая конвертация animated WebP → GIF |

Локальный Telegram Bot API Server (`TELEGRAM_API_BASE_URL` + `TELEGRAM_LOCAL_MODE`)
нужен для роликов больше 50 МБ.

## Деплой

Прод-сервер: `144.31.156.46` (SSH порт `4242`).

Код лежит в `/opt/tiktok-bot` (releases + `current`), unit
`tiktok-liked-bot.service`. Локальный Bot API (общий Docker) слушает
`http://127.0.0.1:8081`.

Push в `main` → CI → GitHub Actions (vars `BER_HOST`, `BER_SSH_PORT=4242`,
secret `BER_DEPLOY_KEY`) вызывает `deploy/deploy-tiktok-bot`.

### Первый запуск на сервере

В `/opt/tiktok-bot/shared/config/`:

1. `settings.conf` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (обязательно)
2. опционально `cookies.txt` — для топ-комментариев и части «закрытых» роликов
3. опционально `secrets.env` — `CONVERTAPI_TOKEN=…`

Для inline-скачивания публичных ссылок cookies **не нужны**. Раньше они
использовались для импорта лайков/saved — этот режим бот больше не крутит.

`TELEGRAM_CHAT_ID` — **не** чат «куда слать ролики». Это служебный чат/канал,
куда бот кратковременно загружает файл, чтобы получить `file_id` для
`InlineQueryResultCachedVideo` (сообщение сразу удаляется). Видео в нужный
чат уходит от имени пользователя через inline.

В BotFather: `/setinline` (inline mode on).

```bash
systemctl restart tiktok-liked-bot.service
journalctl -u tiktok-liked-bot.service -f
```