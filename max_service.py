"""MAX relay: принимает пост от Telegram-бота, адаптирует ссылки и отправляет в MAX."""
import asyncio
import html
import logging
import os
import re
import sqlite3
from contextlib import closing
from typing import Optional
from pathlib import Path
import aiohttp
import certifi
import ssl
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("max-relay")

MAX_TOKEN = os.getenv("MAX_TOKEN", "").strip()
MAX_CHANNEL_ID = os.getenv("MAX_CHANNEL_ID", "").strip()
RELAY_SECRET = os.getenv("MAX_RELAY_SECRET", "").strip()
DB_PATH = os.getenv("MAX_SUBSCRIBERS_DB", "max_subscribers.sqlite3")
BROADCAST_TO_USERS = os.getenv("MAX_BROADCAST_TO_USERS", "true").lower() in {"1", "true", "yes", "да"}
MAX_API = "https://platform-api2.max.ru"
TELEGRAM_URL = "https://t.me/+zOfQvQP-qcxmZjcy"
MAX_URL = "https://max.ru/channel_skidochnayapromo"

app = FastAPI(title="MAX post relay")


async def upload_image(
    session: aiohttp.ClientSession,
    photo: bytes,
    filename: str = "product.jpg"
) -> str:
    import json

    # 1. Запрашиваем у MAX адрес загрузки изображения.
    info = await max_api_request(
        session,
        "POST",
        "/uploads",
        params={"type": "image"}
    )

    if not isinstance(info, dict):
        raise RuntimeError(
            f"Неожиданный ответ MAX при запросе /uploads: {info!r}"
        )

    upload_url = info.get("url")
    token = info.get("token")

    if not upload_url:
        raise RuntimeError(
            f"MAX не вернул URL загрузки. Ответ: {info!r}"
        )

    # 2. Загружаем файл изображения.
    form = aiohttp.FormData()
    form.add_field(
        "data",
        photo,
        filename=filename,
        content_type="image/jpeg"
    )

    async with session.post(
        upload_url,
        data=form,
        headers={"Authorization": MAX_TOKEN},
        timeout=aiohttp.ClientTimeout(total=60)
    ) as response:
        body = await response.text()

        if response.status >= 400:
            raise RuntimeError(
                f"Ошибка загрузки изображения в MAX "
                f"HTTP {response.status}: {body[:500]}"
            )

        try:
            result = json.loads(body) if body else {}
        except ValueError:
            result = {}

        # Диагностика: записываем структуру ответа в лог.
        # Токены намеренно не выводим.
        if isinstance(result, dict):
            photos = result.get("photos")
            log.warning(
                "MAX UPLOAD RESPONSE: status=%s, top_level_keys=%s, "
                "photos_type=%s, photos_keys=%s",
                response.status,
                list(result.keys()),
                type(photos).__name__,
                list(photos.keys()) if isinstance(photos, dict) else None
            )
        else:
            photos = None
            log.warning(
                "MAX UPLOAD RESPONSE: status=%s, response_type=%s",
                response.status,
                type(result).__name__
            )

        # 3. Пробуем извлечь токен из известных вариантов ответа.
        if isinstance(result, dict):
            token = token or result.get("token")

            if isinstance(photos, dict):
                photo_ids = photos.get("photoIds")

                if isinstance(photo_ids, dict):
                    token = token or photo_ids.get("token")

                elif isinstance(photo_ids, str):
                    token = token or photo_ids

                token = token or photos.get("token")

                # Иногда photoIds может содержать вложенные данные.
                if not token:
                    for value in photos.values():
                        if isinstance(value, dict):
                            token = value.get("token")
                            if token:
                                break

    if not token:
        raise RuntimeError(
            "MAX принял изображение, но токен не найден. "
            "Посмотри в логах строку MAX UPLOAD RESPONSE."
        )

    # 4. Даём MAX время обработать изображение.
    await asyncio.sleep(1.5)

    return token


def adapt_for_max(text: str) -> str:
    """Адаптирует текст поста для публикации в MAX."""
    text = html.escape(text, quote=False)

    # Преобразуем ссылки Telegram в ссылки MAX.
    text = text.replace(
        html.escape(TELEGRAM_URL, quote=False),
        html.escape(MAX_URL, quote=False)
    )

    return text

async def send_to_target(
    session: aiohttp.ClientSession,
    target: str,
    text: str,
    image_token: Optional[str] = None,
    is_user: bool = False
):
    params = {
        "user_id" if is_user else "chat_id": target
    }

    body = {
        "text": text,
        "format": "html",
        "notify": True
    }

    if image_token:
        body["attachments"] = [
            {
                "type": "image",
                "payload": {"token": image_token}
            }
        ]

    try:
        await max_api_request(
            session,
            "POST",
            "/messages",
            params=params,
            json_body=body
        )
    except Exception as exc:
        log.error(
            "Ошибка отправки в MAX (тип получателя: %s): %s",
            "пользователь" if is_user else "канал",
            exc
        )
        raise

