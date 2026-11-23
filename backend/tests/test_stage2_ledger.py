import os
import tempfile
import unittest

from backend.balances import keys
from backend.balances.projection import UnknownEventType, no_effect, projects
from backend.balances.rebuild import repair, verify
from backend.balances.store import Balances, Docs, MemBalances, MemDocs
from backend.core.clock import FixedClock
from backend.ledger.counters import new_doc_id
from backend.ledger.db import SqliteDatabase
from backend.tests.dbtest import open_test_db
from backend.ledger.events import DuplicateEvent, NewEvent
from backend.ledger.journal import Journal

T0 = "2026-11-18T06:00:00.000Z"


# ---- faqat testlar uchun projeksiyalar (haqiqiy hodisa turlari keyingi bosqichlarda) ----
@projects("test.deposit")
def _p_dep(ev, bal, docs):
    bal.add(f"acct:{ev.payload['acct']}", ev.payload["amount"])
    docs.put(f"dep:{ev.event_id}", "dep", ev.payload["acct"], "open", {"amount": ev.payload["amount"]})


@projects("test.close")
def _p_close(ev, bal, docs):
    docs.update(f"dep:{ev.payload['dep']}", status="closed", closed_by=ev.actor_id)


no_effect("test.noop")


def ev(n, type="test.deposit", acct="A", amount=100, **kw):
    payload = kw.pop("payload", {"acct": acct, "amount": amount})
    return NewEvent(event_id=f"e{n}", type=type, actor_id="u1", payload=payload, occurred_at=T0, **kw)


def make(path=None):
    db = SqliteDatabase(path) if path else open_test_db()        # fayl yo'li berilsa, faqat SQLite (fayl testi uchun)
    return db, Journal(db, FixedClock(T0))


class LedgerAppendTests(unittest.TestCase):
    def setUp(self):
        self.db, self.j = make()

    def rec(self, *events):
        with self.j.transaction():
            return [self.j.record(e) for e in events]

    def test_seq_and_hash_chain(self):
        a, b, c = self.rec(ev(1), ev(2), ev(3))
        self.assertEqual((a.seq, b.seq, c.seq), (1, 2, 3))
        self.assertEqual(b.prev_hash, a.hash)
        self.assertEqual(c.prev_hash, b.hash)
        rep = self.j.ledger.verify_chain()
        self.assertTrue(rep.ok)
        self.assertEqual(rep.checked, 3)

    def test_balance_and_doc_updated_with_event(self):
        self.rec(ev(1, amount=100), ev(2, amount=50))
        self.assertEqual(self.j.bal.get("acct:A"), 150)
        self.assertEqual(self.j.docs.get("dep:e1").data, {"amount": 100})

    def test_duplicate_event_is_noop(self):
        self.rec(ev(1, amount=100))
        with self.assertRaises(DuplicateEvent) as cm:
            self.rec(ev(1, amount=999))                       # boshqa mazmun, lekin shu event_id
        self.assertEqual(cm.exception.existing.seq, 1)
        self.assertEqual(self.j.ledger.count(), 1)
        self.assertEqual(self.j.bal.get("acct:A"), 100)       # birinchi yozuv qoladi

    def test_append_outside_transaction_refused(self):
        with self.assertRaises(RuntimeError):
            self.j.ledger.append(ev(1), T0)

    def test_update_and_delete_blocked_by_database(self):
        self.rec(ev(1))
        with self.assertRaises(self.db.integrity_errors):
            self.db.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
        with self.assertRaises(self.db.integrity_errors):
            self.db.execute("DELETE FROM events WHERE seq = 1")
        self.assertEqual(self.j.ledger.count(), 1)

    def test_tampering_is_detected(self):
        self.rec(ev(1), ev(2), ev(3))
        with self.db.guards_off():                            # buzg'unchi himoyani olib tashladi
            self.db.execute("""UPDATE events SET payload = '{"acct":"A","amount":1}' WHERE seq = 2""")
        rep = self.j.ledger.verify_chain()
        self.assertFalse(rep.ok)
        self.assertEqual(rep.broken_seq, 2)
        self.assertIn("hash", rep.reason)

    def test_deleted_row_is_detected(self):
        self.rec(ev(1), ev(2), ev(3))
        with self.db.guards_off():
            self.db.execute("DELETE FROM events WHERE seq = 2")
        rep = self.j.ledger.verify_chain()
        self.assertFalse(rep.ok)
        self.assertEqual(rep.broken_seq, 3)

    def test_corrects_event_must_exist(self):
        self.rec(ev(1))
        self.rec(ev(2, corrects_event_id="e1"))
        with self.assertRaises(ValueError):
            self.rec(ev(3, corrects_event_id="nope"))

    def test_failed_transaction_rolls_everything_back(self):
        with self.assertRaises(RuntimeError):
            with self.j.transaction():
                self.j.record(ev(1))
                raise RuntimeError("boom")
        self.assertEqual(self.j.ledger.count(), 0)
        self.assertEqual(self.j.bal.get("acct:A"), 0)
        self.assertIsNone(self.j.docs.get("dep:e1"))

    def test_unknown_event_type_is_not_recorded(self):
        with self.assertRaises(UnknownEventType):
            with self.j.transaction():
                self.j.record(ev(1))
                self.j.record(ev(2, type="test.unknown", payload={}))
        self.assertEqual(self.j.ledger.count(), 0)            # hech biri yozilmadi

    def test_nested_savepoint_keeps_outer_work(self):
        with self.j.transaction():
            self.j.record(ev(1))
            try:
                with self.j.transaction():
                    self.j.record(ev(2))
                    raise ValueError("inner fails")
            except ValueError:
                pass
            self.j.record(ev(3))
        self.assertEqual([e.event_id for e in self.j.ledger.iter_events()], ["e1", "e3"])
        self.assertTrue(self.j.ledger.verify_chain().ok)

    def test_duplicate_inside_transaction_keeps_earlier_events(self):
        self.rec(ev(1))
        with self.j.transaction():
            self.j.record(ev(2))
            with self.assertRaises(DuplicateEvent):
                self.j.record(ev(1))
        self.assertEqual(self.j.ledger.count(), 2)

    def test_between_filters_by_business_time(self):
        self.rec(ev(1), NewEvent("e2", "test.noop", "u2", {}, "2026-11-19T06:00:00.000Z"))
        got = self.j.ledger.between("2026-11-18T00:00:00.000Z", "2026-11-19T00:00:00.000Z")
        self.assertEqual([e.event_id for e in got], ["e1"])
        got = self.j.ledger.between("2026-11-18T00:00:00.000Z", "2026-11-20T00:00:00.000Z", actor_id="u2")
        self.assertEqual([e.event_id for e in got], ["e2"])


