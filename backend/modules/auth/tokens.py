"""Kirish tokeni: HS256 imzolangan JWT (faqat standart kutubxona, tashqi paket kerak emas).

Tokenda: foydalanuvchi, FAOL vazifa, qurilma. Vazifa almashtirilganda yangi token beriladi.
Foydalanuvchi o'chirilgan, vazifasi olib tashlangan yoki qurilma bekor qilingan bo'lsa, token imzosi to'g'ri bo'lsa
ham ishlamaydi: bu `UserStore.validate_actor` da har buyruqda tekshiriladi.
"""
import base64
import hashlib
import hmac
import json
import os

from backend.core.errors import DomainError
from backend.core.timing import parse_ts

MIN_SECRET_BYTES = 32


def secret_from_env(name: str = "TERA_SECRET") -> bytes:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} muhit o'zgaruvchisi o'rnatilmagan")
    return v.encode("utf-8")


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenService:
    def __init__(self, secret: bytes | str, clock, ttl_seconds: int = 12 * 3600):
        secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"maxfiy kalit kamida {MIN_SECRET_BYTES} bayt bo'lishi kerak")
        self._secret = secret
        self.clock = clock
        self.ttl = ttl_seconds

    def _now(self) -> int:
        return int(parse_ts(self.clock.now()).timestamp())

    def _sign(self, signing_input: str) -> str:
        return _b64e(hmac.new(self._secret, signing_input.encode("ascii"), hashlib.sha256).digest())

    def issue(self, user_id: str, task: str, device_id: str) -> str:
        now = self._now()
        header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = {"sub": user_id, "tsk": task, "dev": device_id, "iat": now, "exp": now + self.ttl}
        body = _b64e(json.dumps(claims, separators=(",", ":")).encode())
        return f"{header}.{body}.{self._sign(header + '.' + body)}"

    def verify(self, token: str) -> dict:
        try:
            h, b, s = token.split(".")
            header = json.loads(_b64d(h))
            claims = json.loads(_b64d(b))
        except (ValueError, TypeError, AttributeError):
            raise DomainError("token_invalid", "Token noto'g'ri")
        if header.get("alg") != "HS256":                     # "none" va boshqa algoritmlar rad etiladi
            raise DomainError("token_invalid", "Token noto'g'ri")
        if not hmac.compare_digest(self._sign(h + "." + b), s):
            raise DomainError("token_invalid", "Token noto'g'ri")
        if not all(k in claims for k in ("sub", "tsk", "dev", "exp")):
            raise DomainError("token_invalid", "Token noto'g'ri")
        if self._now() >= int(claims["exp"]):
            raise DomainError("token_expired", "Token muddati tugagan. Qayta kiring.")
        return claims
