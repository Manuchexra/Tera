import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from backend.balances.store import Balances
from backend.core.clock import FixedClock
from backend.ledger.db import (ALL_TABLES, PG_LOCK_KEY, DatabaseProvider, PostgresDatabase, SqliteDatabase,
                               connect)
from backend.ledger.events import NewEvent
from backend.ledger.journal import Journal
from backend.ledger.schema import MIGRATIONS, POSTGRES_DIR, postgres_migrations
from backend.ledger.sqlutil import to_pg
from backend.balances.projection import projects

from backend.balances.rebuild import verify
from .dbtest import is_postgres, open_test_db, test_db_url

T0 = "2026-11-18T06:00:00.000Z"


@projects("test4b.deposit")
def _p(ev, bal, docs):
    bal.add(f"acct:{ev.payload['acct']}", ev.payload["amount"])


def deposit(n, amount=10):
    return NewEvent(f"d{n}", "test4b.deposit", "u1", {"acct": "A", "amount": amount}, T0)


class SqlTranslationTests(unittest.TestCase):
    def test_placeholders(self):
        self.assertEqual(to_pg("SELECT * FROM t WHERE a = ? AND b = ?"), "SELECT * FROM t WHERE a = %s AND b = %s")

    def test_percent_escaped(self):
        self.assertEqual(to_pg("SELECT 1 WHERE x LIKE '50%' AND y = ?"), "SELECT 1 WHERE x LIKE '50%%' AND y = %s")
        self.assertEqual(to_pg("SELECT 5 % 2"), "SELECT 5 %% 2")

    def test_question_mark_inside_string_untouched(self):
        self.assertEqual(to_pg("SELECT 'why?' , ?"), "SELECT 'why?' , %s")
        self.assertEqual(to_pg("SELECT 'it''s ?', ?"), "SELECT 'it''s ?', %s")

    def test_real_statements_from_codebase(self):
        s = "INSERT INTO balances (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = balances.value + excluded.value"
        self.assertEqual(to_pg(s).count("%s"), 2)
        self.assertNotIn("?", to_pg(s))


class ConnectTests(unittest.TestCase):
    def test_memory_and_file_urls(self):
        for url in ("sqlite://:memory:", "sqlite://"):
            db = connect(url)
            self.assertEqual(db.dialect, "sqlite")
            db.close()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.sqlite3")
            db = connect(f"sqlite:///{path}")                 # sqlite:////mutlaq/yo'l
            self.assertEqual(db.path, path)
            db.close()
            old = os.getcwd()
            os.chdir(d)
            try:
                db = connect("sqlite:///nisbiy.sqlite3")      # sqlite:///nisbiy
                self.assertEqual(db.path, "nisbiy.sqlite3")
                db.close()
                self.assertTrue(Path("nisbiy.sqlite3").exists())
            finally:
                os.chdir(old)

    def test_unknown_scheme(self):
        with self.assertRaises(ValueError):
            connect("mysql://x/y")


