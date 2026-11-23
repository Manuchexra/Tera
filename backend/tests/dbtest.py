"""Testlar uchun baza tanlash.

    TERA_TEST_DB=postgresql://tera:tera@localhost:5432/tera_test  -> PostgreSQL (har testdan oldin jadvallar tozalanadi)
    (o'rnatilmagan)                                                -> SQLite xotirada

Bir xil testlar ikkala bazada o'tishi shart: bu PostgreSQL bilan mosligini tekshiradi.
"""
import os

from backend.ledger.db import Database, connect

_PG_CACHE: dict[str, Database] = {}


def test_db_url() -> str | None:
    url = os.environ.get("TERA_TEST_DB", "").strip()
    return url or None


def is_postgres() -> bool:
    url = test_db_url()
    return bool(url and url.startswith(("postgres://", "postgresql://")))


def open_test_db() -> Database:
    url = test_db_url()
    if is_postgres():
        db = _PG_CACHE.get(url)
        if db is None:
            db = _PG_CACHE[url] = connect(url)                 # ulanish va migratsiya bir marta
        db.reset_for_tests()
        return db
    return connect(url or "sqlite://:memory:")
