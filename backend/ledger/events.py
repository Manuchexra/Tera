"""Hodisa (event) modeli va hash zanjiri.

Har bir hodisa: noyob ID, kim, qaysi qurilma, uch xil vaqt (qurilma / biznes / yozilgan), joylashuv, mazmun.
Mazmundagi pul va og'irlik faqat butun son (kasr taqiqlanadi: yaxlitlash xatosi jurnalga kirib qolmasin).
"""
import hashlib
import json
from dataclasses import dataclass, field

from backend.core.timing import parse_ts

GENESIS_HASH = "0" * 64
TIME_FLAGS = ("ok", "suspicious")


class DuplicateEvent(Exception):
    """Bu event_id jurnalda allaqachon bor. Qayta yuborish xavfsiz: hech narsa ikki marta yozilmaydi."""

    def __init__(self, existing: "Event"):
        super().__init__(f"duplicate event {existing.event_id}")
        self.existing = existing


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reject_floats(value, path="payload"):
    if isinstance(value, float):
        raise ValueError(f"{path}: kasr son taqiqlangan (pul va og'irlik butun son bo'lishi kerak)")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{path}: kalit satr bo'lishi kerak")
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_floats(v, f"{path}[{i}]")


@dataclass(frozen=True)
class NewEvent:
    event_id: str
    type: str
    actor_id: str
    payload: dict
    occurred_at: str
    time_flag: str = "ok"
    device_id: str | None = None
    device_time: str | None = None
    gps: dict | None = None
    corrects_event_id: str | None = None

    def __post_init__(self):
        if not self.event_id or len(self.event_id) > 200 or any(c.isspace() for c in self.event_id):
            raise ValueError("event_id noto'g'ri")
        if not self.type or not self.actor_id:
            raise ValueError("type va actor_id majburiy")
        if not isinstance(self.payload, dict):
            raise ValueError("payload lug'at bo'lishi kerak")
        if self.time_flag not in TIME_FLAGS:
            raise ValueError("time_flag noto'g'ri")
        parse_ts(self.occurred_at)                 # noto'g'ri sana bo'lsa ValueError
        _reject_floats(self.payload)


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    type: str
    actor_id: str
    payload: dict
    occurred_at: str
    recorded_at: str
    time_flag: str
    prev_hash: str
    hash: str
    device_id: str | None = None
    device_time: str | None = None
    gps: dict | None = None
    corrects_event_id: str | None = None


def hash_fields(new: NewEvent, recorded_at: str) -> dict:
    return {
        "event_id": new.event_id, "type": new.type, "actor_id": new.actor_id,
        "device_id": new.device_id, "device_time": new.device_time,
        "occurred_at": new.occurred_at, "recorded_at": recorded_at, "time_flag": new.time_flag,
        "gps": new.gps, "payload": new.payload, "corrects_event_id": new.corrects_event_id,
    }


def compute_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(fields)).encode("utf-8")).hexdigest()


def event_hash_fields(ev: Event) -> dict:
    return {
        "event_id": ev.event_id, "type": ev.type, "actor_id": ev.actor_id,
        "device_id": ev.device_id, "device_time": ev.device_time,
        "occurred_at": ev.occurred_at, "recorded_at": ev.recorded_at, "time_flag": ev.time_flag,
        "gps": ev.gps, "payload": ev.payload, "corrects_event_id": ev.corrects_event_id,
    }
