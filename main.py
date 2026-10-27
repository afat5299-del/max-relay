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
    raise SystemExit("Заполните TG_BOT_TOKEN, TG_SOURCE_CHAT_ID, MAX_TOKEN и MAX_CHAT_ID в переменных окружения Bothost")

try:
    MAX_CHAT_ID = int(MAX_CHAT_ID)
except ValueError:
    raise SystemExit("MAX_CHAT_ID должен быть числовым ID чата или канала MAX")

TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
MAX_API = "https://platform-api2.max.ru"
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CUSTOM_CA = BASE_DIR / "mincifry_bundle.pem"
COMBINED_CA = BASE_DIR / "combined-ca-bundle.pem"

if not CUSTOM_CA.exists():
    raise SystemExit("Не найден файл mincifry_bundle.pem")

COMBINED_CA.write_bytes(
    Path(certifi.where()).read_bytes()
    + b"\n"
    + CUSTOM_CA.read_bytes()
)

CA_BUNDLE = str(COMBINED_CA)

def telegram_api(method, data=None, timeout=35):
    response = requests.post(f"{TG_API}/{method}", data=data or {}, timeout=timeout, verify=certifi.where())
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {result}")
    return result["result"]


def telegram_file_bytes(file_id):
    """Download a Telegram file and return (bytes, filename)."""
    result = telegram_api("getFile", {"file_id": file_id})
    file_path = result["file_path"]
    response = requests.get(
        f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}/{file_path}",
        timeout=60,
        verify=certifi.where(),
    )
    response.raise_for_status()
    return response.content, os.path.basename(file_path) or "image.jpg"


def upload_image_to_max(file_bytes, filename):
    """Upload an image to MAX and return its media token."""
    headers = {"Authorization": MAX_TOKEN}
    response = requests.post(
        f"{MAX_API}/uploads",
        params={"type": "image"},
        headers=headers,
        timeout=35,
        verify=CA_BUNDLE,
    )
    response.raise_for_status()
    upload_info = response.json()
    upload_url = upload_info.get("url")
    if not upload_url:
        raise RuntimeError(f"MAX не вернул URL для загрузки изображения: {upload_info}")

    upload_response = requests.post(
        upload_url,
        headers={"Authorization": MAX_TOKEN},
        files={"data": (filename, file_bytes)},
        timeout=90,
        verify=CA_BUNDLE,
    )
    try:
        upload_result = upload_response.json()
    except ValueError:
        upload_result = upload_response.text[:1000]
    if not upload_response.ok:
        raise RuntimeError(
            f"Ошибка загрузки изображения в MAX HTTP "
            f"{upload_response.status_code}: {upload_result}"
        )

    # MAX image uploads commonly return photos.photoIds.token.
    token = (
        upload_result.get("photos", {})
        .get("photoIds", {})
        .get("token")
        or upload_result.get("token")
    )
    if not token:
        raise RuntimeError(f"MAX не вернул токен загруженного изображения: {upload_result}")
    return token


def telegram_text_to_max_html(text, entities=None):
    """Escape Telegram text and preserve hidden text_link entities as HTML links."""
    if not text:
        return ""

    # Telegram offsets are UTF-16 code units, not Python character positions.
    utf16_to_index = {0: 0}
    units = 0
    for index, char in enumerate(text, start=1):
        units += len(char.encode("utf-16-le")) // 2
        utf16_to_index[units] = index

    replacements = []
    for entity in entities or []:
        if entity.get("type") != "text_link" or not entity.get("url"):
            continue
        start_units = entity.get("offset", 0)
        end_units = start_units + entity.get("length", 0)
        start_index = utf16_to_index.get(start_units)
        end_index = utf16_to_index.get(end_units)
        if start_index is None or end_index is None or end_index <= start_index:
            continue
        label = html.escape(text[start_index:end_index], quote=False)
        url = html.escape(entity["url"], quote=True)
        replacements.append((start_index, end_index, f'<a href="{url}">{label}</a>'))

    # Apply from right to left so earlier offsets remain valid.
    result = text
    for start_index, end_index, replacement in sorted(replacements, reverse=True):
        result = result[:start_index] + replacement + result[end_index:]

    # Escape only text outside the generated anchors.
    # Split around our own anchor markup to avoid escaping the tags/URL attributes.
    import re
    parts = re.split(r'(<a href="[^"]*">.*?</a>)', result, flags=re.DOTALL)
    for index, part in enumerate(parts):
        if not (part.startswith('<a href="') and part.endswith('</a>')):
            parts[index] = html.escape(part, quote=False)
    return "".join(parts)


def send_to_max(text, attachments=None, formatted=False):
    """Send a text and optional attachments to MAX."""
    body = {}
    if text:
        body["text"] = text
    if attachments:
        body["attachments"] = attachments
    if formatted and text:
        body["format"] = "html"

    # MAX may need a short time to process freshly uploaded media.
    for attempt in range(4):
        response = requests.post(
            f"{MAX_API}/messages",
            params={"chat_id": MAX_CHAT_ID},
            headers={"Authorization": MAX_TOKEN, "Content-Type": "application/json"},
            json=body,
            timeout=35,
            verify=CA_BUNDLE,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:1000]
        if response.ok:
            return payload
        if attempt < 3 and (
            response.status_code in (429, 500, 502, 503, 504)
            or (isinstance(payload, dict) and payload.get("code") == "attachment.not.ready")
        ):
            time.sleep(1.5 * (attempt + 1))
            continue
        raise RuntimeError(f"MAX API HTTP {response.status_code}: {payload}")


def main():
    print("Проверяем токен официального API MAX…")
    # GET /me validates the bot token and returns the bot profile.
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
        raise SystemExit(f"Не удалось авторизоваться в официальном API MAX (HTTP {response.status_code}): {info}")
    print("Авторизация MAX успешна. Начинаем проверять новые сообщения Telegram.")

    # Skip old queued updates on first start to avoid forwarding history.
    updates = telegram_api("getUpdates", {"timeout": 0, "allowed_updates": '["message", "channel_post"]'})
    offset = max((item["update_id"] for item in updates), default=0) + 1

    while True:
        try:
            updates = telegram_api("getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": '["message", "channel_post"]'
            }, timeout=35)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("channel_post") or update.get("message")
                if not message:
                    continue
                chat = message.get("chat", {})
                if str(chat.get("id")) != TG_SOURCE_CHAT_ID:
                    continue
                text = message.get("text") or message.get("caption") or ""
                entities = message.get("entities") or message.get("caption_entities") or []
                max_text = telegram_text_to_max_html(text, entities)

                try:
                    attachments = []
                    photos = message.get("photo") or []
                    if photos:
                        # Telegram lists photo sizes from smallest to largest.
                        photo = max(photos, key=lambda item: item.get("file_size", 0))
                        image_bytes, filename = telegram_file_bytes(photo["file_id"])
                        image_token = upload_image_to_max(image_bytes, filename)
                        attachments.append({
                            "type": "image",
                            "payload": {"token": image_token},
                        })

                    # Forward text-only posts, photos with captions, and photos without captions.
                    if max_text.strip() or attachments:
                        send_to_max(max_text, attachments=attachments, formatted=bool(max_text))
                        print(
                            f"Переслан пост Telegram update_id={update['update_id']} "
                            f"в MAX chat_id={MAX_CHAT_ID}"
                        )
                    elif message.get("document") or message.get("video"):
                        print(
                            "Сообщение содержит документ или видео; пока реализована "
                            "пересылка текста, гиперссылок и изображений."
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
