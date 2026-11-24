import os
import time
import unittest
import uuid
from unittest import mock

from backend.balances.rebuild import verify
from backend.cache.base import Backend, LockUnavailable
from backend.cache.factory import make_backend
from backend.cache.memory import MemoryBackend
from backend.cache.redis_impl import RedisBackend
from backend.core.config import ConfigError, Settings
from backend.core.errors import DomainError
from backend.modules.auth.service import AuthService
from backend.modules.reference import queries
from backend.modules.sync import relay
from backend.modules.sync.outbox import Outbox

from .fake_redis import FakeRedis, FakeRedisError
from .helpers import SECRET, AppCase


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class BackendContract:
    """Xotiradagi va Redis realizatsiyalari bir xil qoidaga amal qilishi kerak."""

    def make(self, clock: Clock) -> Backend:
        raise NotImplementedError

    def setUp(self):
        self.clock = Clock()
        self.b = self.make(self.clock)

    # --- kesh ---
    def test_cache_get_set_delete_ttl(self):
        self.assertIsNone(self.b.get("k"))
        self.b.set("k", "v", 30)
        self.assertEqual(self.b.get("k"), "v")
        self.clock.t += 29
        self.assertEqual(self.b.get("k"), "v")
        self.clock.t += 2
        self.assertIsNone(self.b.get("k"))
        self.b.set("a", "1", 100)
        self.b.set("b", "2", 100)
        self.b.delete("a", "b", "missing")
        self.assertEqual((self.b.get("a"), self.b.get("b")), (None, None))

    # --- tezlik cheklovi ---
    def test_rate_limit_blocks_after_limit_and_resets(self):
        decisions = [self.b.hit("login:u", 3, 60) for _ in range(5)]
        self.assertEqual([d.allowed for d in decisions], [True, True, True, False, False])
        self.assertEqual([d.count for d in decisions], [1, 2, 3, 4, 5])
        self.assertTrue(1 <= decisions[3].retry_after <= 60)
        self.clock.t += 61
        self.assertTrue(self.b.hit("login:u", 3, 60).allowed)

    def test_rate_limit_keys_are_independent_and_window_is_fixed(self):
        for _ in range(3):
            self.b.hit("a", 3, 60)
        self.assertFalse(self.b.hit("a", 3, 60).allowed)
        self.assertTrue(self.b.hit("b", 3, 60).allowed)
        self.clock.t += 30                                           # oyna yarmida: hali bloklangan
        d = self.b.hit("a", 3, 60)
        self.assertFalse(d.allowed)
        self.assertTrue(28 <= d.retry_after <= 31)

    # --- qulf ---
    def test_lock_is_exclusive_and_owner_only(self):
        t1 = self.b.acquire("nightly", 60)
        self.assertIsNotNone(t1)
        self.assertIsNone(self.b.acquire("nightly", 60))
        self.assertFalse(self.b.release("nightly", "wrong-token"))
        self.assertIsNone(self.b.acquire("nightly", 60))             # begona token qulfni ochmadi
        self.assertTrue(self.b.release("nightly", t1))
        self.assertIsNotNone(self.b.acquire("nightly", 60))

    def test_lock_expires_so_crashed_owner_cannot_block_forever(self):
        t1 = self.b.acquire("nightly", 10)
        self.clock.t += 11
        t2 = self.b.acquire("nightly", 10)
        self.assertIsNotNone(t2)
        self.assertFalse(self.b.release("nightly", t1))              # eski egasi yangi qulfni ocholmaydi
        self.assertTrue(self.b.release("nightly", t2))

    def test_lock_context_manager(self):
        with self.b.lock("job", 60):
            with self.assertRaises(LockUnavailable):
                with self.b.lock("job", 60):
                    pass
        with self.assertRaises(RuntimeError):
            with self.b.lock("job", 60):
                raise RuntimeError("job xato berdi")
        self.assertIsNotNone(self.b.acquire("job", 60))             # xatodan keyin ham qulf ochilgan

    # --- oqim ---
    def test_stream_groups_deliver_each_message_once_per_group(self):
        ids = [self.b.publish("outbox:x", {"n": str(i)}) for i in range(5)]
        self.b.ensure_group("outbox:x", "bot")
        self.b.ensure_group("outbox:x", "bot")                       # ikkinchi marta xato bermaydi
        first = self.b.read("outbox:x", "bot", "c1", count=3)
        self.assertEqual([f["n"] for _, f in first], ["0", "1", "2"])
        rest = self.b.read("outbox:x", "bot", "c2", count=10)
        self.assertEqual([f["n"] for _, f in rest], ["3", "4"])
        self.assertEqual(self.b.read("outbox:x", "bot", "c1"), [])
        self.assertEqual(self.b.ack("outbox:x", "bot", [i for i, _ in first]), 3)
        self.assertEqual(self.b.ack("outbox:x", "bot", [i for i, _ in first]), 0)   # ikki marta ack qilinmaydi
        self.b.ensure_group("outbox:x", "audit")                       # boshqa guruh hammasini ko'radi
        self.assertEqual(len(self.b.read("outbox:x", "audit", "c1", count=10)), 5)
        self.assertEqual(ids, sorted(ids, key=lambda s: int(s.split("-")[0])))


