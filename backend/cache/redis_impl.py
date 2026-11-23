"""Haqiqiy Redis realizatsiyasi (redis-py). Redis ishlamay qolsa, tizim to'xtamaydi:
kesh "topilmadi" deydi, tezlik cheklovi ruxsat beradi (bazadagi login bloklash baribir ishlaydi), qulf esa xato beradi."""
import logging
import uuid

from .base import Backend, LockUnavailable, RateDecision

log = logging.getLogger("tera.cache")

# Qulfni faqat egasi (token mos kelsa) ochadi. Atomik bo'lishi uchun Lua.
RELEASE_SCRIPT = ("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end")


class RedisBackend(Backend):
    def __init__(self, client, prefix: str = "tera:", errors: tuple = None):
        self.r = client
        self.p = prefix
        if errors is None:
            from redis.exceptions import RedisError
            errors = (RedisError, OSError)
        self._errors = errors
        self._healthy = True

    @classmethod
    def from_url(cls, url: str) -> "RedisBackend":
        import redis
        return cls(redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2))

    @property
    def healthy(self) -> bool:
        return self._healthy

    def _fail(self, what: str, exc: Exception) -> None:
        if self._healthy:
            log.warning("Redis ishlamayapti (%s): %s", what, exc)
        self._healthy = False

    def _ok(self) -> None:
        self._healthy = True

    # kesh
    def get(self, key):
        try:
            v = self.r.get(self.p + key)
            self._ok()
            return v
        except self._errors as e:
            self._fail("get", e)
            return None

    def set(self, key, value, ttl_seconds):
        try:
            self.r.set(self.p + key, value, ex=ttl_seconds)
            self._ok()
        except self._errors as e:
            self._fail("set", e)

    def delete(self, *keys):
        try:
            if keys:
                self.r.delete(*[self.p + k for k in keys])
            self._ok()
        except self._errors as e:
            self._fail("delete", e)

    # tezlik cheklovi: kalit har doim TTL bilan yaratiladi (SET NX EX), keyin INCR
    def hit(self, key, limit, window_seconds):
        k = f"{self.p}rl:{key}"
        try:
            self.r.set(k, 0, ex=window_seconds, nx=True)
            count = int(self.r.incr(k))
            ttl = int(self.r.ttl(k))
            if ttl < 0:                                           # kalit TTL siz qolgan bo'lsa tuzatiladi
                self.r.expire(k, window_seconds)
                ttl = window_seconds
            self._ok()
        except self._errors as e:
            self._fail("hit", e)
            return RateDecision(True, 0, 0)                       # ochiq: bazadagi bloklash baribir himoya qiladi
        if count > limit:
            return RateDecision(False, count, max(1, ttl))
        return RateDecision(True, count, 0)

    # qulf
    def acquire(self, name, ttl_seconds):
        token = uuid.uuid4().hex
        try:
            ok = self.r.set(f"{self.p}lock:{name}", token, nx=True, px=int(ttl_seconds * 1000))
            self._ok()
        except self._errors as e:
            self._fail("acquire", e)
            raise LockUnavailable(f"{name}: Redis ishlamayapti") from e
        return token if ok else None

    def release(self, name, token):
        try:
            res = self.r.eval(RELEASE_SCRIPT, 1, f"{self.p}lock:{name}", token)
            self._ok()
            return bool(res)
        except self._errors as e:
            self._fail("release", e)
            return False

    # oqim
    def publish(self, stream, fields, maxlen=10_000):
        return self.r.xadd(self.p + stream, fields, maxlen=maxlen, approximate=True)

    def ensure_group(self, stream, group):
        try:
            self.r.xgroup_create(self.p + stream, group, id="0", mkstream=True)
        except Exception as e:                                    # BUSYGROUP: guruh allaqachon bor
            if "BUSYGROUP" not in str(e):
                raise

    def read(self, stream, group, consumer, count=10):
        resp = self.r.xreadgroup(group, consumer, {self.p + stream: ">"}, count=count)
        out = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                out.append((msg_id, dict(fields)))
        return out

    def ack(self, stream, group, ids):
        return int(self.r.xack(self.p + stream, group, *ids)) if ids else 0
