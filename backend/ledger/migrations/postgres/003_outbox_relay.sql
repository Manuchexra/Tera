-- Xabar Redis oqimiga uzatilgan vaqt (bot tasdiqlaguncha sent_at bo'sh qoladi).
ALTER TABLE outbox ADD COLUMN relayed_at TEXT;
