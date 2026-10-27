import os
import time
import html
import requests
import certifi
from dotenv import load_dotenv

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_SOURCE_CHAT_ID = os.getenv("TG_SOURCE_CHAT_ID", "").strip()
MAX_TOKEN = os.getenv("MAX_TOKEN", "").strip()
MAX_CHAT_ID = os.getenv("MAX_CHAT_ID", "").strip()

if not all([TG_BOT_TOKEN, TG_SOURCE_CHAT_ID, MAX_TOKEN, MAX_CHAT_ID]):
    raise SystemExit(
        "Заполните TG_BOT_TOKEN, TG_SOURCE_CHAT_ID, MAX_TOKEN и MAX_CHAT_ID "
        "в переменных окружения Bothost"
    )

try:
    MAX_CHAT_ID = int(MAX_CHAT_ID)
except ValueError:
    raise SystemExit("MAX_CHAT_ID должен быть числовым ID чата или канала MAX")

TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}"
MAX_API = "https://platform-api2.max.ru"
CA_BUNDLE = certifi.where()


def telegram_api(method, data=None, timeout=35):
    response = requests.post(
        f"{TG_API}/{method}",
        data=data or {},
        timeout=timeout,
        verify=CA_BUNDLE,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {result}")
    return result["result"]


def max_request(method, *, params=None, json_body=None, timeout=35):
    response = requests.request(
        method,
        f"{MAX_API}/messages",
        params=params or {"chat_id": MAX_CHAT_ID},
        headers={"Authorization": MAX_TOKEN, "Content-Type": "application/json"},
        json=json_body,
        timeout=timeout,
        verify=CA_BUNDLE,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = response.text[:1000]
    if not response.ok:
        raise RuntimeError(f"MAX API HTTP {response.status_code}: {payload}")
    return payload


def utf16_to_index(text, offset):
    """Convert Telegram's UTF-16 entity offset to a Python string index."""
    used = 0
    for index, char in enumerate(text):
        if used >= offset:
            return index
        used += len(char.encode("utf-16-le")) // 2
    return len(text)


def telegram_text_to_html(text, entities):
    """
    Convert Telegram message/caption entities into HTML accepted by MAX.
    Supports links, bold, italic, underline, strikethrough, spoiler, code,
    preformatted text, and blockquotes. Unknown entities remain plain text.
    """
    if not text:
        return ""

    if not entities:
        return html.escape(text)

    prepared = []
    for entity in entities:
        try:
            start = utf16_to_index(text, int(entity.get("offset", 0)))
            end = utf16_to_index(
                text, int(entity.get("offset", 0)) + int(entity.get("length", 0))
            )
            if end <= start:
                continue
            kind = entity.get("type", "")
            value = text[start:end]
            escaped = html.escape(value)

            if kind == "text_link":
                url = entity.get("url", "")
                if url.startswith(("https://", "http://", "tg://")):
                    escaped = f'<a href="{html.escape(url, quote=True)}">{escaped}</a>'
            elif kind == "url":
                url = value.strip()
                if url.startswith(("https://", "http://")):
                    escaped = f'<a href="{html.escape(url, quote=True)}">{escaped}</a>'
            elif kind == "bold":
                escaped = f"<b>{escaped}</b>"
            elif kind == "italic":
                escaped = f"<i>{escaped}</i>"
            elif kind == "underline":
                escaped = f"<u>{escaped}</u>"
            elif kind == "strikethrough":
                escaped = f"<s>{escaped}</s>"
            elif kind == "spoiler":
                escaped = f"<span class=\"spoiler\">{escaped}</span>"
            elif kind == "code":
                escaped = f"<code>{escaped}</code>"
            elif kind == "pre":
                escaped = f"<pre>{escaped}</pre>"
            elif kind == "blockquote":
                escaped = f"<blockquote>{escaped}</blockquote>"
            else:
                continue

            prepared.append((start, end, escaped, kind))
        except (TypeError, ValueError):
            continue

    # Telegram entities may overlap (for example, bold text containing a link).
    # To avoid corrupting markup in such cases, use the first non-overlapping
    # entities in source order.
    prepared.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    result = []
    cursor = 0
    for start, end, rendered, _kind in prepared:
        if start < cursor:
            continue
        result.append(html.escape(text[cursor:start]))
        result.append(rendered)
        cursor = end
    result.append(html.escape(text[cursor:]))
    return "".join(result)


def download_telegram_file(file_id):
    file_info = telegram_api("getFile", {"file_id": file_id})
    file_path = file_info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram не вернул file_path для фотографии")

    response = requests.get(
        f"{TG_FILE_API}/{file_path}",
        timeout=60,
        verify=CA_BUNDLE,
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return response.content, os.path.basename(file_path), content_type


def upload_image_to_max(image_bytes, filename, content_type):
    # Step 1: ask MAX for a one-time upload URL and media token.
    response = requests.post(
        f"{MAX_API}/uploads",
        params={"type": "image"},
        headers={"Authorization": MAX_TOKEN},
        timeout=35,
        verify=CA_BUNDLE,
    )
    try:
        upload_info = response.json()
    except ValueError:
        upload_info = response.text[:1000]
    if not response.ok:
        raise RuntimeError(f"MAX /uploads HTTP {response.status_code}: {upload_info}")

    upload_url = upload_info.get("url")
    media_token = upload_info.get("token")
    if not upload_url:
        raise RuntimeError(f"MAX не вернул URL загрузки изображения: {upload_info}")

    # Step 2: upload the actual bytes to the URL returned by MAX.
    upload_response = requests.post(
        upload_url,
        files={"data": (filename or "photo.jpg", image_bytes, content_type)},
        timeout=90,
    )
    try:
        upload_result = upload_response.json()
    except ValueError:
        upload_result = upload_response.text[:1000]
    if not upload_response.ok:
        raise RuntimeError(
            f"Ошибка загрузки изображения в хранилище MAX "
            f"(HTTP {upload_response.status_code}): {upload_result}"
        )

    # Some API responses include the token only in the first response;
    # others may return it in the upload response.
    if not media_token:
        media_token = upload_result.get("token") if isinstance(upload_result, dict) else None
    if not media_token:
        raise RuntimeError(
            "MAX принял файл, но не вернул токен изображения. "
            f"Ответ /uploads: {upload_info}; ответ загрузки: {upload_result}"
        )
    return media_token


def send_post_to_max(text_html, photo_file_id=None):
    attachments = []
    if photo_file_id:
        image_bytes, filename, content_type = download_telegram_file(photo_file_id)
        image_token = upload_image_to_max(image_bytes, filename, content_type)
        attachments.append({
            "type": "image",
            "payload": {"token": image_token},
        })
        # Give MAX time to process the uploaded image before creating the post.
        time.sleep(1.5)

    body = {"text": text_html, "format": "html"}
    if attachments:
        body["attachments"] = attachments

    return max_request("POST", json_body=body)


def main():
    print("Проверяем токен официального API MAX…")
    response = requests.get(
        f"{MAX_API}/me",
        headers={"Authorization": MAX_TOKEN},
        timeout=25,
        verify=CA_BUNDLE,
    )
    try:
        info = response.json()
    except ValueError:
        info = response.text[:1000]
    if not response.ok:
        raise SystemExit(
            f"Не удалось авторизоваться в официальном API MAX "
            f"(HTTP {response.status_code}): {info}"
        )

    print("Авторизация MAX успешна. Начинаем проверять новые сообщения Telegram.")

    # Skip old queued updates on first start to avoid forwarding history.
    updates = telegram_api(
        "getUpdates",
        {"timeout": 0, "allowed_updates": '["message", "channel_post"]'},
    )
    offset = max((item["update_id"] for item in updates), default=0) + 1

    while True:
        try:
            updates = telegram_api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": '["message", "channel_post"]',
                },
                timeout=35,
            )
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("channel_post") or update.get("message")
                if not message:
                    continue

                chat = message.get("chat", {})
                if str(chat.get("id")) != TG_SOURCE_CHAT_ID:
                    continue

                text = message.get("text") or message.get("caption") or ""
                entities = (
                    message.get("entities", [])
                    if message.get("text") is not None
                    else message.get("caption_entities", [])
                )
                text_html = telegram_text_to_html(text, entities)

                photo_file_id = None
                photos = message.get("photo") or []
                if photos:
                    # Telegram supplies photo sizes from smaller to larger.
                    photo_file_id = photos[-1].get("file_id")

                # Ignore only messages with neither text nor supported photo.
                if not text_html.strip() and not photo_file_id:
                    if message.get("document") or message.get("video"):
                        print(
                            "Пропущено сообщение: пока поддерживаются текст, "
                            "гиперссылки и фотографии; документы/видео не реализованы."
                        )
                    continue

                try:
                    send_post_to_max(text_html, photo_file_id)
                    print(
                        f"Переслан пост Telegram update_id={update['update_id']} "
                        f"в MAX chat_id={MAX_CHAT_ID}; фото: "
                        f"{'да' if photo_file_id else 'нет'}"
                    )
                except Exception as exc:
                    print(f"Ошибка отправки в MAX: {exc}")

        except KeyboardInterrupt:
            print("Остановка по команде пользователя.")
            break
        except Exception as exc:
            print(f"Ошибка цикла: {exc}. Повтор через 5 секунд.")
            time.sleep(5)


if __name__ == "__main__":
    main()