class MemoryContract(BackendContract, unittest.TestCase):
    def make(self, clock):
        return MemoryBackend(clock=clock)

    def test_stream_maxlen_trims_oldest(self):
        for i in range(5):
            self.b.publish("s", {"n": str(i)}, maxlen=3)
        self.b.ensure_group("s", "g")
        self.assertEqual([f["n"] for _, f in self.b.read("s", "g", "c", 10)], ["2", "3", "4"])


class RedisContract(BackendContract, unittest.TestCase):
    def make(self, clock):
        self.fake = FakeRedis(clock)
        return RedisBackend(self.fake, errors=(FakeRedisError,))

    def test_keys_are_prefixed(self):
        self.b.set("k", "v", 10)
        self.b.hit("x", 1, 10)
        self.b.acquire("n", 10)
        self.assertTrue(all(k.startswith("tera:") for k in self.fake.data))

    def test_rate_limit_key_always_has_ttl(self):
        self.b.hit("x", 5, 60)
        self.assertGreater(self.fake.ttl("tera:rl:x"), 0)
        self.fake.data["tera:rl:y"] = ("4", None)                    # TTL siz qolgan kalit tuzatiladi
        self.b.hit("y", 5, 60)
        self.assertGreater(self.fake.ttl("tera:rl:y"), 0)


@unittest.skipUnless(os.environ.get("TERA_TEST_REDIS"), "haqiqiy Redis uchun: TERA_TEST_REDIS=redis://... o'rnating")
class RealRedisTests(unittest.TestCase):
    """Soxta Redis tekshirolmaydigan narsalar: haqiqiy buyruqlar, TTL, Lua skript, oqimlar. Haqiqiy vaqt ishlatiladi."""

    def setUp(self):
        import redis
        self.client = redis.Redis.from_url(os.environ["TERA_TEST_REDIS"], decode_responses=True)
        self.prefix = f"teratest-{uuid.uuid4().hex[:8]}:"
        self.b = RedisBackend(self.client, prefix=self.prefix)

    def tearDown(self):
        for k in self.client.scan_iter(self.prefix + "*"):
            self.client.delete(k)

    def test_cache_ttl(self):
        self.b.set("k", "v", 1)
        self.assertEqual(self.b.get("k"), "v")
        time.sleep(1.2)
        self.assertIsNone(self.b.get("k"))

    def test_rate_limit(self):
        ds = [self.b.hit("u", 3, 2) for _ in range(5)]
        self.assertEqual([d.allowed for d in ds], [True, True, True, False, False])
        self.assertTrue(1 <= ds[3].retry_after <= 2)
        time.sleep(2.2)
        self.assertTrue(self.b.hit("u", 3, 2).allowed)

    def test_lock_with_lua_release(self):
        t1 = self.b.acquire("n", 1)
        self.assertIsNone(self.b.acquire("n", 1))
        self.assertFalse(self.b.release("n", "wrong"))
        self.assertTrue(self.b.release("n", t1))
        t2 = self.b.acquire("n", 1)
        time.sleep(1.2)
        self.assertIsNotNone(self.b.acquire("n", 1))               # muddati tugagan qulf qayta olinadi
        self.assertFalse(self.b.release("n", t2))

    def test_stream_roundtrip(self):
        for i in range(3):
            self.b.publish("outbox:x", {"n": str(i)})
        self.b.ensure_group("outbox:x", "bot")
        self.b.ensure_group("outbox:x", "bot")
        got = self.b.read("outbox:x", "bot", "c1", count=10)
        self.assertEqual([f["n"] for _, f in got], ["0", "1", "2"])
        self.assertEqual(self.b.ack("outbox:x", "bot", [i for i, _ in got]), 3)
        self.assertEqual(self.b.read("outbox:x", "bot", "c1", count=10), [])


