"""Hodisa -> qoldiq/hujjat o'zgarishi. Har bir hodisa turi uchun bitta sof funksiya.

Qoida: funksiya faqat (hodisa, hozirgi holat) ga qaraydi, soat, tasodif yoki tashqi ma'lumotga emas.
Hisoblangan qiymatlar (tannarx, narx nusxasi, taqsimot) hodisa ichiga yoziladi, shunda jurnaldan qayta qurish
har doim aynan bir xil natija beradi, hatto o'sha vaqtdan keyin sozlamalar o'zgargan bo'lsa ham.
"""
from typing import Callable

from backend.ledger.events import Event

Projector = Callable[[Event, object, object], None]
_REGISTRY: dict[str, Projector] = {}


class UnknownEventType(Exception):
    pass


def projects(*event_types: str):
    """@projects("sale_recorded") — shu turdagi hodisa qoldiqqa qanday ta'sir qilishini ro'yxatdan o'tkazadi."""
    def deco(fn: Projector) -> Projector:
        for t in event_types:
            if t in _REGISTRY:
                raise ValueError(f"{t} uchun projeksiya allaqachon bor")
            _REGISTRY[t] = fn
        return fn
    return deco


def no_effect(*event_types: str) -> None:
    """Qoldiqqa ta'sir qilmaydigan, faqat tarix uchun yoziladigan hodisalar (masalan, vazifa almashtirish)."""
    for t in event_types:
        _REGISTRY[t] = lambda ev, bal, docs: None


def apply_event(ev: Event, bal, docs) -> None:
    fn = _REGISTRY.get(ev.type)
    if fn is None:
        raise UnknownEventType(ev.type)
    fn(ev, bal, docs)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
