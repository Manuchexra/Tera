"""Xotiradagi realizatsiya: testlar va Redis sozlanmagan bitta jarayonli ishlab chiqish uchun.
Ko'p jarayonli ishlab chiqarishda ishlatilmaydi (jarayonlar orasida bo'lishilmaydi)."""
import threading
import time
import uuid

from .base import Backend, RateDecision


class MemoryBackend(Backend):
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._kv: dict[str, tuple[str, float | None]] = {}
        self._rates: dict[str, tuple[int, float]] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._groups: dict[tuple[str, str], dict] = {}
        self._seq = 0

    # kesh
    def get(self, key):
        with self._lock:
            item = self._kv.get(key)
            if item is None:
                return None
            value, exp = item
            if exp is not None and self._clock() >= exp:
                del self._kv[key]
                return None
            return value

    def set(self, key, value, ttl_seconds):
        with self._lock:
            self._kv[key] = (value, self._clock() + ttl_seconds)

    def delete(self, *keys):
        with self._lock:
            for k in keys:
                self._kv.pop(k, None)

    # tezlik cheklovi
    def hit(self, key, limit, window_seconds):
        with self._lock:
            now = self._clock()
            count, reset = self._rates.get(key, (0, 0.0))
            if now >= reset:
                count, reset = 0, now + window_seconds
            count += 1
            self._rates[key] = (count, reset)
            if count > limit:
                return RateDecision(False, count, max(1, int(reset - now + 0.999)))
            return RateDecision(True, count, 0)

    # qulf
    def acquire(self, name, ttl_seconds):
        with self._lock:
            held = self._locks.get(name)
            if held and self._clock() < held[1]:
                return None
            token = uuid.uuid4().hex
            self._locks[name] = (token, self._clock() + ttl_seconds)
            return token

    def release(self, name, token):
        with self._lock:
            held = self._locks.get(name)
            if held and held[0] == token:
                del self._locks[name]
                return True
            return False

    # oqim
    def publish(self, stream, fields, maxlen=10_000):
        with self._lock:
            self._seq += 1
            msg_id = f"{self._seq}-0"
            entries = self._streams.setdefault(stream, [])
            entries.append((msg_id, dict(fields)))
            if len(entries) > maxlen:
                del entries[: len(entries) - maxlen]
            return msg_id

    def ensure_group(self, stream, group):
        with self._lock:
            self._streams.setdefault(stream, [])
            self._groups.setdefault((stream, group), {"delivered": 0, "pending": set()})

    def read(self, stream, group, consumer, count=10):
        with self._lock:
            self.ensure_group(stream, group)
            g = self._groups[(stream, group)]
            entries = self._streams[stream]
            batch = [e for e in entries if int(e[0].split("-")[0]) > g["delivered"]][:count]
            for msg_id, _ in batch:
                g["delivered"] = max(g["delivered"], int(msg_id.split("-")[0]))
                g["pending"].add(msg_id)
            return [(i, dict(f)) for i, f in batch]

    def ack(self, stream, group, ids):
        with self._lock:
            g = self._groups.get((stream, group))
            if not g:
                return 0
            n = len(set(ids) & g["pending"])
            g["pending"] -= set(ids)
            return n
