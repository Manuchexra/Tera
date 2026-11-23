"""Vaqt qoidalari (talablar §20.2.4).

Barcha vaqt UTC ISO-8601 satr ("2026-11-18T06:35:00.000Z"). Biznes sanasi Toshkent vaqti bo'yicha (UTC+5).
"""
from datetime import date, datetime, timedelta, timezone

from .errors import DomainError

UTC = timezone.utc
TASHKENT = timezone(timedelta(hours=5))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_date(ts: str) -> date:
    """Hodisaning biznes sanasi (Toshkent bo'yicha)."""
    return parse_ts(ts).astimezone(TASHKENT).date()


def month_of(ts: str) -> str:
    d = local_date(ts)
    return f"{d.year:04d}-{d.month:02d}"


def resolve_occurred_at(device_time: str, recorded_at: str, est_server_time: str | None = None,
                        ok_seconds: int = 300, max_seconds: int = 86400) -> tuple[str, str]:
    """Hodisaning biznes vaqtini aniqlaydi.

    Qurilma o'z soatidan tashqari `est_server_time` ni ham yuboradi: oxirgi sinxronizatsiyadagi server vaqti
    plus o'tgan monoton vaqt. Shu bilan "soat noto'g'ri" va "yuborish kechikdi" bir-biridan ajratiladi.
    Agar u yuborilmasa, solishtirish serverga kelgan vaqt (`recorded_at`) bilan qilinadi.

    * farq < 5 daqiqa     -> occurred_at = device_time, belgi 'ok'
    * 5 daqiqa .. 1 kun   -> occurred_at = recorded_at, belgi 'suspicious'
    * 1 kundan ortiq      -> rad etiladi (soatni to'g'rilash kerak)
    """
    dev = parse_ts(device_time)
    ref = parse_ts(est_server_time) if est_server_time else parse_ts(recorded_at)
    skew = abs((dev - ref).total_seconds())
    if skew < ok_seconds:
        return fmt_ts(dev), "ok"
    if skew <= max_seconds:
        return recorded_at, "suspicious"
    raise DomainError("device_clock_wrong", "Qurilma soati noto'g'ri. Soatni to'g'rilang.", skew_seconds=int(skew))


def add_working_days(start: date, n: int) -> date:
    """Ish kunlarini qo'shadi. Shanba va yakshanba dam olish kuni (kerak bo'lsa sozlamaga chiqariladi)."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d
