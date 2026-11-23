"""Outbox -> Redis oqimi -> bot.

Haqiqiy manba `outbox` jadvali. Redis oqimi faqat tez yetkazish uchun. Yetkazish kamida bir marta (at-least-once):
xabar ikki marta kelishi mumkin, shuning uchun bot `id` bo'yicha takrorlarni tashlab yuboradi.
"""
import json
import logging

from backend.cache.base import Backend
from backend.core.timing import fmt_ts, parse_ts
from datetime import timedelta

from .outbox import Outbox

log = logging.getLogger("tera.relay")


def stream_name(channel: str) -> str:
    return f"outbox:{channel}"


def relay_outbox(db, backend: Backend, channel: str, now: str, limit: int = 100) -> int:
    """Hali uzatilmagan xabarlarni Redis oqimiga qo'yadi. Nechta xabar uzatilganini qaytaradi.

    Redis o'rtada ishlamay qolsa, shu paytgacha uzatilganlar belgilanadi, qolganlari keyingi safar uzatiladi."""
    ob = Outbox(db)
    done: list[int] = []
    with db.transaction():
        for row in ob.unrelayed(channel, limit):
            try:
                backend.publish(stream_name(channel), {
                    "id": str(row["id"]), "kind": row["kind"], "recipient": row["recipient"] or "",
                    "payload": json.dumps(row["payload"], ensure_ascii=False, sort_keys=True),
                    "created_at": row["created_at"]})
            except Exception as e:
                log.warning("Oqimga uzatib bo'lmadi (outbox id=%s): %s", row["id"], e)
                break
            done.append(row["id"])
        ob.mark_relayed(done, now)
    return len(done)


def requeue_stale(db, now: str, older_than_seconds: int = 600) -> int:
    """Uzatilgan, lekin bot tasdiqlamagan xabarlarni qayta uzatishga qaytaradi (Redis xotirasini yo'qotgan bo'lishi mumkin)."""
    cutoff = fmt_ts(parse_ts(now) - timedelta(seconds=older_than_seconds))
    with db.transaction():
        return Outbox(db).reset_stale_relays(cutoff)


def read_messages(backend: Backend, channel: str, group: str, consumer: str, count: int = 10) -> list[dict]:
    """Bot tomoni: oqimdan xabarlarni o'qiydi. Har bir element: stream_id, outbox_id, kind, recipient, payload."""
    stream = stream_name(channel)
    backend.ensure_group(stream, group)
    out = []
    for stream_id, f in backend.read(stream, group, consumer, count):
        out.append({"stream_id": stream_id, "outbox_id": int(f["id"]), "kind": f["kind"],
                    "recipient": f.get("recipient") or None, "payload": json.loads(f["payload"]),
                    "created_at": f["created_at"]})
    return out


def ack_messages(backend: Backend, channel: str, group: str, stream_ids: list[str]) -> int:
    return backend.ack(stream_name(channel), group, stream_ids)


def confirm_sent(db, outbox_ids: list[int], now: str) -> None:
    """Bot xabarni Telegramga yuborgach tasdiqlaydi: `sent_at` qo'yiladi va xabar navbatdan chiqadi."""
    with db.transaction():
        Outbox(db).mark_sent(outbox_ids, now)
