"""Baza qatlami: bitta interfeys, ikki realizatsiya.

* PostgreSQL: ishlab chiqarish uchun (psycopg 3).
* SQLite:     tezkor sinov va ishlab chiqish uchun (tashqi dastursiz). Ishlab chiqarishda ishlatilmaydi.

Muhim kafolat (ikkalasida bir xil): har bir yozuvchi tranzaksiya BIRMA-BIR bajariladi.
SQLite'da buni `BEGIN IMMEDIATE`, PostgreSQL'da tranzaksiya boshidagi `pg_advisory_xact_lock` ta'minlaydi.
Bu hash zanjiri butunligi va biznes qoidalari (qoldiq manfiy bo'lmasligi, qarz limiti) uchun shart:
ikki buyruq bir vaqtda bir xil qoldiqni "ko'rib", ikkalasi ham o'tib ketmasligi kerak.

SQL `?` parametrlari bilan yoziladi; PostgreSQL uchun avtomatik `%s` ga o'giriladi.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import MIGRATIONS, postgres_migrations
from .sqlutil import to_pg

# Barcha jadvallar (testlarda tozalash uchun). Tartib: bog'liqlar oldin.
ALL_TABLES = ("outbox", "sync_inbox", "devices", "users", "counters", "docs", "balances", "events")
LEDGER_TRIGGERS = ("events_no_update", "events_no_delete")
PG_LOCK_KEY = 7_201_126                     # advisory lock kaliti: barcha yozuvchi tranzaksiyalar uchun bitta


class Database:
    """Umumiy mantiq: ichma-ich tranzaksiya hisobi, yordamchi so'rovlar."""

    dialect = "base"

    def __init__(self):
        self._depth = 0

    # --- interfeys (realizatsiyalar yozadi) ---
    def execute(self, sql: str, params=()): raise NotImplementedError
    def query(self, sql: str, params=()) -> list: raise NotImplementedError
    def one(self, sql: str, params=()): raise NotImplementedError
    def iterate(self, sql: str, params=()) -> Iterator: raise NotImplementedError
    def insert_id(self, sql: str, params, id_column: str) -> int: raise NotImplementedError
    def transaction(self): raise NotImplementedError
    def migrate(self) -> int: raise NotImplementedError
    def reset_for_tests(self) -> None: raise NotImplementedError
    def guards_off(self): raise NotImplementedError
    def close(self) -> None: raise NotImplementedError

    @property
    def integrity_errors(self) -> tuple:
        raise NotImplementedError

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0


# ====================================================================== SQLite
class SqliteDatabase(Database):
    dialect = "sqlite"

    def __init__(self, path: str = ":memory:"):
        super().__init__()
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)   # tranzaksiyani o'zimiz boshqaramiz
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")        # elektr o'chsa ham yozilgan hodisa yo'qolmaydi
        self.migrate()

    @property
    def integrity_errors(self) -> tuple:
        return (sqlite3.IntegrityError,)

    def migrate(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] or 0
        for version, script in MIGRATIONS:
            if version > current:
                with self.transaction():
                    for stmt in _split_script(script):
                        self.conn.execute(stmt)
                    self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                current = version
        return current

    @contextmanager
    def transaction(self):
        """Tashqi chaqiruv `BEGIN IMMEDIATE` (yozuvchi yagona), ichkisi savepoint."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp{self._depth}")
        level = self._depth
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth = level
            if level == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO sp{level}")
                self.conn.execute(f"RELEASE sp{level}")
            raise
        else:
            self._depth = level
            if level == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE sp{level}")

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def iterate(self, sql, params=()):
        yield from self.conn.execute(sql, params)

    def insert_id(self, sql, params, id_column):
        return self.conn.execute(sql, params).lastrowid

    def reset_for_tests(self) -> None:
        with self.guards_off():
            for t in ALL_TABLES:
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.execute("DELETE FROM sqlite_sequence")

    @contextmanager
    def guards_off(self):
        """Faqat testlar uchun: jurnal himoyasi (triggerlar) vaqtincha o'chiriladi, chiqishda qaytariladi."""
        rows = self.conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
        for r in rows:
            self.conn.execute(f"DROP TRIGGER {r['name']}")
        try:
            yield
        finally:
            for r in rows:
                self.conn.execute(r["sql"])

    def close(self) -> None:
        self.conn.close()


