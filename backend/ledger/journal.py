"""Journal: hodisani yozish va uning ta'sirini qoldiqqa qo'llash bitta tranzaksiyada.

Barcha bosqichlar (xarid, sotuv, qaytim, ...) hodisani faqat shu yerdan yozadi.
"""
from backend.balances.projection import apply_event
from backend.balances.store import Balances, Docs
from backend.core.clock import SystemClock

from .db import Database
from .events import Event, NewEvent
from .store import Ledger


class Journal:
    def __init__(self, db: Database, clock=None):
        self.db = db
        self.clock = clock or SystemClock()
        self.ledger = Ledger(db)
        self.bal = Balances(db)
        self.docs = Docs(db)

    def transaction(self):
        return self.db.transaction()

    def record(self, new: NewEvent, recorded_at: str | None = None) -> Event:
        """Hodisani jurnalga yozadi va qoldiqqa qo'llaydi. Tranzaksiya ichida chaqiriladi.

        Bir xil event_id ikkinchi marta kelsa `DuplicateEvent` ko'tariladi va hech narsa o'zgarmaydi.
        """
        ev = self.ledger.append(new, recorded_at or self.clock.now())
        apply_event(ev, self.bal, self.docs)
        return ev
