"""Buyruq bajarilayotganda handlerga beriladigan kontekst."""
from backend.core.errors import DomainError, Forbidden
from backend.core.policy import Policy
from backend.core.timing import parse_ts
from backend.ledger.counters import new_doc_id
from backend.ledger.events import Event, NewEvent
from backend.ledger.journal import Journal
from backend.modules.auth.actor import Actor
from backend.modules.auth.permissions import Permissions
from backend.modules.auth.users import User

from .outbox import Outbox
from .registry import Command


class Ctx:
    def __init__(self, journal: Journal, actor: Actor, user: User, command: Command, occurred_at: str,
                 time_flag: str, recorded_at: str, policy: Policy, permissions: Permissions, outbox: Outbox):
        self.journal = journal
        self.db = journal.db
        self.bal = journal.bal
        self.docs = journal.docs
        self.actor = actor
        self.user = user
        self.command = command
        self.occurred_at = occurred_at
        self.time_flag = time_flag
        self.recorded_at = recorded_at
        self.policy = policy
        self._perms = permissions
        self._outbox = outbox
        self._emitted = 0

    # --- ruxsat ---
    def require(self, action: str) -> None:
        if not self._perms.allowed(self.actor.task, action):
            raise Forbidden("forbidden", "Bu amal sizning vazifangiz uchun ruxsat etilmagan",
                            action=action, task=self.actor.task)

    def require_fresh(self) -> None:
        """Faqat onlayn bajariladigan amallar (ma'lumotnoma, narx, siyosat, ruxsatlar).

        Navbatda turib keyin yuborilgan yoki soati ishonchsiz buyruq o'tmaydi: egasi qaror vaqtida
        serverga ulangan bo'lishi kerak (talablar §4.1: ma'lumotnoma faqat onlayn)."""
        gap = abs((parse_ts(self.recorded_at) - parse_ts(self.occurred_at)).total_seconds())
        if self.time_flag != "ok" or gap > self.policy.clock_ok_seconds:
            raise DomainError("online_required", "Bu amal faqat internet orqali, hoziroq bajariladi",
                              gap_seconds=int(gap))

    def two_party(self, other_user_id: str, what: str) -> None:
        """Muhim harakat ikki tomonlama: bir odam ikkala tomonda bo'la olmaydi (§2.5)."""
        if other_user_id == self.actor.user_id:
            raise DomainError("same_person_both_sides", "Bu harakatni ikkinchi shaxs tasdiqlashi kerak", what=what)

    # --- yozish ---
    def emit(self, type_: str, payload: dict, corrects_event_id: str | None = None) -> Event:
        """Jurnalga hodisa yozadi va qoldiqqa qo'llaydi. Birinchi hodisa buyruqning event_id sini oladi,
        keyingilari `event_id#1`, `event_id#2`, ... (qayta yuborilsa, hammasi bir butun takror deb topiladi)."""
        eid = self.command.event_id if self._emitted == 0 else f"{self.command.event_id}#{self._emitted}"
        self._emitted += 1
        try:
            ev = NewEvent(
                event_id=eid, type=type_, actor_id=self.actor.user_id, payload=payload,
                occurred_at=self.occurred_at, time_flag=self.time_flag, device_id=self.actor.device_id,
                device_time=self.command.device_time, gps=self.command.gps,
                corrects_event_id=corrects_event_id)
        except ValueError as e:
            raise DomainError("invalid_payload", str(e))
        return self.journal.record(ev, self.recorded_at)

    def new_id(self, prefix: str) -> str:
        return new_doc_id(self.db, prefix, self.occurred_at)

    def notify(self, channel: str, kind: str, recipient: str | None, payload: dict) -> None:
        self._outbox.add(channel, kind, recipient, payload, self.recorded_at)

    @property
    def events_emitted(self) -> int:
        return self._emitted