# ====================================================================== PostgreSQL
class PostgresDatabase(Database):
    """psycopg 3 ulanishi (autocommit=True, row_factory=dict_row). Tranzaksiyani psycopg boshqaradi:
    tashqi `conn.transaction()` — BEGIN/COMMIT, ichkisi — SAVEPOINT."""

    dialect = "postgresql"

    def __init__(self, conn):
        super().__init__()
        self.conn = conn

    @classmethod
    def connect(cls, url: str) -> "PostgresDatabase":
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:                                      # pragma: no cover
            raise RuntimeError("PostgreSQL uchun 'psycopg[binary,pool]' o'rnatilishi kerak "
                               "(pip install -r backend/requirements.txt)") from e
        db = cls(psycopg.connect(url, autocommit=True, row_factory=dict_row))
        db.migrate()
        return db

    @property
    def integrity_errors(self) -> tuple:
        import psycopg
        return (psycopg.IntegrityError,)

    def _cur(self, sql: str, params):
        cur = self.conn.cursor()
        cur.execute(to_pg(sql), tuple(params) if params else None)
        return cur

    def execute(self, sql, params=()):
        return self._cur(sql, params)

    def query(self, sql, params=()):
        return self._cur(sql, params).fetchall()

    def one(self, sql, params=()):
        return self._cur(sql, params).fetchone()

    def iterate(self, sql, params=()):
        yield from self._cur(sql, params)

    def insert_id(self, sql, params, id_column):
        return self._cur(f"{sql} RETURNING {id_column}", params).fetchone()[id_column]

    @contextmanager
    def transaction(self):
        with self.conn.transaction():
            if self._depth == 0:
                # Barcha yozuvchi tranzaksiyalar ketma-ket: SQLite'dagi BEGIN IMMEDIATE bilan bir xil kafolat.
                self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (PG_LOCK_KEY,))
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1

    def migrate(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for version, script in postgres_migrations():
            if version > current:
                with self.transaction():
                    self.conn.execute(script)                          # butun fayl (funksiya tanalaridagi `;` bilan)
                    self.conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
                current = version
        return current

    def reset_for_tests(self) -> None:
        name = self.conn.execute("SELECT current_database() AS n").fetchone()["n"]
        if not name.endswith("_test"):
            raise RuntimeError(f"reset_for_tests faqat '*_test' bazada ishlaydi (hozirgi: {name})")
        with self.guards_off():
            self.conn.execute(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")

    @contextmanager
    def guards_off(self):
        """Faqat testlar uchun: jurnal himoyasi vaqtincha o'chiriladi, chiqishda qaytariladi."""
        self.conn.execute("ALTER TABLE events DISABLE TRIGGER USER")
        try:
            yield
        finally:
            self.conn.execute("ALTER TABLE events ENABLE TRIGGER USER")

    def close(self) -> None:
        self.conn.close()


# ====================================================================== ulanish
def connect(url: str) -> Database:
    """URL bo'yicha ulanadi:  postgresql://user:pass@host/db  |  sqlite:///yo'l/fayl.sqlite3  |  sqlite://:memory:"""
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresDatabase.connect(url)
    if url.startswith("sqlite://"):
        rest = url[len("sqlite://"):]
        if rest in (":memory:", "/:memory:", ""):
            return SqliteDatabase(":memory:")
        return SqliteDatabase(rest[1:] if rest.startswith("/") else rest)     # sqlite:///nisbiy  |  sqlite:////mutlaq
    raise ValueError(f"Noma'lum baza URL: {url.split('://')[0]}://...")


class DatabaseProvider:
    """So'rovlar uchun alohida `Database` beradi (FastAPI har bir so'rovga o'z ulanishini oladi).

    PostgreSQL: ulanishlar puli (psycopg_pool). SQLite fayl: har safar yangi ulanish.
    SQLite xotira: bitta umumiy ulanish (faqat bitta oqimli testlar uchun)."""

    def __init__(self, url: str, pool_min: int = 1, pool_max: int = 10):
        self.url = url
        self._pool = None
        self._shared: Database | None = None
        if url.startswith(("postgresql://", "postgres://")):
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as e:                                  # pragma: no cover
                raise RuntimeError("PostgreSQL uchun 'psycopg[binary,pool]' o'rnatilishi kerak") from e
            self._pool = ConnectionPool(url, min_size=pool_min, max_size=pool_max, open=True,
                                        kwargs={"autocommit": True, "row_factory": dict_row})
            with self.session() as db:
                db.migrate()
        else:
            if url in ("sqlite://:memory:", "sqlite://"):
                self._shared = connect(url)
            else:
                connect(url).close()                                   # migratsiyani bir marta ishga tushiradi

    @contextmanager
    def session(self):
        if self._pool is not None:
            with self._pool.connection() as conn:
                yield PostgresDatabase(conn)
        elif self._shared is not None:
            yield self._shared
        else:
            db = connect(self.url)
            try:
                yield db
            finally:
                db.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
        if self._shared is not None:
            self._shared.close()


def _split_script(script: str) -> list[str]:
    """SQLite uchun: `;` bo'yicha bo'ladi, lekin TRIGGER ... END; bloklarini butun qoldiradi."""
    out, buf, in_trigger = [], [], False
    for line in script.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if s.upper().endswith("END;"):
                out.append("\n".join(buf)); buf = []; in_trigger = False
        elif s.endswith(";"):
            out.append("\n".join(buf)); buf = []
    if buf:
        out.append("\n".join(buf))
    return out
