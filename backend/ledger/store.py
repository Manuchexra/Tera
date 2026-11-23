"""Hodisalar jurnali: faqat qo'shish, hash zanjiri, takroriy yuborishdan himoya."""
import json
from dataclasses import dataclass
from typing import Iterator

from .db import Database
from .events import (GENESIS_HASH, DuplicateEvent, Event, NewEvent, canonical, compute_hash,
                     event_hash_fields, hash_fields)


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    broken_seq: int | None = None
    reason: str | None = None


def _row_to_event(r) -> Event:
    return Event(
        seq=r["seq"], event_id=r["event_id"], type=r["type"], actor_id=r["actor_id"],
        payload=json.loads(r["payload"]), occurred_at=r["occurred_at"], recorded_at=r["recorded_at"],
        time_flag=r["time_flag"], prev_hash=r["prev_hash"], hash=r["hash"],
        device_id=r["device_id"], device_time=r["device_time"],
        gps=json.loads(r["gps"]) if r["gps"] else None, corrects_event_id=r["corrects_event_id"],
    )


class Ledger:
    def __init__(self, db: Database):
        self.db = db

    def append(self, new: NewEvent, recorded_at: str) -> Event:
        """Jurnal oxiriga hodisa qo'shadi. Faqat tranzaksiya ichida (zanjir va yozuv bir butun bo'lishi uchun)."""
        if not self.db.in_transaction:
            raise RuntimeError("Ledger.append tranzaksiya ichida chaqirilishi kerak")
        existing = self.get(new.event_id)
        if existing is not None:
            raise DuplicateEvent(existing)
        if new.corrects_event_id and self.get(new.corrects_event_id) is None:
            raise ValueError(f"tuzatilayotgan hodisa topilmadi: {new.corrects_event_id}")
        prev = self.db.one("SELECT hash FROM events ORDER BY seq DESC LIMIT 1")
        prev_hash = prev["hash"] if prev else GENESIS_HASH
        h = compute_hash(prev_hash, hash_fields(new, recorded_at))
        seq = self.db.insert_id(
            "INSERT INTO events (event_id, type, actor_id, device_id, device_time, occurred_at, recorded_at, "
            "time_flag, gps, payload, corrects_event_id, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new.event_id, new.type, new.actor_id, new.device_id, new.device_time, new.occurred_at, recorded_at,
             new.time_flag, canonical(new.gps) if new.gps else None, canonical(new.payload),
             new.corrects_event_id, prev_hash, h), "seq")
        return Event(seq=seq, event_id=new.event_id, type=new.type, actor_id=new.actor_id,
                     payload=new.payload, occurred_at=new.occurred_at, recorded_at=recorded_at,
                     time_flag=new.time_flag, prev_hash=prev_hash, hash=h, device_id=new.device_id,
                     device_time=new.device_time, gps=new.gps, corrects_event_id=new.corrects_event_id)

    def get(self, event_id: str) -> Event | None:
        r = self.db.one("SELECT * FROM events WHERE event_id = ?", (event_id,))
        return _row_to_event(r) if r else None

    def iter_events(self, after_seq: int = 0, types: tuple[str, ...] | None = None) -> Iterator[Event]:
        sql, params = "SELECT * FROM events WHERE seq > ?", [after_seq]
        if types:
            sql += " AND type IN (%s)" % ",".join("?" * len(types))
            params += list(types)
        sql += " ORDER BY seq"
        for r in self.db.iterate(sql, params):
            yield _row_to_event(r)

    def between(self, start_ts: str, end_ts: str, actor_id: str | None = None) -> list[Event]:
        """`occurred_at` [start, end) oralig'idagi hodisalar (kun hisobotlari uchun)."""
        sql, params = "SELECT * FROM events WHERE occurred_at >= ? AND occurred_at < ?", [start_ts, end_ts]
        if actor_id:
            sql += " AND actor_id = ?"
            params.append(actor_id)
        sql += " ORDER BY seq"
        return [_row_to_event(r) for r in self.db.query(sql, params)]

    def count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS c FROM events")["c"]

    def last_seq(self) -> int:
        return self.db.one("SELECT COALESCE(MAX(seq), 0) AS m FROM events")["m"]

    def last_seq_of(self, types: tuple[str, ...]) -> int:
        """Berilgan turdagi hodisalarning oxirgisining tartib raqami (kesh versiyasi uchun)."""
        ph = ",".join("?" * len(types))
        return self.db.one(f"SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE type IN ({ph})", tuple(types))["m"]

    def verify_chain(self) -> ChainReport:
        """Butun zanjirni qayta hisoblaydi. Biror yozuv o'zgargan yoki o'chirilgan bo'lsa, birinchi buzilgan joyni topadi."""
        prev_hash, checked = GENESIS_HASH, 0
        for ev in self.iter_events():
            if ev.prev_hash != prev_hash:
                return ChainReport(False, checked, ev.seq, "prev_hash zanjirdan uzilgan (yozuv o'chirilgan yoki qo'shilgan)")
            if compute_hash(ev.prev_hash, event_hash_fields(ev)) != ev.hash:
                return ChainReport(False, checked, ev.seq, "hash mos emas (yozuv o'zgartirilgan)")
            prev_hash, checked = ev.hash, checked + 1
        return ChainReport(True, checked)
