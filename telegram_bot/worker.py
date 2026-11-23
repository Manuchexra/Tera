"""Outbox oqimidagi xabarnomalarni Telegram egasi chatiga yetkazadi."""
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request

from backend.cache.redis_impl import RedisBackend
from backend.core.clock import SystemClock
from backend.ledger.db import DatabaseProvider
from backend.modules.sync.outbox import Outbox
from backend.modules.sync.relay import ack_messages, confirm_sent, read_messages

log = logging.getLogger("tera.telegram")
MAX_MESSAGE_LENGTH = 4096


class TelegramDeliveryError(RuntimeError):
    """Telegram API xabarni qabul qilmaganini bildiradi."""


def format_notification(kind: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    text = f"Tera | {kind}\n{body}"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 14] + "\n[kesib olindi]"
    return text


def send_message(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise TelegramDeliveryError("Telegram bot tokeni yoki chat ID berilmagan")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TelegramDeliveryError(f"Telegram API HTTP xatosi ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise TelegramDeliveryError(
            f"Telegram API so'rovida xato ({type(exc).__name__})"
        ) from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = result.get("description", "noma'lum Telegram API xatosi") \
            if isinstance(result, dict) else "Telegram javobi noto'g'ri formatda"
        raise TelegramDeliveryError(str(description))


def process_owner_messages(
    db,
    backend: RedisBackend,
    token: str,
    owner_chat_id: str,
    group: str = "telegram-owner",
    consumer: str | None = None,
    count: int = 10,
    now: str | None = None,
) -> int:
    consumer_name = consumer or f"{socket.gethostname()}-{os.getpid()}"
    messages = read_messages(backend, "owner", group, consumer_name, count)
    if not messages:
        return 0

    delivered = 0
    acknowledged = []
    for message in messages:
        outbox_id = message["outbox_id"]
        chat_id = message["recipient"] or owner_chat_id
        try:
            send_message(token, chat_id, format_notification(message["kind"], message["payload"]))
        except TelegramDeliveryError as exc:
            with db.transaction():
                Outbox(db).mark_failed(outbox_id, str(exc))
            log.error("Telegram xabari yetkazilmadi (outbox id=%s): %s", outbox_id, exc)
        else:
            confirm_sent(db, [outbox_id], now or SystemClock().now())
            delivered += 1
        acknowledged.append(message["stream_id"])

    ack_messages(backend, "owner", group, acknowledged)
    return delivered


def main() -> None:
    database_url = os.environ.get("TERA_DATABASE_URL")
    redis_url = os.environ.get("TERA_REDIS_URL")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not database_url:
        raise RuntimeError("TERA_DATABASE_URL o'rnatilmagan")
    if not redis_url:
        raise RuntimeError("TERA_REDIS_URL o'rnatilmagan; Telegram oqimi uchun Redis kerak")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN o'rnatilmagan")
    if not owner_chat_id:
        raise RuntimeError("TELEGRAM_OWNER_CHAT_ID o'rnatilmagan")

    provider = DatabaseProvider(database_url)
    backend = RedisBackend.from_url(redis_url)
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    try:
        while True:
            with provider.session() as db:
                delivered = process_owner_messages(
                    db, backend, token, owner_chat_id, consumer=consumer,
                )
            if not delivered:
                time.sleep(1)
    finally:
        backend.r.close()
        provider.close()


if __name__ == "__main__":
    main()
