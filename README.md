# TikTok Bot

Telegram-бот скачивает TikTok по ссылкам, отправляет видео через локальный Telegram Bot API Server и добавляет популярные комментарии. Локальный API позволяет загружать файлы больше 50 МБ.

Конфигурация и cookies хранятся только на сервере в `/opt/tiktok-bot/shared/config`. Push в `main` запускает проверки, после которых GitHub Actions создаёт атомарный release на сервере `ber`.

`CONVERTAPI_TOKEN` в `shared/config/secrets.env` включает удалённую конвертацию анимированных WebP-комментариев в GIF. При ошибке или недоступности ConvertAPI бот использует локальную оптимизированную конвертацию.
