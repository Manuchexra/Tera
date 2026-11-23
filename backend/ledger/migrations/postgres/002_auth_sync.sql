-- Foydalanuvchilar, qurilmalar, rad etilganlar qutisi, xabarlar navbati.

CREATE TABLE users (
    user_id        TEXT PRIMARY KEY,
    login          TEXT NOT NULL UNIQUE,
    full_name      TEXT NOT NULL,
    phone          TEXT,
    pw_hash        TEXT NOT NULL,
    tasks          TEXT NOT NULL,
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

CREATE TABLE outbox (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    recipient  TEXT,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX outbox_pending_idx ON outbox (channel, sent_at, id);
