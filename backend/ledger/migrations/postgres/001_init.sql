-- Jurnal, qoldiq keshi, hujjatlar keshi, hujjat raqamlari.
-- SQLite varianti bilan (schema.py MIGRATION_1) bir xil jadval va ustunlar bo'lishi shart.

CREATE TABLE events (
    seq               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id          TEXT NOT NULL UNIQUE,
    type              TEXT NOT NULL,
    actor_id          TEXT NOT NULL,
    device_id         TEXT,
    device_time       TEXT,
    occurred_at       TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    time_flag         TEXT NOT NULL CHECK (time_flag IN ('ok', 'suspicious')),
    gps               TEXT,
    payload           TEXT NOT NULL,       -- kanonik JSON MATN sifatida: hash aynan shu matnga tayanadi (JSONB kalit tartibini o'zgartiradi)
    corrects_event_id TEXT,
    prev_hash         TEXT NOT NULL,
    hash              TEXT NOT NULL
);
CREATE INDEX events_type_idx     ON events (type, seq);
CREATE INDEX events_occurred_idx ON events (occurred_at);
CREATE INDEX events_actor_idx    ON events (actor_id, occurred_at);

-- Jurnal faqat qo'shiladi: o'zgartirish, o'chirish va TRUNCATE bazaning o'zida to'sib qo'yilgan.
CREATE FUNCTION ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger is append-only: % forbidden', TG_OP USING ERRCODE = 'integrity_constraint_violation';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER events_no_update   BEFORE UPDATE   ON events FOR EACH ROW       EXECUTE FUNCTION ledger_append_only();
CREATE TRIGGER events_no_delete   BEFORE DELETE   ON events FOR EACH ROW       EXECUTE FUNCTION ledger_append_only();
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();

-- COLLATE "C": kalit bo'yicha prefiks qidiruvi (custody:U002:...) tilga bog'liq tartibdan mustaqil bo'lsin.
CREATE TABLE balances (
    key   TEXT COLLATE "C" PRIMARY KEY,
    value BIGINT NOT NULL
);

CREATE TABLE docs (
    doc_id TEXT COLLATE "C" PRIMARY KEY,
    kind   TEXT NOT NULL,
    ref    TEXT,
    status TEXT,
    data   TEXT NOT NULL
);
CREATE INDEX docs_kind_idx ON docs (kind, ref, status);

CREATE TABLE counters (
    name  TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);
