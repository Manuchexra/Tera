"""Buyruq dvigateli: mobil ilova yuborgan buyruqni tekshiradi, bajaradi va natijasini aniq qaytaradi.

Tartib (har bir buyruq bitta tranzaksiyada):
  1. takrorlanganmi?  (jurnalda yoki rad etilganlar qutisida bor event_id) -> shu natijani qaytaradi
  2. foydalanuvchi faolmi, vazifa va qurilma ruxsat etilganmi
  3. buyruq ro'yxatdan o'tganmi, vazifaga ruxsat bormi
  4. hodisa vaqti (qurilma soati yoki server vaqti) aniqlanadi
  5. handler ishlaydi: hodisalarni jurnalga yozadi va qoldiqni yangilaydi
Natija: accepted | duplicate | rejected | conflict.
Rad etilgan va konflikt buyruqlar asl holida `sync_inbox` da saqlanadi. Jurnalga hech narsa yozilmaydi.
"""
from dataclasses import dataclass, field

from backend.balances.projection import no_effect  # noqa: F401
from backend.core.errors import Conflict, DomainError, Forbidden
from backend.core.policy import Policy
from backend.core.timing import parse_ts, resolve_occurred_at
from backend.ledger.events import NewEvent
from backend.ledger.journal import Journal
from backend.modules.auth.actor import Actor
from backend.modules.auth.permissions import Permissions
from backend.modules.auth.users import UserStore

from . import projections  # noqa: F401
from .context import Ctx
from .inbox import Inbox
from .outbox import Outbox
from .registry import Command, get_command


@dataclass
class CommandResult:
    event_id: str
    status: str                     # accepted | duplicate | rejected | conflict
    seq: int | None = None
    code: str | None = None
    message: str | None = None
    details: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "duplicate")

    def to_json(self) -> dict:
        return {"event_id": self.event_id, "status": self.status, "seq": self.seq, "code": self.code,
                "message": self.message, "details": self.details, "response": self.response,
                "replayed": self.replayed}


def _valid_event_id(s) -> bool:
    return isinstance(s, str) and 0 < len(s) <= 200 and not any(c.isspace() for c in s)