class EventValidationTests(unittest.TestCase):
    def test_floats_in_payload_rejected(self):
        with self.assertRaises(ValueError):
            ev(1, payload={"amount": 1.5})
        with self.assertRaises(ValueError):
            ev(1, payload={"lines": [{"qty": 2.0}]})

    def test_gps_may_hold_floats(self):
        e = ev(1, gps={"lat": 41.3111, "lon": 69.2797, "mock": False})
        self.assertEqual(e.gps["lat"], 41.3111)

    def test_bad_ids_and_times_rejected(self):
        for bad in ["", "has space", "x" * 201]:
            with self.assertRaises(ValueError):
                NewEvent(bad, "test.noop", "u1", {}, T0)
        with self.assertRaises(ValueError):
            NewEvent("e1", "test.noop", "u1", {}, "kecha")
        with self.assertRaises(ValueError):
            NewEvent("e1", "test.noop", "u1", {}, T0, time_flag="weird")

    def test_unicode_payload_roundtrip(self):
        db, j = make()
        note = "Do'kon: oʻzbekcha, русский, English"
        with j.transaction():
            j.record(ev(1, type="test.noop", payload={"note": note}))
        self.assertEqual(j.ledger.get("e1").payload["note"], note)
        self.assertTrue(j.ledger.verify_chain().ok)


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.db, self.j = make()
        with self.j.transaction():
            self.j.record(ev(1, acct="A", amount=100))
            self.j.record(ev(2, acct="B", amount=40))
            self.j.record(ev(3, acct="A", amount=-30))
            self.j.record(ev(4, type="test.close", payload={"dep": "e1"}))

    def test_cache_equals_rebuild(self):
        rep = verify(self.db)
        self.assertTrue(rep.ok, (rep.balance_diffs, rep.doc_diffs))
        self.assertEqual(rep.events, 4)
        self.assertEqual(self.j.bal.get("acct:A"), 70)
        self.assertEqual(self.j.docs.get("dep:e1").status, "closed")

    def test_corrupted_cache_is_reported_and_repaired(self):
        self.db.execute("UPDATE balances SET value = 999 WHERE key = 'acct:A'")
        self.db.execute("DELETE FROM docs WHERE doc_id = 'dep:e2'")
        rep = verify(self.db)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.balance_diffs, [("acct:A", 999, 70)])
        self.assertEqual([d[0] for d in rep.doc_diffs], ["dep:e2"])
        fixed = repair(self.db)
        self.assertTrue(fixed.ok)
        self.assertEqual(self.j.bal.get("acct:A"), 70)

    def test_repair_refuses_when_chain_broken(self):
        with self.db.guards_off():
            self.db.execute("""UPDATE events SET payload = '{"acct":"A","amount":5}' WHERE seq = 1""")
        with self.assertRaises(RuntimeError):
            repair(self.db)

    def test_rebuild_is_deterministic(self):
        a = verify(self.db)
        b = verify(self.db)
        self.assertEqual((a.ok, a.events), (b.ok, b.events))