class RedisFailureTests(unittest.TestCase):
    """Redis ishlamasa, tizim to'xtamaydi va noto'g'ri natija ham bermaydi."""

    def setUp(self):
        self.clock = Clock()
        self.fake = FakeRedis(self.clock)
        self.b = RedisBackend(self.fake, errors=(FakeRedisError,))

    def test_fail_open_for_cache_and_rate_limit_fail_closed_for_lock(self):
        self.fake.fail = True
        with self.assertLogs("tera.cache", level="WARNING") as captured:
            self.assertIsNone(self.b.get("k"))                       # kesh: "topilmadi"
            self.b.set("k", "v", 10)                                 # xato ko'tarilmaydi
            self.b.delete("k")
            d = self.b.hit("login:u", 1, 60)
            self.assertTrue(d.allowed)                               # bazadagi bloklash himoya qiladi
            with self.assertRaises(LockUnavailable):
                self.b.acquire("nightly", 60)                        # qulf: berilmaydi
        self.assertIn("Redis ishlamayapti (get)", captured.output[0])
        self.assertFalse(self.b.healthy)

    def test_recovers_when_redis_returns(self):
        self.fake.fail = True
        with self.assertLogs("tera.cache", level="WARNING") as captured:
            self.b.get("k")
        self.assertIn("Redis ishlamayapti (get)", captured.output[0])
        self.assertFalse(self.b.healthy)
        self.fake.fail = False
        self.b.set("k", "v", 10)
        self.assertTrue(self.b.healthy)
        self.assertEqual(self.b.get("k"), "v")

    def test_factory(self):
        with self.assertLogs("tera.cache", level="WARNING") as captured:
            self.assertIsInstance(make_backend(None), MemoryBackend)
            self.assertIsInstance(make_backend(""), MemoryBackend)
        self.assertEqual(len(captured.output), 2)


class RelayTests(AppCase):
    def setUp(self):
        super().setUp()
        self.backend = MemoryBackend()
        self.ob = Outbox(self.env.db)
        with self.j.transaction():
            for i in range(5):
                self.ob.add("telegram_store", "receipt", f"store-{i}", {"total": 1000 * (i + 1), "izoh": "Do'kon"}, "2026-11-18T06:00:00.000Z")
            self.ob.add("owner", "alert", None, {"x": 1}, "2026-11-18T06:00:00.000Z")

    def relayed(self, channel="telegram_store"):
        return relay.relay_outbox(self.env.db, self.backend, channel, self.j.clock.now())

    def test_relay_publishes_once_and_marks(self):
        self.assertEqual(self.relayed(), 5)
        self.assertEqual(self.relayed(), 0)                          # takror uzatilmaydi
        self.assertEqual(len(self.ob.unrelayed("telegram_store")), 0)
        self.assertEqual(len(self.ob.unrelayed("owner")), 1)         # boshqa kanal tegilmagan
        self.assertEqual(len(self.ob.pending("telegram_store")), 5)  # hali tasdiqlanmagan: manba jadvalda turibdi

    def test_bot_reads_parses_acks_and_confirms(self):
        self.relayed()
        msgs = relay.read_messages(self.backend, "telegram_store", "bot", "c1", count=10)
        self.assertEqual([m["recipient"] for m in msgs], [f"store-{i}" for i in range(5)])
        self.assertEqual(msgs[1]["payload"], {"total": 2000, "izoh": "Do'kon"})
        self.assertEqual(relay.ack_messages(self.backend, "telegram_store", "bot", [m["stream_id"] for m in msgs]), 5)
        relay.confirm_sent(self.env.db, [m["outbox_id"] for m in msgs[:3]], self.j.clock.now())
        self.assertEqual([m["id"] for m in self.ob.pending("telegram_store")], [m["outbox_id"] for m in msgs[3:]])

    def test_redis_failure_midway_keeps_what_was_sent_and_retries_rest(self):
        calls = {"n": 0}
        real = self.backend.publish

        def flaky(stream, fields, maxlen=10_000):
            calls["n"] += 1
            if calls["n"] == 3:
                raise ConnectionError("redis tushib qoldi")
            return real(stream, fields, maxlen)

        with mock.patch.object(self.backend, "publish", flaky):
            with self.assertLogs("tera.relay", level="WARNING") as captured:
                self.assertEqual(self.relayed(), 2)
        self.assertIn("redis tushib qoldi", captured.output[0])
        self.assertEqual(len(self.ob.unrelayed("telegram_store")), 3)
        self.assertEqual(self.relayed(), 3)                          # tiklangach qolganlari uzatiladi
        msgs = relay.read_messages(self.backend, "telegram_store", "bot", "c", 10)
        self.assertEqual(len(msgs), 5)
        self.assertEqual(len({m["outbox_id"] for m in msgs}), 5)     # takror yo'q

    def test_requeue_stale_only_unconfirmed_old_messages(self):
        self.relayed()
        msgs = relay.read_messages(self.backend, "telegram_store", "bot", "c", 10)
        relay.confirm_sent(self.env.db, [msgs[0]["outbox_id"]], self.j.clock.now())      # bittasi yetkazilgan
        self.assertEqual(relay.requeue_stale(self.env.db, self.j.clock.now(), 600), 0)   # hali yangi
        self.env.clock.advance(minutes=11)
        self.assertEqual(relay.requeue_stale(self.env.db, self.j.clock.now(), 600), 4)   # yetkazilganidan tashqari hammasi
        self.assertEqual(self.relayed(), 4)


class SnapshotCacheTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.ok("price.set", {"product_id": self.s["product"], "price": 8_000, "reason": "boshlang'ich"})
        self.backend = MemoryBackend()

    def snap(self, **kw):
        return queries.cached_snapshot(self.backend, self.j, self.e.current_policy(), **kw)

    def test_second_call_comes_from_cache(self):
        with mock.patch.object(queries, "snapshot", wraps=queries.snapshot) as spy:
            a, b = self.snap(), self.snap()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(a, b)

    def test_price_change_invalidates_immediately(self):
        self.assertEqual(self.snap()["products"][0]["price"], 8_000)
        self.ok("price.set", {"product_id": self.s["product"], "price": 8_500, "reason": "qimmatladi"})
        self.assertEqual(self.snap()["products"][0]["price"], 8_500)         # eskirgan narx qaytmaydi

    def test_unrelated_events_keep_cache_but_version_tracks_reference_only(self):
        v = self.snap()["version"]
        self.ok("approval.grant", {"kind": "labor", "max_amount": 28_000, "reason": "x"})
        with mock.patch.object(queries, "snapshot", wraps=queries.snapshot) as spy:
            self.assertEqual(self.snap()["version"], v)
        self.assertEqual(spy.call_count, 0)
        self.ok("policy.update", {"changes": {"credit_limit": 3_000_000}, "reason": "x"})
        fresh = self.snap()
        self.assertGreater(fresh["version"], v)
        self.assertEqual(fresh["policy"]["credit_limit"], 3_000_000)

    def test_agent_keys_are_separate(self):
        everyone = self.snap()
        mine = self.snap(agent_id=self.agent_user.user_id)
        other = self.snap(agent_id="U999")
        self.assertEqual((len(everyone["stores"]), len(mine["stores"]), len(other["stores"])), (1, 1, 0))

    def test_corrupt_cache_entry_is_ignored(self):
        self.backend.set("snapshot:all", "{not json", 60)
        self.assertEqual(self.snap()["products"][0]["price"], 8_000)

    def test_works_when_redis_is_down(self):
        fake = FakeRedis(Clock())
        fake.fail = True
        down = RedisBackend(fake, errors=(FakeRedisError,))
        with self.assertLogs("tera.cache", level="WARNING") as captured:
            data = queries.cached_snapshot(down, self.j, self.e.current_policy())
        self.assertIn("Redis ishlamayapti (get)", captured.output[0])
        self.assertEqual(data["products"][0]["price"], 8_000)

    def test_cache_and_rebuild_stay_consistent(self):
        self.snap()
        self.assertTrue(verify(self.env.db).ok)


