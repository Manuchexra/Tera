"""Biznes sozlamalari.

Qiymatlarning yagona manbasi: `shared/constants/policy.json` (backend, mobil va bot bir xil o'qiydi).
Egasi ularni sabab bilan o'zgartira oladi (4-bosqichda `policy.update` buyrug'i); o'zgargan qiymatlar
jurnalga yoziladi va ustiga qo'llanadi.
"""
import json
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

POLICY_JSON = Path(__file__).resolve().parents[2] / "shared" / "constants" / "policy.json"

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# kalit -> (tur, eng kam, eng ko'p). Chegaralar xato kiritishdan (masalan 14 o'rniga 1400 kun) himoya qiladi.
FIELD_RULES = {
    "credit_limit": (int, 0, 1_000_000_000),
    "payment_term_days": (int, 1, 180),
    "day_close_deadline": (str, None, None),
    "dispute_working_days": (int, 1, 30),
    "return_alert_pct": (int, 1, 100),
    "shrink_deviation_pct": (int, 1, 500),
    "shrink_min_batches": (int, 1, 1000),
    "shrink_min_input_g": (int, 1_000, 100_000_000),
    "stale_after_minutes": (int, 5, 1_440),
    "risky_debt_age_days": (int, 1, 3_650),
    "clock_ok_seconds": (int, 30, 3_600),
    "clock_max_seconds": (int, 60, 604_800),
    "default_shelf_life_days": (int, 1, 1_095),
}


class PolicyError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Policy:
    credit_limit: int                # barcha do'konlar uchun bir xil, so'm
    payment_term_days: int           # qarz muddati
    day_close_deadline: str          # agent kunni shu vaqtgacha yopadi (Toshkent)
    dispute_working_days: int        # bahsni hal qilish muddati (16-qaror)
    return_alert_pct: int            # qaytim ulushi shundan oshsa egasiga ogohlantirish
    shrink_deviation_pct: int        # kam chiqish o'rtachadan shuncha chetlashsa ogohlantirish
    shrink_min_batches: int          # kam chiqish o'rtachasi uchun eng kamida partiya
    shrink_min_input_g: int          # ... va eng kamida shuncha gramm xom ashyo
    stale_after_minutes: int         # agent ma'lumoti shundan eski bo'lsa kulrang
    risky_debt_age_days: int         # "31+ kun" qarz (sotuv sanasidan, D-06 tasdiqlanishi kerak)
    clock_ok_seconds: int            # soat farqi shundan kam bo'lsa qurilma vaqti ishonchli
    clock_max_seconds: int           # shundan ko'p bo'lsa soat noto'g'ri, rad etiladi
    default_shelf_life_days: int     # xom ashyo turida muddat ko'rsatilmasa

    @classmethod
    def default(cls) -> "Policy":
        return cls.from_dict(json.loads(POLICY_JSON.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        known = {f.name for f in fields(cls)}
        missing = known - set(data)
        if missing:
            raise ValueError(f"policy keys missing: {sorted(missing)}")
        return cls(**{k: v for k, v in data.items() if k in known})

    def with_changes(self, **changes) -> "Policy":
        known = {f.name for f in fields(self)}
        bad = set(changes) - known
        if bad:
            raise ValueError(f"unknown policy keys: {sorted(bad)}")
        return replace(self, **changes)

    def validated_with(self, changes: dict) -> "Policy":
        """Egasi kiritgan o'zgarishlarni qat'iy tekshirib, yangi sozlamani qaytaradi. Xato bo'lsa `PolicyError`."""
        if not isinstance(changes, dict) or not changes:
            raise PolicyError("changes", "O'zgarishlar bo'sh")
        for key, value in changes.items():
            if key not in FIELD_RULES:
                raise PolicyError(key, f"Noma'lum sozlama: {key}")
            typ, lo, hi = FIELD_RULES[key]
            if typ is int:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise PolicyError(key, f"{key} butun son bo'lishi kerak")
                if value < lo or value > hi:
                    raise PolicyError(key, f"{key} {lo} dan {hi} gacha bo'lishi kerak")
            elif not isinstance(value, str) or not _HHMM.match(value):
                raise PolicyError(key, f"{key} SS:DD formatida bo'lishi kerak (masalan 21:00)")
        new = self.with_changes(**changes)
        if new.clock_max_seconds < new.clock_ok_seconds:
            raise PolicyError("clock_max_seconds", "clock_max_seconds clock_ok_seconds dan kichik bo'lmasligi kerak")
        if new.risky_debt_age_days < new.payment_term_days:
            raise PolicyError("risky_debt_age_days", "Xavfli qarz yoshi qarz muddatidan kam bo'lmasligi kerak")
        return new

    def merged(self, data: dict | None) -> "Policy":
        """Jurnaldan kelgan o'zgarishlarni (nomaʼlum kalitlarsiz) ustiga qo'llaydi."""
        known = {f.name for f in fields(self)}
        return replace(self, **{k: v for k, v in (data or {}).items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)
