"""Qoldiq va hujjatlar keshi. Ikki ko'rinish: bazadagi (`Balances`, `Docs`) va xotiradagi (`Mem*`, qayta qurish uchun)."""
import json
from dataclasses import dataclass

from backend.ledger.db import Database
from backend.ledger.events import canonical


@dataclass
class Doc:
    doc_id: str
    kind: str
    ref: str | None
    status: str | None
    data: dict


# ---------------------------------------------------------------- xotirada
class MemBalances:
    def __init__(self):
        self._d: dict[str, int] = {}

    def get(self, key: str) -> int:
        return self._d.get(key, 0)

    def add(self, key: str, delta: int) -> int:
        self._d[key] = self._d.get(key, 0) + delta
        return self._d[key]

    def set(self, key: str, value: int) -> None:
        self._d[key] = value

    def scan(self, prefix: str) -> dict[str, int]:
        return {k: v for k, v in sorted(self._d.items()) if k.startswith(prefix) and v != 0}

    def snapshot(self) -> dict[str, int]:
        return {k: v for k, v in sorted(self._d.items()) if v != 0}


class MemDocs:
    def __init__(self):
        self._d: dict[str, Doc] = {}

    def get(self, doc_id: str) -> Doc | None:
        d = self._d.get(doc_id)
        return Doc(d.doc_id, d.kind, d.ref, d.status, json.loads(canonical(d.data))) if d else None

    def put(self, doc_id: str, kind: str, ref: str | None, status: str | None, data: dict) -> None:
        self._d[doc_id] = Doc(doc_id, kind, ref, status, json.loads(canonical(data)))

    def update(self, doc_id: str, status: str | None = None, **fields) -> Doc:
        d = self.get(doc_id)
        if d is None:
            raise KeyError(f"doc topilmadi: {doc_id}")
        d.data.update(fields)
        self.put(doc_id, d.kind, d.ref, status if status is not None else d.status, d.data)
        return self.get(doc_id)

    def query(self, kind: str, ref: str | None = None, status: str | None = None) -> list[Doc]:
        out = [self.get(i) for i in sorted(self._d)
               if self._d[i].kind == kind and (ref is None or self._d[i].ref == ref)
               and (status is None or self._d[i].status == status)]
        return out

    def snapshot(self) -> dict[str, tuple]:
        return {i: (d.kind, d.ref, d.status, canonical(d.data)) for i, d in sorted(self._d.items())}


# ---------------------------------------------------------------- bazada
class Balances:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> int:
        r = self.db.one("SELECT value FROM balances WHERE key = ?", (key,))
        return r["value"] if r else 0

    def add(self, key: str, delta: int) -> int:
        self.db.execute("INSERT INTO balances (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = balances.value + excluded.value", (key, delta))
        return self.get(key)

    def set(self, key: str, value: int) -> None:
        self.db.execute("INSERT INTO balances (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def scan(self, prefix: str) -> dict[str, int]:
        rows = self.db.query("SELECT key, value FROM balances WHERE key >= ? AND key < ? AND value != 0 ORDER BY key",
                             (prefix, prefix + "\U0010ffff"))
        return {r["key"]: r["value"] for r in rows}

    def snapshot(self) -> dict[str, int]:
        return {r["key"]: r["value"] for r in self.db.query("SELECT key, value FROM balances WHERE value != 0 ORDER BY key")}


class Docs:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r) -> Doc:
        return Doc(r["doc_id"], r["kind"], r["ref"], r["status"], json.loads(r["data"]))

    def get(self, doc_id: str) -> Doc | None:
        r = self.db.one("SELECT * FROM docs WHERE doc_id = ?", (doc_id,))
        return self._row(r) if r else None

    def put(self, doc_id: str, kind: str, ref: str | None, status: str | None, data: dict) -> None:
        self.db.execute("INSERT INTO docs (doc_id, kind, ref, status, data) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(doc_id) DO UPDATE SET kind=excluded.kind, ref=excluded.ref, "
                        "status=excluded.status, data=excluded.data",
                        (doc_id, kind, ref, status, canonical(data)))

    def update(self, doc_id: str, status: str | None = None, **fields) -> Doc:
        d = self.get(doc_id)
        if d is None:
            raise KeyError(f"doc topilmadi: {doc_id}")
        d.data.update(fields)
        self.put(doc_id, d.kind, d.ref, status if status is not None else d.status, d.data)
        return self.get(doc_id)

    def query(self, kind: str, ref: str | None = None, status: str | None = None) -> list[Doc]:
        sql, params = "SELECT * FROM docs WHERE kind = ?", [kind]
        if ref is not None:
            sql += " AND ref = ?"; params.append(ref)
        if status is not None:
            sql += " AND status = ?"; params.append(status)
        return [self._row(r) for r in self.db.query(sql + " ORDER BY doc_id", params)]

    def snapshot(self) -> dict[str, tuple]:
        return {r["doc_id"]: (r["kind"], r["ref"], r["status"], canonical(json.loads(r["data"])))
                for r in self.db.query("SELECT * FROM docs ORDER BY doc_id")}