class SchemaParityTests(unittest.TestCase):
    """PostgreSQL migratsiyalari SQLite bilan jadval, ustun, indeks va triggerlari bo'yicha mos bo'lishi shart.
    PostgreSQL bu yerda ishga tushmaydi, shuning uchun DDL matni tahlil qilinadi."""

    @classmethod
    def setUpClass(cls):
        cls.sqlite = SqliteDatabase(":memory:")
        cls.pg_sql = "\n".join(sql for _, sql in postgres_migrations())

    @classmethod
    def _pg_tables(cls):
        tables = {}
        for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", cls.pg_sql, re.S):
            cols = set()
            for line in m.group(2).split("\n"):
                line = re.sub(r"--.*$", "", line).strip()
                if not line:
                    continue
                first = line.split()[0].rstrip(",")
                if first.upper() not in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                    cols.add(first)
            tables[m.group(1)] = cols
        for m in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", cls.pg_sql):
            tables[m.group(1)].add(m.group(2))
        return tables

    def test_same_versions(self):
        self.assertEqual([v for v, _ in MIGRATIONS], [v for v, _ in postgres_migrations()])
        self.assertEqual(sorted(p.name[:3] for p in POSTGRES_DIR.glob("*.sql")), ["001", "002", "003"])

    def test_same_tables_and_columns(self):
        pg = self._pg_tables()
        sqlite_tables = {r["name"] for r in self.sqlite.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('sqlite_sequence', 'schema_version')")}
        self.assertEqual(sqlite_tables, set(pg), "jadvallar to'plami mos emas")
        self.assertEqual(sqlite_tables, set(ALL_TABLES))
        for t in sqlite_tables:
            cols = {r["name"] for r in self.sqlite.query(f"PRAGMA table_info({t})")}
            self.assertEqual(cols, pg[t], f"{t} jadvali ustunlari mos emas")

    def test_same_indexes(self):
        sqlite_idx = {r["name"] for r in self.sqlite.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
        pg_idx = set(re.findall(r"CREATE INDEX (\w+)", self.pg_sql))
        self.assertEqual(sqlite_idx, pg_idx)

    def test_ledger_guards_exist_in_postgres(self):
        sqlite_trg = {r["name"] for r in self.sqlite.query("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        pg_trg = set(re.findall(r"CREATE TRIGGER (\w+)", self.pg_sql))
        self.assertTrue(sqlite_trg <= pg_trg)
        self.assertIn("events_no_truncate", pg_trg)             # PostgreSQL'da TRUNCATE ham to'siladi
        self.assertIn("ERRCODE = 'integrity_constraint_violation'", self.pg_sql)

    def test_postgres_specifics(self):
        self.assertNotIn("AUTOINCREMENT", self.pg_sql)
        code = "\n".join(re.sub(r"--.*$", "", l) for l in self.pg_sql.split("\n"))        # izohlarsiz
        self.assertEqual(code.count('COLLATE "C"'), 2)         # balances.key va docs.doc_id
        self.assertNotIn("JSONB", code.upper())                # hodisa mazmuni MATN: hash aynan shu matnga tayanadi
        for line in code.split("\n"):
            if re.match(r"\s*value\s", line) or re.match(r"\s*seq\s", line) or re.match(r"\s*id\s", line):
                self.assertIn("BIGINT", line.upper(), line)     # 32 bitli INTEGER pul yig'indisiga yetmaydi


class _FakeCursor:
    def __init__(self, conn):
        self.conn, self.rows, self.rowcount = conn, [], 0

    def execute(self, sql, params=None):
        self.conn.log.append((sql, params))
        self.rows = self.conn.respond(sql, params)
        self.rowcount = len(self.rows)
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _FakeTx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.level = self.conn.depth
        self.conn.depth += 1
        self.conn.log.append(("BEGIN" if self.level == 0 else "SAVEPOINT", None))
        return self

    def __exit__(self, et, ev, tb):
        self.conn.depth -= 1
        end = ("COMMIT" if self.level == 0 else "RELEASE") if et is None else ("ROLLBACK" if self.level == 0 else "ROLLBACK TO")
        self.conn.log.append((end, None))
        return False


class _FakeConn:
    def __init__(self):
        self.log, self.depth, self.version, self.dbname = [], 0, 0, "tera_test"

    def respond(self, sql, params):
        if "MAX(version)" in sql:
            return [{"v": self.version}]
        if "current_database" in sql:
            return [{"n": self.dbname}]
        if "RETURNING" in sql:
            return [{"seq": 42, "id": 42}]
        if sql.startswith("INSERT INTO schema_version"):
            self.version = params[0]
        return []

    def cursor(self):
        return _FakeCursor(self)

    def execute(self, sql, params=None):
        return _FakeCursor(self).execute(sql, params)

    def transaction(self):
        return _FakeTx(self)

    def statements(self):
        return [s for s, _ in self.log]


class PostgresGlueTests(unittest.TestCase):
    """PostgresDatabase ning psycopg bilan bog'lanish mantiqi (soxta ulanish bilan): SQL tarjimasi, tranzaksiya, qulf."""

    def setUp(self):
        self.conn = _FakeConn()
        self.db = PostgresDatabase(self.conn)

    def test_query_translation_and_params(self):
        self.db.query("SELECT * FROM t WHERE a = ? AND b LIKE '5%'", (1,))
        self.assertEqual(self.conn.log[-1], ("SELECT * FROM t WHERE a = %s AND b LIKE '5%%'", (1,)))
        self.db.one("SELECT 1")
        self.assertEqual(self.conn.log[-1], ("SELECT 1", None))             # parametrsiz: formatlash yo'q

    def test_insert_id_uses_returning(self):
        n = self.db.insert_id("INSERT INTO outbox (kind) VALUES (?)", ("x",), "id")
        self.assertEqual(n, 42)
        self.assertEqual(self.conn.log[-1], ("INSERT INTO outbox (kind) VALUES (%s) RETURNING id", ("x",)))

    def test_outer_transaction_takes_advisory_lock_once(self):
        with self.db.transaction():
            self.assertTrue(self.db.in_transaction)
            self.db.execute("INSERT INTO t VALUES (?)", (1,))
            with self.db.transaction():                                      # ichma-ich: savepoint, qulf takrorlanmaydi
                self.db.execute("INSERT INTO t VALUES (?)", (2,))
        self.assertFalse(self.db.in_transaction)
        st = self.conn.statements()
        self.assertEqual(st, ["BEGIN", "SELECT pg_advisory_xact_lock(%s)", "INSERT INTO t VALUES (%s)",
                              "SAVEPOINT", "INSERT INTO t VALUES (%s)", "RELEASE", "COMMIT"])
        self.assertEqual(self.conn.log[1][1], (PG_LOCK_KEY,))

    def test_rollback_propagates_and_resets_depth(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                with self.db.transaction():
                    raise RuntimeError("boom")
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.conn.statements()[-2:], ["ROLLBACK TO", "ROLLBACK"])

    def test_migrate_applies_files_in_order_once(self):
        self.assertEqual(self.db.migrate(), 3)
        scripts = [p for s, p in self.conn.log if s.startswith("INSERT INTO schema_version")]
        self.assertEqual(scripts, [(1,), (2,), (3,)])
        scripts_run = [s for s in self.conn.statements() if "CREATE TABLE events" in s or "CREATE TABLE users" in s
                       or "ADD COLUMN relayed_at" in s]
        self.assertEqual(len(scripts_run), 3)
        self.assertIn("CREATE TABLE events", scripts_run[0])
        self.assertIn("CREATE TABLE users", scripts_run[1])
        self.assertIn("ALTER TABLE outbox ADD COLUMN relayed_at", scripts_run[2])
        self.conn.log.clear()
        self.assertEqual(self.db.migrate(), 3)                              # qayta: hech narsa bajarilmaydi
        self.assertFalse([s for s in self.conn.statements() if "CREATE TABLE events" in s])

    def test_reset_refuses_non_test_database(self):
        self.conn.dbname = "tera_prod"
        with self.assertRaises(RuntimeError):
            self.db.reset_for_tests()
        self.assertFalse([s for s in self.conn.statements() if "TRUNCATE" in s])

    def test_reset_truncates_all_tables_with_guards_off(self):
        self.db.reset_for_tests()
        st = self.conn.statements()
        trunc = [s for s in st if s.startswith("TRUNCATE")][0]
        for t in ALL_TABLES:
            self.assertIn(t, trunc)
        self.assertIn("RESTART IDENTITY", trunc)
        self.assertLess(st.index("ALTER TABLE events DISABLE TRIGGER USER"), st.index(trunc))
        self.assertEqual(st[-1], "ALTER TABLE events ENABLE TRIGGER USER")

    def test_guards_restored_even_on_error(self):
        with self.assertRaises(ValueError):
            with self.db.guards_off():
                raise ValueError("x")
        self.assertEqual(self.conn.statements()[-1], "ALTER TABLE events ENABLE TRIGGER USER")


class DatabaseBehaviourTests(unittest.TestCase):
    """Tanlangan bazada (SQLite yoki TERA_TEST_DB bo'yicha PostgreSQL) bir xil bo'lishi kerak xulq-atvor."""

    def setUp(self):
        self.db = open_test_db()
        self.j = Journal(self.db, FixedClock(T0))

    def test_reset_clears_everything_and_restarts_sequence(self):
        with self.j.transaction():
            self.j.record(deposit(1))
            self.j.record(deposit(2))
        self.db.reset_for_tests()
        for t in ALL_TABLES:
            self.assertEqual(self.db.one(f"SELECT COUNT(*) AS c FROM {t}")["c"], 0, t)
        with self.j.transaction():
            self.assertEqual(self.j.record(deposit(3)).seq, 1)

    def test_event_id_unique_in_database_itself(self):
        with self.j.transaction():
            ev = self.j.record(deposit(1))
        with self.assertRaises(self.db.integrity_errors):
            with self.db.transaction():
                self.db.execute(
                    "INSERT INTO events (event_id, type, actor_id, occurred_at, recorded_at, time_flag, payload, "
                    "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("d1", "x", "u", T0, T0, "ok", "{}", "p", "h"))
        self.assertEqual(self.j.ledger.count(), 1)
        self.assertEqual(ev.seq, 1)

    def test_big_numbers_fit(self):
        """Pul yig'indisi 32 bitdan katta bo'lishi mumkin (PostgreSQL'da INTEGER yetmaydi)."""
        bal = Balances(self.db)
        with self.db.transaction():
            bal.add("stat:big", 5_000_000_000)
            bal.add("stat:big", 5_000_000_000)
        self.assertEqual(bal.get("stat:big"), 10_000_000_000)

    def test_prefix_scan_is_binary_ordered(self):
        bal = Balances(self.db)
        with self.db.transaction():
            for k in ["custody:U1:P2:B1", "custody:U1:P10:B1", "custody:U1:P1:B1", "custody:U10:P1:B1", "Custody:U1:P1:B1"]:
                bal.add(k, 1)
        got = list(bal.scan("custody:U1:"))
        # Baytlar tartibi ("0" belgisi ":" dan oldin): P10 < P1: < P2. PostgreSQL'da COLLATE "C" aynan shuni beradi.
        self.assertEqual(got, ["custody:U1:P10:B1", "custody:U1:P1:B1", "custody:U1:P2:B1"])   # U10 va Custody kirmaydi

    def test_nested_transactions_and_lock_semantics(self):
        with self.db.transaction():
            self.j.record(deposit(1))
            with self.assertRaises(RuntimeError):
                with self.db.transaction():
                    self.j.record(deposit(2))
                    raise RuntimeError("inner")
            self.j.record(deposit(3))
        self.assertEqual([e.event_id for e in self.j.ledger.iter_events()], ["d1", "d3"])


class ConcurrencyTests(unittest.TestCase):
    """Bir nechta ulanish bir vaqtda yozganda ham zanjir butun, qoldiq to'g'ri bo'lishi shart.
    SQLite'da fayl bazada, PostgreSQL'da (TERA_TEST_DB) tranzaksiya boshidagi qulf bilan ta'minlanadi."""

    THREADS, PER_THREAD = 4, 25

    def _hammer(self, open_conn):
        errors = []

        def worker(tid):
            try:
                db = open_conn()
                j = Journal(db, FixedClock(T0))
                for i in range(self.PER_THREAD):
                    with j.transaction():
                        j.record(NewEvent(f"t{tid}-{i}", "test4b.deposit", f"u{tid}", {"acct": "A", "amount": 1}, T0))
                db.close()
            except Exception as e:                                  # pragma: no cover
                errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def _check(self, db):
        total = self.THREADS * self.PER_THREAD
        j = Journal(db, FixedClock(T0))
        self.assertEqual(j.ledger.count(), total)
        self.assertTrue(j.ledger.verify_chain().ok)                 # zanjir uzilmagan: har yozuv oldingisiga bog'langan
        self.assertEqual(j.bal.get("acct:A"), total)                # birorta yozuv yo'qolmagan yoki ikki marta sanalmagan
        rep = verify(db)
        self.assertTrue(rep.ok, (rep.balance_diffs, rep.doc_diffs))

    def test_sqlite_file_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.sqlite3")
            SqliteDatabase(path).close()                            # migratsiya oldindan
            self._hammer(lambda: SqliteDatabase(path))
            db = SqliteDatabase(path)
            self._check(db)
            db.close()

    @unittest.skipUnless(is_postgres(), "PostgreSQL uchun: TERA_TEST_DB o'rnating")
    def test_postgres_concurrent_writers(self):
        db = open_test_db()
        self._hammer(lambda: connect(test_db_url()))
        self._check(db)


class ProviderTests(unittest.TestCase):
    def test_memory_provider_shares_connection(self):
        p = DatabaseProvider("sqlite://:memory:")
        with p.session() as a, p.session() as b:
            self.assertIs(a, b)
        p.close()

    def test_file_provider_gives_independent_connections_on_same_data(self):
        with tempfile.TemporaryDirectory() as d:
            p = DatabaseProvider(f"sqlite:///{os.path.join(d, 't.sqlite3')}")
            with p.session() as db:
                j = Journal(db, FixedClock(T0))
                with j.transaction():
                    j.record(deposit(1, 7))
            with p.session() as db2:
                self.assertEqual(Balances(db2).get("acct:A"), 7)
            p.close()


if __name__ == "__main__":
    unittest.main()
