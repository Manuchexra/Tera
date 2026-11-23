"""Rad etilgan va konfliktga tushgan buyruqlar (sync_inbox).

Qoida: server qurilmadagi asl yozuvni hech qachon jim o'chirmaydi va "oxirgi yozgan yutadi" ishlatmaydi.
Buyruq nega o'tmaganini (kod, tafsilot) va asl mazmunini saqlaydi; egasi ko'rib chiqib, kerak bo'lsa
yangi (bog'langan) buyruq yuborish yo'lini tanlaydi.
"""
import json

from backend.ledger.db import Database


class Inbox:
    def __init__(self, db: Database):
        self.db = db

    def get(self, event_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM sync_inbox WHERE event_id = ?", (event_id,))
        return self._row(r) if r else None

    def add(self, cmd_json: dict, actor_id: str, device_id: str | None, status: str, code: str,
            message: str, details: dict, received_at: str) -> None:
        self.db.execute(
            "INSERT INTO sync_inbox (event_id, actor_id, device_id, type, status, code, message, details, command, received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cmd_json["event_id"], actor_id, device_id, cmd_json["type"], status, code, message,
             json.dumps(details, ensure_ascii=False, sort_keys=True),
             json.dumps(cmd_json, ensure_ascii=False, sort_keys=True), received_at))

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.query("SELECT * FROM sync_inbox WHERE status = ? ORDER BY received_at, event_id", (status,))
        else:
            rows = self.db.query("SELECT * FROM sync_inbox ORDER BY received_at, event_id")
        return [self._row(r) for r in rows]

    def resolve(self, event_id: str, by: str, resolution: str, at: str) -> None:
        cur = self.db.execute(
            "UPDATE sync_inbox SET status = 'resolved', resolved_at = ?, resolved_by = ?, resolution = ? "
            "WHERE event_id = ? AND status IN ('rejected', 'conflict')", (at, by, resolution, event_id))
        if cur.rowcount == 0:
            raise KeyError(event_id)

    @staticmethod
    def _row(r) -> dict:
        return {"event_id": r["event_id"], "actor_id": r["actor_id"], "device_id": r["device_id"], "type": r["type"],
                "status": r["status"], "code": r["code"], "message": r["message"],
                "details": json.loads(r["details"] or "{}"), "command": json.loads(r["command"]),
                "received_at": r["received_at"], "resolved_at": r["resolved_at"],
                "resolved_by": r["resolved_by"], "resolution": r["resolution"]}
