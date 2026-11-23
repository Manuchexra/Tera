"""Hujjat raqamlari: SL-261118-029 ko'rinishida (prefiks, yymmdd, tartib raqami)."""
from backend.core.timing import local_date

from .db import Database


def next_number(db: Database, name: str) -> int:
    if not db.in_transaction:
        raise RuntimeError("next_number tranzaksiya ichida chaqirilishi kerak")
    db.execute("INSERT INTO counters (name, value) VALUES (?, 1) "
               "ON CONFLICT(name) DO UPDATE SET value = counters.value + 1", (name,))
    return db.one("SELECT value FROM counters WHERE name = ?", (name,))["value"]


def new_doc_id(db: Database, prefix: str, occurred_at: str) -> str:
    d = local_date(occurred_at)
    stamp = f"{d:%y%m%d}"
    return f"{prefix}-{stamp}-{next_number(db, f'{prefix}:{stamp}'):03d}"
