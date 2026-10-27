import os
import time
import html
import requests
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


def telegram_api(method, data=None, timeout=35):
    response = requests.post(f"{TG_API}/{method}", data=data or {}, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {result}")
    return result["result"]


def send_to_max(text):
    """Send plain text to MAX using the official Chatbot API."""
    response = requests.post(
        f"{MAX_API}/messages",
        params={"chat_id": MAX_CHAT_ID},
        headers={"Authorization": MAX_TOKEN, "Content-Type": "application/json"},
        json={"text": text},
        timeout=35,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = response.text[:1000]
    if not response.ok:
        # Avoid printing the token; response body is safe to inspect unless MAX includes sensitive data.
        raise RuntimeError(f"MAX API HTTP {response.status_code}: {payload}")
    return payload


def main():
    print("Проверяем токен официального API MAX…")
    # GET /me validates the bot token and returns the bot profile.
    response = requests.get(
        f"{MAX_API}/me",
        headers={"Authorization": MAX_TOKEN},
        timeout=25,
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
                if not text.strip():
                    if message.get("photo") or message.get("document") or message.get("video"):
                        print("Пропущено сообщение с медиа без подписи: передача фотографий/файлов пока не реализована.")
                    continue
                try:
                    send_to_max(html.unescape(text))
                    print(f"Переслан текст Telegram update_id={update['update_id']} в MAX chat_id={MAX_CHAT_ID}")
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