class LoginRateLimitTests(AppCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.limiter = MemoryBackend(clock=self.clock)
        self.auth = AuthService(self.j, self.env.auth.tokens, self.env.perms, limiter=self.limiter)

    def login(self, name="jasur", pw="password123", client="1.2.3.4"):
        return self.auth.login(name, pw, f"dev-{name}", client_key=client)

    def test_user_limit(self):
        for _ in range(10):
            self.login()
        with self.assertRaises(DomainError) as cm:
            self.login()
        self.assertEqual(cm.exception.code, "rate_limited")
        self.assertGreaterEqual(cm.exception.details["retry_after"], 1)
        self.login("malika", client="9.9.9.9")                       # boshqa foydalanuvchiga ta'sir qilmaydi
        self.clock.t += 61
        self.login()                                                 # oyna o'tgach yana mumkin

    def test_client_key_limit_is_enforced_by_login(self):
        self.limiter.hit("login:client:7.7.7.7", 1, 60)               # oldindan to'ldiramiz: 30 ta urinish
        for _ in range(29):
            self.limiter.hit("login:client:7.7.7.7", 30, 60)
        with self.assertRaises(DomainError) as cm:
            self.login(client="7.7.7.7")
        self.assertEqual(cm.exception.code, "rate_limited")
        self.login(client="8.8.8.8")                                 # boshqa manzil o'tadi

    def test_without_limiter_or_with_dead_redis_login_still_works(self):
        self.assertEqual(self.env.auth.login("jasur", "password123", "dev-jasur", client_key="x").user.login, "jasur")
        fake = FakeRedis(Clock())
        fake.fail = True
        auth = AuthService(self.j, self.env.auth.tokens, self.env.perms,
                           limiter=RedisBackend(fake, errors=(FakeRedisError,)))
        with self.assertLogs("tera.cache", level="WARNING") as captured:
            result = auth.login("jasur", "password123", "dev-jasur", client_key="x")
        self.assertIn("Redis ishlamayapti (hit)", captured.output[0])
        self.assertEqual(result.user.login, "jasur")

    def test_database_lockout_still_protects_when_redis_is_down(self):
        fake = FakeRedis(Clock())
        fake.fail = True
        auth = AuthService(self.j, self.env.auth.tokens, self.env.perms,
                           limiter=RedisBackend(fake, errors=(FakeRedisError,)))
        with self.assertLogs("tera.cache", level="WARNING") as captured:
            for _ in range(5):
                with self.assertRaises(DomainError):
                    auth.login("jasur", "wrong-password", "dev-jasur", client_key="x")
            with self.assertRaises(DomainError) as cm:
                auth.login("jasur", "password123", "dev-jasur", client_key="x")
        self.assertIn("Redis ishlamayapti (hit)", captured.output[0])
        self.assertEqual(cm.exception.code, "account_locked")


class SettingsTests(unittest.TestCase):
    ENV = {"TERA_SECRET": "s" * 40, "TERA_DATABASE_URL": "postgresql://tera:pw@db:5432/tera"}

    def env(self, **over):
        e = dict(self.ENV)
        e.update(over)
        return {k: v for k, v in e.items() if v is not None}

    def test_valid_and_defaults(self):
        s = Settings.from_env(self.env())
        self.assertEqual((s.redis_url, s.token_ttl_seconds, s.pool_min, s.pool_max), (None, 43200, 1, 10))
        self.assertTrue(s.is_production_db)
        s = Settings.from_env(self.env(TERA_REDIS_URL="redis://cache:6379/0", TERA_DB_POOL_MAX="20", TERA_TOKEN_TTL_SECONDS="3600"))
        self.assertEqual((s.redis_url, s.pool_max, s.token_ttl_seconds), ("redis://cache:6379/0", 20, 3600))

    def test_invalid(self):
        for over in [dict(TERA_SECRET=None), dict(TERA_SECRET="short"), dict(TERA_DATABASE_URL=None),
                     dict(TERA_DATABASE_URL="mysql://x"), dict(TERA_REDIS_URL="http://x"),
                     dict(TERA_DB_POOL_MAX="abc"), dict(TERA_DB_POOL_MIN="5", TERA_DB_POOL_MAX="2"),
                     dict(TERA_TOKEN_TTL_SECONDS="5")]:
            with self.assertRaises(ConfigError, msg=over):
                Settings.from_env(self.env(**over))

    def test_sqlite_is_allowed_but_flagged_not_production(self):
        s = Settings.from_env(self.env(TERA_DATABASE_URL="sqlite:///dev.sqlite3"))
        self.assertFalse(s.is_production_db)


if __name__ == "__main__":
    unittest.main()
