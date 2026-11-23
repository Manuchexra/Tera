"""Vaqt manbai. Testlarda `FixedClock` ishlatiladi, ishlab chiqarishda `SystemClock`."""
from datetime import datetime, timedelta, timezone

from .timing import fmt_ts, parse_ts


class SystemClock:
    def now(self) -> str:
        return fmt_ts(datetime.now(timezone.utc))


class FixedClock:
    """Boshqariladigan soat: `now()` har chaqirilganda `step_ms` millisekundga oshadi."""

    def __init__(self, start: str = "2026-11-18T06:00:00.000Z", step_ms: int = 1):
        self._t = parse_ts(start)
        self._step = timedelta(milliseconds=step_ms)

    def now(self) -> str:
        t = self._t
        self._t = t + self._step
        return fmt_ts(t)

    def set(self, ts: str) -> None:
        self._t = parse_ts(ts)

    def advance(self, **kwargs) -> None:
        self._t = self._t + timedelta(**kwargs)
