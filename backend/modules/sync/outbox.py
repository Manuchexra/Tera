"""Xabarlar navbati. Backend xabarni shu yerga yozadi, Telegram bot olib ketadi (backend botga to'g'ridan-to'g'ri bog'lanmaydi).

Xabar buyruq bilan bir tranzaksiyada yoziladi: buyruq rad etilsa, xabar ham yozilmaydi.
Kanallar: 'telegram_store' (do'konga: chek, to'lov, qaytim), 'owner' (egasiga ogohlantirish).

Yetkazish ikki bosqichli (haqiqiy manba har doim shu jadval, Redis emas):
  relayed_at - xabar Redis oqimiga qo'yildi (bot tezroq oladi)
  sent_at    - bot qabul qilganini tasdiqladi
Redis xotirasini yo'qotsa, `reset_stale_relays` tasdiqlanmagan xabarlarni qayta uzatishga qaytaradi.
"""
import json

from backend.ledger.db import Database


class Outbox:
    def __init__(self, db: Database):
        self.db = db

    def add(self, channel: str, kind: str, recipient: str | None, payload: dict, created_at: str) -> int:
        return self.db.insert_id(
            "INSERT INTO outbox (channel, kind, recipient, payload, created_at) VALUES (?,?,?,?,?)",
            (channel, kind, recipient, json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at), "id")

    def pending(self, channel: str, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT * FROM outbox WHERE channel = ? AND sent_at IS NULL ORDER BY id LIMIT ?",
                             (channel, limit))
        return [self._row(r) for r in rows]

    def unrelayed(self, channel: str, limit: int = 100) -> list[dict]:
        rows = self.db.query("SELECT * FROM outbox WHERE channel = ? AND sent_at IS NULL AND relayed_at IS NULL "
                             "ORDER BY id LIMIT ?", (channel, limit))
        return [self._row(r) for r in rows]

    def mark_relayed(self, ids: list[int], at: str) -> None:
        for i in ids:
            self.db.execute("UPDATE outbox SET relayed_at = ? WHERE id = ?", (at, i))

    def reset_stale_relays(self, older_than: str) -> int:
        """`older_than` dan oldin uzatilgan, lekin bot tasdiqlamagan xabarlarni qayta uzatishga qaytaradi."""
        cur = self.db.execute("UPDATE outbox SET relayed_at = NULL WHERE sent_at IS NULL AND relayed_at IS NOT NULL "
                              "AND relayed_at < ?", (older_than,))
        return cur.rowcount

    def mark_sent(self, ids: list[int], sent_at: str) -> None:
        for i in ids:
            self.db.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (sent_at, i))

    def mark_failed(self, id_: int, error: str) -> None:
        self.db.execute("UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?", (error[:500], id_))

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "channel": r["channel"], "kind": r["kind"], "recipient": r["recipient"],
                "payload": json.loads(r["payload"]), "created_at": r["created_at"], "sent_at": r["sent_at"],
                "relayed_at": r["relayed_at"], "attempts": r["attempts"], "last_error": r["last_error"]}
