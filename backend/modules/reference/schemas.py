"""Ma'lumotnoma maydonlari sxemasi (talablar §4). Qat'iy: noma'lum maydon rad etiladi, turlar tekshiriladi.

Koordinatalar kasr son bo'lmasligi uchun mikro-daraja butun sonda saqlanadi: lat_e6 = 41311100 (41,3111°).
"""
import re
from dataclasses import dataclass
from typing import Any

from backend.core.errors import DomainError

KINDS = ("material", "packmaterial", "product", "store", "supplier")
ID_PREFIX = {"material": "RM", "packmaterial": "PM", "product": "P", "store": "S", "supplier": "SUP"}
LANGS = ("uz", "ru", "en")
_PHONE = re.compile(r"^\+?\d{9,15}$")
_ID = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


@dataclass(frozen=True)
class F:
    type: str                     # str | int | bool | phone | enum | id_list | bom
    required: bool = False
    default: Any = None
    min: int | None = None
    max: int | None = None
    max_len: int = 120
    allowed: tuple = ()
    immutable: bool = False


SCHEMAS: dict[str, dict[str, F]] = {
    "material": {                                   # xom ashyo turi (gramm hisobida)
        "name": F("str", required=True, max_len=80),
        "shelf_life_days": F("int", min=1, max=1095),    # bo'sh bo'lsa siyosatdagi standart muddat
        "min_stock_g": F("int", default=0, min=0, max=100_000_000),
        "active": F("bool", default=True),
    },
    "packmaterial": {                               # qadoq materiali (dona hisobida), har o'lcham alohida
        "name": F("str", required=True, max_len=80),
        "min_stock": F("int", default=0, min=0, max=100_000_000),
        "active": F("bool", default=True),
    },
    "product": {                                    # tayyor mahsulot: xom ashyo turi + qadoq og'irligi
        "name": F("str", max_len=100),
        "raw_material_id": F("str", required=True, immutable=True),
        "pack_g": F("int", required=True, min=1, max=100_000, immutable=True),
        "materials": F("bom", default=[]),               # bitta qadoqqa ketadigan material: [{material_id, qty}]
        "active": F("bool", default=True),
    },
    "store": {
        "name": F("str", required=True, max_len=100),
        "agent_id": F("str", required=True),             # mas'ul agent (foydalanuvchi)
        "address": F("str", max_len=200),
        "contact_name": F("str", max_len=100),
        "phone": F("phone"),
        "lat_e6": F("int", min=-90_000_000, max=90_000_000),
        "lon_e6": F("int", min=-180_000_000, max=180_000_000),
        "telegram_chat_id": F("str", max_len=40),
        "lang": F("enum", default="uz", allowed=LANGS),  # Telegram xabarlari tili
        "note": F("str", max_len=300),
        "active": F("bool", default=True),
    },
    "supplier": {
        "name": F("str", required=True, max_len=100),
        "phone": F("phone"),
        "supplies": F("id_list", default=[]),             # yetkazadigan xom ashyo turlari
        "note": F("str", max_len=300),
        "active": F("bool", default=True),
    },
}


def _bad(field, msg, **kw):
    return DomainError("invalid_field", msg, field=field, **kw)


def _coerce(name: str, f: F, v):
    if v is None:
        if f.required:
            raise DomainError("missing_field", f"'{name}' kiritilmagan", field=name)
        return None                                   # ixtiyoriy maydonni tozalash
    if f.type == "str":
        if not isinstance(v, str) or not v.strip() or len(v.strip()) > f.max_len:
            raise _bad(name, f"'{name}' matn bo'lishi va {f.max_len} belgidan oshmasligi kerak")
        return v.strip()
    if f.type == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise _bad(name, f"'{name}' butun son bo'lishi kerak")
        if (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
            raise _bad(name, f"'{name}' {f.min} dan {f.max} gacha bo'lishi kerak")
        return v
    if f.type == "bool":
        if not isinstance(v, bool):
            raise _bad(name, f"'{name}' ha/yo'q bo'lishi kerak")
        return v
    if f.type == "phone":
        if not isinstance(v, str):
            raise _bad(name, "Telefon raqami noto'g'ri")
        cleaned = re.sub(r"[\s\-()]", "", v)
        if not _PHONE.match(cleaned):
            raise _bad(name, "Telefon raqami noto'g'ri (masalan +998901234567)")
        return cleaned
    if f.type == "enum":
        if v not in f.allowed:
            raise _bad(name, f"'{name}' qiymati noto'g'ri", allowed=list(f.allowed))
        return v
    if f.type == "id_list":
        if not isinstance(v, list) or any(not isinstance(i, str) or not _ID.match(i) for i in v) or len(set(v)) != len(v):
            raise _bad(name, f"'{name}' takrorlanmaydigan ID lar ro'yxati bo'lishi kerak")
        return list(v)
    if f.type == "bom":
        if not isinstance(v, list):
            raise _bad(name, f"'{name}' ro'yxat bo'lishi kerak")
        out, seen = [], set()
        for item in v:
            if (not isinstance(item, dict) or set(item) != {"material_id", "qty"}
                    or not isinstance(item["material_id"], str) or not _ID.match(item["material_id"])
                    or isinstance(item["qty"], bool) or not isinstance(item["qty"], int) or item["qty"] < 1):
                raise _bad(name, "Har bir element {material_id, qty>=1} bo'lishi kerak")
            if item["material_id"] in seen:
                raise _bad(name, "Bir material ikki marta ko'rsatilgan")
            seen.add(item["material_id"])
            out.append({"material_id": item["material_id"], "qty": item["qty"]})
        return out
    raise AssertionError(f.type)


def normalize(kind: str, raw, existing: dict | None) -> tuple[dict, dict]:
    """Kiritilgan maydonlarni tekshiradi. Qaytaradi: (yakuniy to'liq ma'lumot, {maydon: [eski, yangi]})."""
    schema = SCHEMAS[kind]
    if not isinstance(raw, dict) or not raw:
        raise DomainError("invalid_field", "'data' bo'sh bo'lmagan lug'at bo'lishi kerak", field="data")
    for k in raw:
        if k not in schema:
            raise DomainError("unknown_field", f"Noma'lum maydon: {k}", field=k, allowed=sorted(schema))
    creating = existing is None
    out = {k: (list(f.default) if isinstance(f.default, list) else f.default) for k, f in schema.items()} if creating \
        else {k: existing.get(k) for k in schema}
    changes: dict = {}
    for k, v in raw.items():
        f = schema[k]
        new = _coerce(k, f, v)
        if not creating and f.immutable and new != existing.get(k):
            raise DomainError("immutable_field", f"'{k}' yaratilgandan keyin o'zgarmaydi", field=k)
        if not creating and new != existing.get(k):
            changes[k] = [existing.get(k), new]
        out[k] = new
    if creating:
        for k, f in schema.items():
            if f.required and out.get(k) in (None, ""):
                raise DomainError("missing_field", f"'{k}' kiritilmagan", field=k)
    return out, changes
