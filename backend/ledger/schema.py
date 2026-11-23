"""Baza sxemasi va migratsiyalar. Har bir bosqich yangi migratsiya qo'shadi, eskisi o'zgarmaydi.

Ikki parallel to'plam bor va ular bir-biriga to'liq mos bo'lishi shart:
* SQLite (sinov uchun): shu faylda, `MIGRATIONS`.
* PostgreSQL (ishlab chiqarish): `migrations/postgres/NNN_nom.sql` fayllarida.
Moslikni `tests/test_stage4b_database.py` (sxema mosligi testi) nazorat qiladi.
"""
import re
from pathlib import Path

POSTGRES_DIR = Path(__file__).resolve().parent / "migrations" / "postgres"


MIGRATION_1 = """
CREATE TABLE events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,         -- qurilma yaratgan noyob ID: takroriy yuborishdan himoya
    type              TEXT NOT NULL,
    actor_id          TEXT NOT NULL,
    device_id         TEXT,
    device_time       TEXT,                         -- qurilma soati (yozib qo'yiladi, hisobda ishlatilmaydi)
    occurred_at       TEXT NOT NULL,                -- biznes vaqti (timing.resolve_occurred_at)
    recorded_at       TEXT NOT NULL,                -- serverga yozilgan vaqt
    time_flag         TEXT NOT NULL CHECK (time_flag IN ('ok', 'suspicious')),
    gps               TEXT,                         -- JSON yoki NULL
    payload           TEXT NOT NULL,                -- kanonik JSON
    corrects_event_id TEXT,                         -- tuzatish asl hodisani o'chirmaydi, unga bog'lanadi
    prev_hash         TEXT NOT NULL,
    hash              TEXT NOT NULL
);
CREATE INDEX events_type_idx     ON events (type, seq);
CREATE INDEX events_occurred_idx ON events (occurred_at);
CREATE INDEX events_actor_idx    ON events (actor_id, occurred_at);

-- Jurnal faqat qo'shiladi: o'zgartirish va o'chirish bazaning o'zida to'sib qo'yilgan.
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: update forbidden'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: delete forbidden'); END;

-- Qoldiq keshi: hisoblangan, qo'lda o'zgartirilmaydi, istalgan paytda jurnaldan qayta quriladi.
CREATE TABLE balances (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- Hujjatlar keshi (sotuv, o'tkazma, qaytim, partiya...): holati jurnal hodisalaridan hisoblanadi.
CREATE TABLE docs (
    doc_id TEXT PRIMARY KEY,
    kind   TEXT NOT NULL,
    ref    TEXT,
    status TEXT,
    data   TEXT NOT NULL
);
CREATE INDEX docs_kind_idx ON docs (kind, ref, status);

-- Hujjat raqamlari (SL-261118-029): jurnalga kirmaydi, raqam hodisa ichida saqlanadi.
CREATE TABLE counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


MIGRATION_2 = """
CREATE TABLE users (
    user_id        TEXT PRIMARY KEY,
    login          TEXT NOT NULL UNIQUE,
    full_name      TEXT NOT NULL,
    phone          TEXT,
    pw_hash        TEXT NOT NULL,
    tasks          TEXT NOT NULL,                 -- JSON ro'yxat: ["agent", "warehouse"]
    active         INTEGER NOT NULL DEFAULT 1,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE devices (
    device_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users (user_id),
    label      TEXT,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0
);

-- Rad etilgan yoki konfliktga tushgan buyruqlar asl holida saqlanadi: hech qachon jim yo'qolmaydi.
CREATE TABLE sync_inbox (
    event_id    TEXT PRIMARY KEY,
    actor_id    TEXT NOT NULL,
    device_id   TEXT,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('rejected', 'conflict', 'resolved')),
    code        TEXT NOT NULL,
    message     TEXT,
    details     TEXT,
    command     TEXT NOT NULL,
    received_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution  TEXT
);
CREATE INDEX sync_inbox_status_idx ON sync_inbox (status, received_at);

-- Telegram bot va egasiga ketadigan xabarlar navbati.
CREATE TABLE outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,                     -- 'telegram_store' | 'owner'
    kind       TEXT NOT NULL,
    recipient  TEXT,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX outbox_pending_idx ON outbox (channel, sent_at, id);
"""

MIGRATION_3 = """
-- Xabar Redis oqimiga uzatilgan vaqt (bot tasdiqlaguncha `sent_at` bo'sh qoladi).
ALTER TABLE outbox ADD COLUMN relayed_at TEXT;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1),
    (2, MIGRATION_2),
    (3, MIGRATION_3),
]


def postgres_migrations() -> list[tuple[int, str]]:
    """`001_nom.sql` ko'rinishidagi fayllarni raqam bo'yicha tartiblab qaytaradi."""
    out = []
    for f in sorted(POSTGRES_DIR.glob("*.sql")):
        m = re.match(r"^(\d+)_", f.name)
        if not m:
            raise ValueError(f"Migratsiya fayl nomi noto'g'ri: {f.name}")
        out.append((int(m.group(1)), f.read_text(encoding="utf-8")))
    return out
