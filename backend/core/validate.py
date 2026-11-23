"""Buyruq mazmunini tekshirish yordamchilari. Xatolar `DomainError` (kod + maydon nomi) bo'lib chiqadi."""
from .errors import DomainError


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def need(payload: dict, *names: str) -> None:
    for n in names:
        if n not in payload or _blank(payload[n]):
            raise DomainError("missing_field", f"'{n}' kiritilmagan", field=n)


def as_int(payload: dict, name: str, *, minimum: int | None = None, maximum: int | None = None,
           default: int | None = None) -> int:
    if name not in payload or payload[name] is None:
        if default is not None:
            return default
        raise DomainError("missing_field", f"'{name}' kiritilmagan", field=name)
    v = payload[name]
    if isinstance(v, bool) or not isinstance(v, int):
        raise DomainError("invalid_field", f"'{name}' butun son bo'lishi kerak", field=name)
    if minimum is not None and v < minimum:
        raise DomainError("invalid_field", f"'{name}' kamida {minimum} bo'lishi kerak", field=name)
    if maximum is not None and v > maximum:
        raise DomainError("invalid_field", f"'{name}' ko'pi bilan {maximum} bo'lishi kerak", field=name)
    return v


def as_str(payload: dict, name: str, *, allowed: tuple[str, ...] | None = None, default: str | None = None,
           max_len: int = 500) -> str:
    if name not in payload or _blank(payload[name]):
        if default is not None:
            return default
        raise DomainError("missing_field", f"'{name}' kiritilmagan", field=name)
    v = payload[name]
    if isinstance(v, str):
        v = v.strip()                                   # bo'sh joylar bilan o'ralgan qiymat tozalanadi
    if not isinstance(v, str) or len(v) > max_len:
        raise DomainError("invalid_field", f"'{name}' noto'g'ri", field=name)
    if allowed is not None and v not in allowed:
        raise DomainError("invalid_field", f"'{name}' qiymati ruxsat etilganlardan biri bo'lishi kerak",
                          field=name, allowed=list(allowed))
    return v


def as_list(payload: dict, name: str, *, min_len: int = 1) -> list:
    v = payload.get(name)
    if not isinstance(v, list) or len(v) < min_len:
        raise DomainError("invalid_field", f"'{name}' ro'yxat bo'lishi va kamida {min_len} ta element bo'lishi kerak",
                          field=name)
    return v