class Engine:
    def __init__(self, journal: Journal, permissions: Permissions | None = None):
        self.journal = journal
        self.db = journal.db
        self.perms = permissions or Permissions.default()
        self.users = UserStore(self.db)
        self.inbox = Inbox(self.db)
        self.outbox = Outbox(self.db)
        self._base_policy = Policy.default()
        from backend.modules import load_all
        load_all()

    def current_policy(self) -> Policy:
        """Sozlamalar: standart qiymatlar plus egasi jurnal orqali kiritgan o'zgarishlar (`policy:global`)."""
        doc = self.journal.docs.get("policy:global")
        return self._base_policy.merged(doc.data if doc else None)

    # ------------------------------------------------------------------ asosiy
    def execute(self, cmd: Command, actor: Actor) -> CommandResult:
        if not _valid_event_id(cmd.event_id):
            return CommandResult(str(cmd.event_id), "rejected", code="invalid_event_id",
                                 message="event_id noto'g'ri (bo'sh yoki bo'sh joyli)")
        recorded_at = self.journal.clock.now()
        with self.db.transaction():
            existing = self.journal.ledger.get(cmd.event_id)
            if existing is not None:
                return CommandResult(cmd.event_id, "duplicate", seq=existing.seq, replayed=True)
            stored = self.inbox.get(cmd.event_id)
            if stored is not None:
                status = "conflict" if stored["status"] == "conflict" else "rejected"
                return CommandResult(cmd.event_id, status, code=stored["code"], message=stored["message"],
                                     details=stored["details"], replayed=True)
            try:
                return self._run(cmd, actor, recorded_at)
            except DomainError as e:
                self.inbox.add(cmd.to_json(), actor.user_id, actor.device_id, e.status, e.code,
                               e.message, e.details, recorded_at)
                if isinstance(e, Conflict):
                    self.outbox.add("owner", "conflict_needs_review", None,
                                    {"event_id": cmd.event_id, "type": cmd.type, "code": e.code,
                                     "actor_id": actor.user_id, "details": e.details}, recorded_at)
                return CommandResult(cmd.event_id, e.status, code=e.code, message=e.message, details=e.details)

    def execute_batch(self, commands: list[Command], actor: Actor) -> list[CommandResult]:
        """Offline navbatdagi buyruqlarni ketma-ket bajaradi. Har biri alohida tranzaksiya: biri rad etilsa, qolganlari o'tadi."""
        return [self.execute(c, actor) for c in commands]

    def _run(self, cmd: Command, actor: Actor, recorded_at: str) -> CommandResult:
        user = self.users.validate_actor(actor)
        self.users.touch_device(actor.device_id, recorded_at)
        spec = get_command(cmd.type)
        if spec is None:
            raise DomainError("unknown_command", f"Noma'lum buyruq: {cmd.type}")
        if not self.perms.allowed(actor.task, spec.action):
            raise Forbidden("forbidden", "Bu amal sizning vazifangiz uchun ruxsat etilmagan",
                            action=spec.action, task=actor.task)
        if not isinstance(cmd.payload, dict):
            raise DomainError("invalid_payload", "Buyruq mazmuni lug'at bo'lishi kerak")
        policy = self.current_policy()
        for name in ("device_time", "est_server_time"):
            v = getattr(cmd, name)
            if v is not None:
                try:
                    parse_ts(v)
                except (ValueError, TypeError, AttributeError):
                    raise DomainError("invalid_field", f"{name} noto'g'ri sana", field=name)
        if not cmd.device_time:
            raise DomainError("missing_field", "device_time kiritilmagan", field="device_time")
        occurred_at, flag = resolve_occurred_at(cmd.device_time, recorded_at, cmd.est_server_time,
                                                policy.clock_ok_seconds, policy.clock_max_seconds)
        with self.db.transaction():            # savepoint: handler xato qilsa, yarim yozilganlar qaytadi
            ctx = Ctx(self.journal, actor, user, cmd, occurred_at, flag, recorded_at, policy, self.perms, self.outbox)
            response = spec.handler(ctx, cmd.payload) or {}
        seq = None
        if ctx.events_emitted:
            seq = self.journal.ledger.get(cmd.event_id).seq
        return CommandResult(cmd.event_id, "accepted", seq=seq, response=response)

    # ------------------------------------------------------------------ rad etilganlar bilan ishlash (egasi)
    def list_inbox(self, actor: Actor, status: str | None = None) -> list[dict]:
        self.users.validate_actor(actor)
        if not self.perms.allowed(actor.task, "inbox.resolve"):
            raise Forbidden("forbidden", "Faqat egasi ko'ra oladi")
        return self.inbox.list(status)

    def resolve_inbox(self, actor: Actor, event_id: str, resolution: str) -> None:
        """Egasi rad etilgan/konfliktdagi buyruqni "ko'rib chiqildi" deb belgilaydi. Asl yozuv saqlanib qoladi;
        haqiqiy tuzatish alohida (bog'langan) buyruq bilan qilinadi."""
        if not resolution or not resolution.strip():
            raise DomainError("missing_field", "Izoh majburiy", field="resolution")
        with self.db.transaction():
            self.users.validate_actor(actor)
            if not self.perms.allowed(actor.task, "inbox.resolve"):
                raise Forbidden("forbidden", "Faqat egasi hal qila oladi")
            now = self.journal.clock.now()
            try:
                self.inbox.resolve(event_id, actor.user_id, resolution.strip(), now)
            except KeyError:
                raise DomainError("not_found", "Bunday ochiq yozuv yo'q")
            self.journal.record(NewEvent(f"inbox-resolved-{event_id}", "inbox_resolved", actor.user_id,
                                         {"event_id": event_id, "resolution": resolution.strip()},
                                         occurred_at=now, device_id=actor.device_id), now)
