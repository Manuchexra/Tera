"""Egaga outbox xabarlari va /start tasdig'ini Telegram orqali yetkazadi."""
import json
import logging
import os
import socket
import time
from typing import Any
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

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


def format_notification(kind: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    text = f"Tera | {kind}\n{body}"
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 14] + "\n[kesib olindi]"
    return text


def send_message(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise TelegramDeliveryError("Telegram bot tokeni yoki chat ID berilmagan")
    _telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text})


def fetch_updates(token: str, offset: int, timeout: int = 50) -> list[dict]:
    result = _telegram_api(
        token,
        "getUpdates",
        {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
        request_timeout=timeout + 15,
    )
    updates = result.get("result")
    if not isinstance(updates, list):
        raise TelegramDeliveryError("Telegram getUpdates javobi noto'g'ri formatda")
    return updates


def handle_updates(
    updates: list[dict],
    token: str,
    owner_chat_id: str,
    offset: int = 0,
) -> int:
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise TelegramDeliveryError("Telegram update_id noto'g'ri")
        offset = max(offset, update_id + 1)

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command != "/start":
            continue
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not isinstance(chat_id, int):
            log.warning("/start xabarida chat ID topilmadi")
            continue
        log.info("Telegram /start qabul qilindi; chat_id=%s", chat_id)
        if str(chat_id) != owner_chat_id:
            log.warning("/start boshqa chatdan keldi; sozlangan egasi chatiga mos emas")
            continue
        send_message(token, owner_chat_id, "Tera xabarnoma boti tayyor. Bildirishnomalar shu chatga keladi.")
        log.info("Ega chatidagi /start so'roviga javob yuborildi")
    return offset


def _telegram_api(
    token: str,
    method: str,
    payload: dict[str, Any],
    request_timeout: int = 10,
) -> dict:
    if not token:
        raise TelegramDeliveryError("Telegram bot tokeni berilmagan")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        retry_after = 0
        description = ""
        if exc.code == 429:
            retry_header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after = max(0, int(retry_header)) if retry_header else 0
            except ValueError:
                retry_after = 0
            try:
                error_body = json.loads(exc.read())
            except (AttributeError, TypeError, ValueError):
                error_body = {}
            body_description = error_body.get("description") if isinstance(error_body, dict) else None
            if isinstance(body_description, str):
                description = body_description.replace(token, "[maxfiy]")[:200]
            parameters = error_body.get("parameters") if isinstance(error_body, dict) else None
            if retry_after == 0 and isinstance(parameters, dict):
                body_retry_after = parameters.get("retry_after")
                if isinstance(body_retry_after, int) and body_retry_after > 0:
                    retry_after = body_retry_after
        message = f"Telegram API HTTP xatosi ({exc.code})"
        if description:
            message += f": {description}"
        raise TelegramDeliveryError(f"{method}: {message}", retry_after=retry_after) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise TelegramDeliveryError(
            f"{method}: Telegram API so'rovida xato ({type(exc).__name__})"
        ) from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = result.get("description", "noma'lum Telegram API xatosi") \
            if isinstance(result, dict) else "Telegram javobi noto'g'ri formatda"
        raise TelegramDeliveryError(str(description))
    return result


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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    provider = DatabaseProvider(database_url)
    backend = RedisBackend.from_url(redis_url)
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    offset = 0
    try:
        while True:
            updates = []
            try:
                updates = fetch_updates(token, offset)
                offset = handle_updates(updates, token, owner_chat_id, offset)
            except TelegramDeliveryError as exc:
                delay = max(5, exc.retry_after + 2 if exc.retry_after else 5)
                log.error("Telegram yangilanishlari xatosi; %s soniyadan so'ng qayta urinadi: %s",
                          delay, exc)
                time.sleep(delay)

            with provider.session() as db:
                delivered = process_owner_messages(
                    db, backend, token, owner_chat_id, consumer=consumer,
                )
            if not delivered and not updates:
                time.sleep(1)
    finally:
        backend.r.close()
        provider.close()


if __name__ == "__main__":
    main()
