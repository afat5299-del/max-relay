import os
import time
import html
import requests
from dotenv import load_dotenv
from max import MaxClient

load_dotenv()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_SOURCE_CHAT_ID = os.getenv("TG_SOURCE_CHAT_ID", "").strip()
MAX_TOKEN = os.getenv("MAX_TOKEN", "").strip()
MAX_CHAT_ID = os.getenv("MAX_CHAT_ID", "").strip()

if not all([TG_BOT_TOKEN, TG_SOURCE_CHAT_ID, MAX_TOKEN, MAX_CHAT_ID]):
    raise SystemExit("Заполните TG_BOT_TOKEN, TG_SOURCE_CHAT_ID, MAX_TOKEN и MAX_CHAT_ID в файле .env")

try:
    MAX_CHAT_ID = int(MAX_CHAT_ID)
except ValueError:
    raise SystemExit("MAX_CHAT_ID должен быть числом — ID чата MAX")

TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def telegram_api(method, data=None, timeout=35):
    response = requests.post(f"{TG_API}/{method}", data=data or {}, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {result}")
    return result["result"]


def main():
    print("Подключаемся к MAX…")
    max_client = MaxClient(token=MAX_TOKEN)
    max_client.run()
    time.sleep(2)
    print("MAX подключён. Начинаем проверять сообщения Telegram.")

    # При первом запуске берём только новые сообщения, чтобы не переслать старую историю.
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
                # Пересылаем обычный текст и подпись к медиа как текст. HTML Telegram не переносим в MAX.
                try:
                    max_client.send_message(MAX_CHAT_ID, html.unescape(text))
                    print(f"Переслано Telegram update_id={update['update_id']} в MAX chat_id={MAX_CHAT_ID}")
                except Exception as exc:
                    print(f"Ошибка отправки в MAX: {exc}")
        except KeyboardInterrupt:
            print("Остановка по команде пользователя.")
            max_client.stop()
            break
        except Exception as exc:
            print(f"Ошибка цикла: {exc}. Повтор через 5 секунд.")
            time.sleep(5)


if __name__ == "__main__":
    main()
