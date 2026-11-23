"""Redis vazifalari uchun interfeys. Ikki realizatsiya: xotirada (`memory.py`) va haqiqiy Redis (`redis_impl.py`).

QOIDA: Redis hech qachon haqiqat manbai emas. Pul, qoldiq, hodisa, xabar navbati faqat PostgreSQL'da.
Redis o'chsa tizim sekinroq ishlaydi, lekin noto'g'ri natija bermaydi:
  kesh -> "topilmadi", tezlik cheklovi -> ruxsat (bazadagi bloklash baribir ishlaydi), qulf -> xato (vazifa o'tkazib yuboriladi).
"""
from contextlib import contextmanager
from dataclasses import dataclass


class LockUnavailable(Exception):
    """Qulf olinmadi (boshqa jarayon ushlab turibdi yoki Redis ishlamayapti)."""


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    count: int
    retry_after: int = 0


class Backend:
    """To'rt vazifa: kesh, tezlik cheklovi, qulf, oqim (stream)."""

    # --- 1. kesh ---
    def get(self, key: str) -> str | None: raise NotImplementedError
    def set(self, key: str, value: str, ttl_seconds: int) -> None: raise NotImplementedError
    def delete(self, *keys: str) -> None: raise NotImplementedError

    # --- 2. tezlik cheklovi (qat'iy oyna) ---
    def hit(self, key: str, limit: int, window_seconds: int) -> RateDecision: raise NotImplementedError

    # --- 3. qulf ---
    def acquire(self, name: str, ttl_seconds: int) -> str | None: raise NotImplementedError
    def release(self, name: str, token: str) -> bool: raise NotImplementedError

    @contextmanager
    def lock(self, name: str, ttl_seconds: int = 60):
        token = self.acquire(name, ttl_seconds)
        if token is None:
            raise LockUnavailable(name)
        try:
            yield token
        finally:
            try:
                self.release(name, token)
            except Exception:                      # qulf TTL bilan baribir tugaydi
                pass

    # --- 4. oqim (bot uchun xabarlar) ---
    def publish(self, stream: str, fields: dict[str, str], maxlen: int = 10_000) -> str: raise NotImplementedError
    def ensure_group(self, stream: str, group: str) -> None: raise NotImplementedError
    def read(self, stream: str, group: str, consumer: str, count: int = 10) -> list[tuple[str, dict]]: raise NotImplementedError
    def ack(self, stream: str, group: str, ids: list[str]) -> int: raise NotImplementedError

    @property
    def healthy(self) -> bool:
        return True
