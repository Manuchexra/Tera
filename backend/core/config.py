"""Sozlamalar muhit o'zgaruvchilaridan. Sirlar (parol, kalit) hech qachon repozitoriyga tushmaydi (`infrastructure/env/`)."""
import os
from dataclasses import dataclass

from .errors import DomainError


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret: str
    redis_url: str | None = None
    token_ttl_seconds: int = 12 * 3600
    pool_min: int = 1
    pool_max: int = 10

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env

        def integer(name: str, default: int, lo: int, hi: int) -> int:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                v = int(raw)
            except ValueError:
                raise ConfigError(f"{name} butun son bo'lishi kerak")
            if not lo <= v <= hi:
                raise ConfigError(f"{name} {lo} dan {hi} gacha bo'lishi kerak")
            return v

        secret = env.get("TERA_SECRET", "")
        if len(secret.encode("utf-8")) < 32:
            raise ConfigError("TERA_SECRET kamida 32 bayt bo'lishi kerak")
        db = (env.get("TERA_DATABASE_URL") or "").strip()
        if not db:
            raise ConfigError("TERA_DATABASE_URL o'rnatilmagan (masalan postgresql://tera:parol@db:5432/tera)")
        if not db.startswith(("postgresql://", "postgres://", "sqlite://")):
            raise ConfigError("TERA_DATABASE_URL postgresql:// yoki sqlite:// bilan boshlanishi kerak")
        redis_url = (env.get("TERA_REDIS_URL") or "").strip() or None
        if redis_url and not redis_url.startswith(("redis://", "rediss://", "unix://")):
            raise ConfigError("TERA_REDIS_URL redis:// bilan boshlanishi kerak")
        pool_min = integer("TERA_DB_POOL_MIN", 1, 1, 100)
        pool_max = integer("TERA_DB_POOL_MAX", 10, 1, 500)
        if pool_max < pool_min:
            raise ConfigError("TERA_DB_POOL_MAX TERA_DB_POOL_MIN dan kichik bo'lmasligi kerak")
        return cls(database_url=db, secret=secret, redis_url=redis_url,
                   token_ttl_seconds=integer("TERA_TOKEN_TTL_SECONDS", 12 * 3600, 60, 30 * 24 * 3600),
                   pool_min=pool_min, pool_max=pool_max)

    @property
    def is_production_db(self) -> bool:
        return self.database_url.startswith(("postgresql://", "postgres://"))
