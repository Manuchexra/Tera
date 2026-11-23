"""Outbox xabarlarini Redis oqimlariga muntazam uzatadigan fon jarayoni."""
import os
import time

from backend.cache.redis_impl import RedisBackend
from backend.core.clock import SystemClock
from backend.ledger.db import DatabaseProvider
from backend.modules.sync.relay import relay_outbox, requeue_stale

CHANNELS = ("owner", "telegram_store")


def relay_once(db, backend, now: str, limit: int = 100) -> dict:
    requeued = requeue_stale(db, now)
    relayed = {channel: relay_outbox(db, backend, channel, now, limit) for channel in CHANNELS}
    return {"requeued": requeued, "relayed": relayed}


def main() -> None:
    database_url = os.environ.get("TERA_DATABASE_URL")
    redis_url = os.environ.get("TERA_REDIS_URL")
    if not database_url:
        raise RuntimeError("TERA_DATABASE_URL o'rnatilmagan")
    if not redis_url:
        raise RuntimeError("TERA_REDIS_URL o'rnatilmagan; outbox relay uchun Redis kerak")
    provider = DatabaseProvider(database_url)
    backend = RedisBackend.from_url(redis_url)
    clock = SystemClock()
    try:
        while True:
            with provider.session() as db:
                relay_once(db, backend, clock.now())
            time.sleep(2)
    finally:
        backend.r.close()
        provider.close()


if __name__ == "__main__":
    main()
