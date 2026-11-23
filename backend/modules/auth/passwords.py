"""Parol xeshlash: scrypt (standart kutubxona). Format: scrypt$N$r$p$tuz$xesh."""
import base64
import hashlib
import hmac
import os

N, R, P = 2 ** 14, 8, 1
MIN_LENGTH = 8


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"parol kamida {MIN_LENGTH} belgidan iborat bo'lishi kerak")
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(h)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, h = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                              n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


# Mavjud bo'lmagan login uchun ham xuddi shuncha vaqt sarflanadi (login mavjudligini vaqtdan bilib bo'lmasin)
DUMMY_HASH = hash_password("dummy-password-not-used")
