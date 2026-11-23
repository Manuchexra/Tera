import logging

from .base import Backend
from .memory import MemoryBackend

log = logging.getLogger("tera.cache")


def make_backend(redis_url: str | None) -> Backend:
    """`redis_url` bo'lsa haqiqiy Redis, bo'lmasa xotirada (faqat bitta jarayonli ishlab chiqish va testlar uchun)."""
    if not redis_url:
        log.warning("TERA_REDIS_URL o'rnatilmagan: xotiradagi kesh ishlatilmoqda (ko'p jarayonli muhitda to'g'ri ishlamaydi)")
        return MemoryBackend()
    from .redis_impl import RedisBackend
    return RedisBackend.from_url(redis_url)
