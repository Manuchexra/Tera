"""Sinov uchun soxta Redis: `RedisBackend` mantiqini (kalitlar, TTL, SET NX, Lua qulf, oqimlar) haqiqiy Redis'siz tekshiradi.
Haqiqiy Redis bilan farqlar bo'lishi mumkin: ularni docker-compose orqali haqiqiy Redis'da ham ishga tushiring."""
import math

from backend.cache.redis_impl import RELEASE_SCRIPT


class FakeRedisError(Exception):
    pass


class FakeRedis:
    def __init__(self, clock):
        self.clock = clock
        self.data: dict[str, tuple[str, float | None]] = {}
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.groups: dict[tuple[str, str], dict] = {}
        self.fail = False
        self._seq = 0

    def _check(self):
        if self.fail:
            raise FakeRedisError("connection refused")

    def _alive(self, key):
        e = self.data.get(key)
        if e is not None and e[1] is not None and self.clock() >= e[1]:
            del self.data[key]
            return None
        return e

    # --- kalit/qiymat ---
    def get(self, key):
        self._check()
        e = self._alive(key)
        return e[0] if e else None

    def set(self, key, value, ex=None, px=None, nx=False):
        self._check()
        if nx and self._alive(key) is not None:
            return None
        exp = self.clock() + ex if ex is not None else (self.clock() + px / 1000 if px is not None else None)
        self.data[key] = (str(value), exp)
        return True

    def delete(self, *keys):
        self._check()
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    def incr(self, key):
        self._check()
        e = self._alive(key)
        val = int(e[0]) + 1 if e else 1
        self.data[key] = (str(val), e[1] if e else None)
        return val

    def ttl(self, key):
        self._check()
        e = self._alive(key)
        if e is None:
            return -2
        return -1 if e[1] is None else int(math.ceil(e[1] - self.clock()))

    def expire(self, key, seconds):
        self._check()
        e = self._alive(key)
        if e is None:
            return False
        self.data[key] = (e[0], self.clock() + seconds)
        return True

    def eval(self, script, numkeys, *args):
        self._check()
        assert script == RELEASE_SCRIPT, "soxta Redis faqat qulfni ochish skriptini biladi"
        key, token = args[0], args[1]
        e = self._alive(key)
        if e and e[0] == token:
            del self.data[key]
            return 1
        return 0

    # --- oqimlar ---
    def xadd(self, name, fields, id="*", maxlen=None, approximate=True):
        self._check()
        self._seq += 1
        msg_id = f"{self._seq}-0"
        entries = self.streams.setdefault(name, [])
        entries.append((msg_id, dict(fields)))
        if maxlen is not None and len(entries) > maxlen:
            del entries[: len(entries) - maxlen]
        return msg_id

    def xgroup_create(self, name, groupname, id="$", mkstream=False):
        self._check()
        if name not in self.streams:
            if not mkstream:
                raise FakeRedisError("ERR The XGROUP subcommand requires the key to exist")
            self.streams[name] = []
        if (name, groupname) in self.groups:
            raise FakeRedisError("BUSYGROUP Consumer Group name already exists")
        self.groups[(name, groupname)] = {"delivered": 0, "pending": set()}

    def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        self._check()
        out = []
        for name, marker in streams.items():
            assert marker == ">"
            g = self.groups[(name, groupname)]
            batch = [e for e in self.streams.get(name, []) if int(e[0].split("-")[0]) > g["delivered"]]
            if count:
                batch = batch[:count]
            for msg_id, _ in batch:
                g["delivered"] = max(g["delivered"], int(msg_id.split("-")[0]))
                g["pending"].add(msg_id)
            if batch:
                out.append([name, [(i, dict(f)) for i, f in batch]])
        return out

    def xack(self, name, groupname, *ids):
        self._check()
        g = self.groups.get((name, groupname))
        if not g:
            return 0
        n = len(set(ids) & g["pending"])
        g["pending"] -= set(ids)
        return n