class CacheStoreTests(unittest.TestCase):
    def test_scan_prefix_and_zero_dropped(self):
        db, _ = make()
        bal = Balances(db)
        with db.transaction():
            bal.add(keys.custody("u1", "P50", "B1"), 10)
            bal.add(keys.custody("u1", "P25", "B1"), 5)
            bal.add(keys.custody("u2", "P50", "B1"), 7)
            bal.add(keys.custody("u1", "P50", "B2"), 3)
            bal.add(keys.custody("u1", "P50", "B2"), -3)      # nolga tushdi
        got = bal.scan(keys.prefix("custody", "u1"))
        self.assertEqual(got, {"custody:u1:P25:B1": 5, "custody:u1:P50:B1": 10})
        self.assertNotIn("custody:u1:P50:B2", bal.snapshot())

    def test_db_and_memory_implementations_agree(self):
        db, _ = make()
        pairs = [(Balances(db), Docs(db)), (MemBalances(), MemDocs())]
        with db.transaction():
            for bal, docs in pairs:
                bal.add("k:1", 5); bal.add("k:1", -2); bal.set("k:2", 9)
                docs.put("d1", "sale", "S1", "open", {"x": 1, "y": [1, 2]})
                docs.put("d2", "sale", "S2", "open", {"x": 2})
                docs.update("d1", status="closed", z="я")
        (b1, d1), (b2, d2) = pairs
        self.assertEqual(b1.snapshot(), b2.snapshot())
        self.assertEqual(d1.snapshot(), d2.snapshot())
        self.assertEqual([d.doc_id for d in d1.query("sale", status="open")], ["d2"])
        self.assertEqual([d.doc_id for d in d2.query("sale", ref="S1")], ["d1"])

    def test_update_missing_doc_raises(self):
        db, _ = make()
        with db.transaction(), self.assertRaises(KeyError):
            Docs(db).update("nope", status="x")


class CounterTests(unittest.TestCase):
    def test_numbers_reset_per_local_day(self):
        db, _ = make()
        with db.transaction():
            self.assertEqual(new_doc_id(db, "SL", "2026-11-18T06:00:00.000Z"), "SL-261118-001")
            self.assertEqual(new_doc_id(db, "SL", "2026-11-18T07:00:00.000Z"), "SL-261118-002")
            self.assertEqual(new_doc_id(db, "PO", "2026-11-18T07:00:00.000Z"), "PO-261118-001")
            # 19:30 UTC = Toshkentda ertasi kun
            self.assertEqual(new_doc_id(db, "SL", "2026-11-18T19:30:00.000Z"), "SL-261119-001")

    def test_requires_transaction(self):
        db, _ = make()
        with self.assertRaises(RuntimeError):
            new_doc_id(db, "SL", T0)


class FilePersistenceTests(unittest.TestCase):
    def test_reopen_keeps_chain_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tera.sqlite3")
            db, j = make(path)
            with j.transaction():
                j.record(ev(1, amount=100))
                j.record(ev(2, amount=25))
            self.assertEqual(db.one("PRAGMA journal_mode")[0], "wal")
            versions_before = db.one("SELECT COUNT(*) AS c FROM schema_version")["c"]
            db.close()

            db2, j2 = make(path)                              # migratsiya qayta ishlamaydi
            self.assertEqual(db2.one("SELECT COUNT(*) AS c FROM schema_version")["c"], versions_before)
            self.assertEqual(j2.ledger.count(), 2)
            with j2.transaction():
                e3 = j2.record(ev(3, amount=5))
            self.assertEqual(e3.seq, 3)
            self.assertTrue(verify(db2).ok)
            self.assertEqual(j2.bal.get("acct:A"), 130)
            db2.close()


if __name__ == "__main__":
    unittest.main()