def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        con.commit()


def save_subscriber(user_id: int, active: bool = True) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            INSERT INTO subscribers (user_id, active)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET active=excluded.active
            """,
            (user_id, 1 if active else 0),
        )
        con.commit()


def get_subscribers() -> list[int]:
    with closing(sqlite3.connect(DB_PATH)) as con:
        rows = con.execute(
            "SELECT user_id FROM subscribers WHERE active=1"
        ).fetchall()
    return [int(row[0]) for row in rows]

@app.get("/health")
async def health():
    return {
        "ok": True,
        "max_token_configured": bool(MAX_TOKEN),
        "channel_configured": bool(MAX_CHANNEL_ID)
    }


@app.post("/max-webhook")
async def max_webhook(request: Request):
    """Обрабатывает события MAX и сохраняет подписчиков."""

    payload = await request.json()

    update_type = (
        payload.get("update_type")
        or payload.get("type")
        or ""
    )

    user = payload.get("user") or payload.get("from") or {}
    message = payload.get("message") or {}
    sender = message.get("sender") or {}

    user_id = (
        user.get("user_id")
        or user.get("id")
        or sender.get("user_id")
        or sender.get("id")
    )

    if user_id:
        try:
            if update_type in {
                "bot_started",
                "user_started",
                "message_created"
            }:
                save_subscriber(int(user_id), True)

            elif update_type in {
                "user_stopped",
                "bot_stopped"
            }:
                save_subscriber(int(user_id), False)

        except (TypeError, ValueError):
            log.warning("Некорректный user_id в событии MAX")

    return {"ok": True}


@app.post("/publish")
async def publish(
    request: Request,
    x_relay_secret: str = Header(default="")
):
    if not RELAY_SECRET or x_relay_secret != RELAY_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized relay"
        )

    if not MAX_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="MAX_TOKEN не настроен"
        )

    form = await request.form()

    text = adapt_for_max(str(form.get("text") or ""))

    upload = form.get("photo")
    photo = (
        await upload.read()
        if upload and hasattr(upload, "read")
        else None
    )

    if not text and not photo:
        raise HTTPException(
            status_code=400,
            detail="Пустой пост"
        )

    image_token = None
    results = []

    ssl_context = ssl.create_default_context(
        cafile=certifi.where()
    )

    certificate_path = Path(__file__).with_name(
        "mincifry_bundle.pem"
    )

    if not certificate_path.exists():
        raise HTTPException(
            status_code=500,
            detail="Не найден файл сертификатов mincifry_bundle.pem"
        )

    try:
        ssl_context.load_verify_locations(
            cafile=str(certificate_path)
        )
    except (OSError, ssl.SSLError) as exc:
        log.exception("Не удалось загрузить сертификаты")
        raise HTTPException(
            status_code=500,
            detail="Не удалось загрузить сертификаты для MAX"
        ) from exc

    connector = aiohttp.TCPConnector(ssl=ssl_context)

    async with aiohttp.ClientSession(
        connector=connector
    ) as session:

        if photo:
            image_token = await upload_image(
                session,
                photo,
                getattr(upload, "filename", None) or "product.jpg"
            )

        if MAX_CHANNEL_ID:
            await send_to_target(
                session,
                str(MAX_CHANNEL_ID),
                text,
                image_token
            )
            results.append("канал MAX")

        if BROADCAST_TO_USERS:
            sent = 0
            failed = 0

            for user_id in get_subscribers():
                try:
                    await send_to_target(
                        session,
                        str(user_id),
                        text,
                        image_token,
                        is_user=True
                    )
                    sent += 1
                    await asyncio.sleep(0.55)

                except Exception as exc:
                    failed += 1
                    log.warning(
                        "Не удалось отправить MAX-пользователю %s: %s",
                        user_id,
                        exc
                    )

            results.append(
                f"личные сообщения MAX: отправлено {sent}, ошибок {failed}"
            )

    if not results:
        raise HTTPException(
            status_code=503,
            detail=(
                "Укажи MAX_CHANNEL_ID и/или включи "
                "MAX_BROADCAST_TO_USERS"
            )
        )

    return JSONResponse({
        "ok": True,
        "message": "Пост передан в " + "; ".join(results)
    })


if __name__ == "__main__":
    import uvicorn

    db_init()

    uvicorn.run(
        "max_service:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )
