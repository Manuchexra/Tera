"""Kirish xizmati: egasini yaratish, foydalanuvchi qo'shish, login, vazifa almashtirish, qurilmani bekor qilish.

Har bir o'zgarish jurnalga yoziladi ("kim, qachon, nima"). Parol va uning xeshi hech qachon jurnalga tushmaydi.
"""
from dataclasses import dataclass
from datetime import timedelta

from backend.core.errors import DomainError, Forbidden
from backend.core.timing import fmt_ts, parse_ts
from backend.ledger.counters import next_number
from backend.ledger.events import NewEvent
from backend.ledger.journal import Journal

from . import projections  # noqa: F401  (audit hodisalarini ro'yxatdan o'tkazadi)
from .actor import Actor
from .passwords import DUMMY_HASH, hash_password, verify_password
from .permissions import Permissions
from .tokens import TokenService
from .users import User, UserStore

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
LOGIN_LIMIT_PER_USER = (10, 60)         # bir login uchun daqiqada 10 urinish
LOGIN_LIMIT_PER_CLIENT = (30, 60)       # bir manzil (IP) uchun daqiqada 30 urinish


class _LoginFailed(Exception):
    """Ichki: login muvaffaqiyatsiz; tranzaksiya commit bo'lgach tashqarida xato sifatida ko'tariladi."""

    def __init__(self, error: DomainError):
        self.error = error


@dataclass
class LoginResult:
    token: str
    user: User
    task: str


class AuthService:
    def __init__(self, journal: Journal, tokens: TokenService, permissions: Permissions | None = None,
                 limiter=None):
        self.limiter = limiter              # cache.base.Backend yoki None (Redis'siz ham ishlaydi)
        self.j = journal
        self.db = journal.db
        self.tokens = tokens
        self.perms = permissions or Permissions.default()
        self.users = UserStore(self.db)

    # ------------------------------------------------------------------ yordamchi
    def _record(self, actor_id: str, type_: str, payload: dict, device_id: str | None = None) -> None:
        n = next_number(self.db, "authevent")
        self.j.record(NewEvent(f"auth-{n:06d}", type_, actor_id, payload,
                               occurred_at=self.j.clock.now(), device_id=device_id))

    def _check_tasks(self, tasks) -> list[str]:
        if not isinstance(tasks, list) or not tasks:
            raise DomainError("invalid_field", "Kamida bitta vazifa kerak", field="tasks")
        bad = [t for t in tasks if not self.perms.is_task(t)]
        if bad or len(set(tasks)) != len(tasks):
            raise DomainError("invalid_field", "Vazifalar noto'g'ri", field="tasks", bad=bad)
        return list(tasks)

    def _require_owner(self, by: Actor) -> User:
        user = self.users.validate_actor(by)
        if not self.perms.allowed(by.task, "user.manage"):
            raise Forbidden("forbidden", "Foydalanuvchilarni faqat egasi boshqaradi")
        return user

    def _normalize_login(self, login: str) -> str:
        login = (login or "").strip().lower()
        if len(login) < 3 or " " in login:
            raise DomainError("invalid_field", "Login kamida 3 belgi, bo'sh joysiz", field="login")
        return login

    def _check_password(self, password: str) -> str:
        try:
            return hash_password(password)
        except ValueError as e:
            raise DomainError("weak_password", str(e))

    def _new_user(self, creator_id: str | None, login, password, full_name, tasks, phone) -> User:
        login = self._normalize_login(login)
        tasks = self._check_tasks(tasks)
        if not full_name or not full_name.strip():
            raise DomainError("missing_field", "Ism kiritilmagan", field="full_name")
        if self.users.get_by_login(login):
            raise DomainError("login_taken", "Bu login band", field="login")
        pw = self._check_password(password)
        user_id = f"U{next_number(self.db, 'user'):03d}"
        user = self.users.create(user_id, login, full_name.strip(), phone, pw, tasks, self.j.clock.now())
        self._record(creator_id or user_id, "user_created",
                     {"user_id": user_id, "login": login, "full_name": user.full_name, "phone": phone, "tasks": tasks})
        return user

    # ------------------------------------------------------------------ boshqaruv
    def bootstrap_owner(self, login: str, password: str, full_name: str, phone: str | None = None) -> User:
        """Tizim birinchi marta ishga tushganda egasini yaratadi. Foydalanuvchi bor bo'lsa, rad etiladi."""
        with self.j.transaction():
            if self.users.count() > 0:
                raise DomainError("already_initialized", "Tizimda foydalanuvchilar allaqachon bor")
            return self._new_user(None, login, password, full_name, ["owner"], phone)

    def create_user(self, by: Actor, login: str, password: str, full_name: str, tasks: list[str],
                    phone: str | None = None) -> User:
        with self.j.transaction():
            self._require_owner(by)
            return self._new_user(by.user_id, login, password, full_name, tasks, phone)

    def set_tasks(self, by: Actor, user_id: str, tasks: list[str]) -> User:
        with self.j.transaction():
            self._require_owner(by)
            target = self.users.get(user_id)
            if target is None:
                raise DomainError("not_found", "Foydalanuvchi topilmadi")
            tasks = self._check_tasks(tasks)
            if "owner" in target.tasks and "owner" not in tasks and self.users.active_owner_count(user_id) == 0:
                raise DomainError("last_owner", "Oxirgi egasidan egalik vazifasini olib bo'lmaydi")
            self.users.set_tasks(user_id, tasks)
            self._record(by.user_id, "user_updated", {"user_id": user_id, "changes": {"tasks": [target.tasks, tasks]}})
            return self.users.get(user_id)

    def set_active(self, by: Actor, user_id: str, active: bool) -> User:
        with self.j.transaction():
            self._require_owner(by)
            target = self.users.get(user_id)
            if target is None:
                raise DomainError("not_found", "Foydalanuvchi topilmadi")
            if not active and "owner" in target.tasks and self.users.active_owner_count(user_id) == 0:
                raise DomainError("last_owner", "Oxirgi faol egani o'chirib bo'lmaydi")
            self.users.set_active(user_id, active)
            self._record(by.user_id, "user_updated",
                         {"user_id": user_id, "changes": {"active": [target.active, active]}})
            return self.users.get(user_id)

    def revoke_device(self, by: Actor, device_id: str) -> None:
        with self.j.transaction():
            self._require_owner(by)
            d = self.users.get_device(device_id)
            if d is None:
                raise DomainError("not_found", "Qurilma topilmadi")
            self.users.revoke_device(device_id)
            self._record(by.user_id, "device_revoked", {"device_id": device_id, "user_id": d["user_id"]})

    def change_password(self, user_id: str, old_password: str, new_password: str) -> None:
        with self.j.transaction():
            user = self.users.get(user_id)
            if user is None or not verify_password(old_password, self.users.pw_hash(user_id)):
                raise DomainError("invalid_credentials", "Parol noto'g'ri")
            self.users.set_password(user_id, self._check_password(new_password))
            self._record(user_id, "password_changed", {"user_id": user_id})

    # ------------------------------------------------------------------ sessiya
    def _check_rate(self, login: str, client_key: str | None) -> None:
        if self.limiter is None:
            return
        checks = [(f"login:user:{(login or '').strip().lower()}", LOGIN_LIMIT_PER_USER)]
        if client_key:
            checks.append((f"login:client:{client_key}", LOGIN_LIMIT_PER_CLIENT))
        for key, (limit, window) in checks:
            d = self.limiter.hit(key, limit, window)
            if not d.allowed:
                raise DomainError("rate_limited", "Juda ko'p urinish. Birozdan keyin qayta urinib ko'ring.",
                                  retry_after=d.retry_after)

    def login(self, login: str, password: str, device_id: str, device_label: str | None = None,
              client_key: str | None = None) -> LoginResult:
        """Kirish. Muvaffaqiyatsiz urinishlar hisobi xato ko'tarilishidan OLDIN saqlanadi, aks holda
        tranzaksiya bekor bo'lib, bloklash hech qachon ishlamas edi.

        `client_key` (odatda IP) bo'lsa, Redis tezlik cheklovi ham qo'llanadi. Redis ishlamasa, cheklov o'tkazib
        yuboriladi, lekin 5 ta xatodan keyin bloklash (bazada) baribir ishlaydi."""
        self._check_rate(login, client_key)
        failure: DomainError | None = None
        result: LoginResult | None = None
        with self.j.transaction():
            try:
                result = self._login(login, password, device_id, device_label)
            except _LoginFailed as f:
                failure = f.error
        if failure is not None:
            raise failure
        return result

    def _login(self, login, password, device_id, device_label) -> LoginResult:
        now = self.j.clock.now()
        user = self.users.get_by_login(login or "")
        if user is None:
            verify_password(password or "", DUMMY_HASH)             # vaqt bir xil bo'lsin
            raise _LoginFailed(DomainError("invalid_credentials", "Login yoki parol noto'g'ri"))
        failures, locked_until = self.users.lock_info(user.user_id)
        if locked_until and parse_ts(locked_until) > parse_ts(now):
            raise _LoginFailed(DomainError("account_locked", "Hisob vaqtincha bloklangan. Keyinroq urinib ko'ring.",
                                           locked_until=locked_until))
        if not verify_password(password or "", self.users.pw_hash(user.user_id)):
            failures += 1
            lock = None
            if failures >= MAX_FAILED_LOGINS:
                lock = fmt_ts(parse_ts(now) + timedelta(minutes=LOCK_MINUTES))
                failures = 0
            self.users.record_failure(user.user_id, failures, lock)  # saqlanadi (xato tranzaksiyadan tashqarida ko'tariladi)
            raise _LoginFailed(DomainError("invalid_credentials", "Login yoki parol noto'g'ri"))
        if not user.active:
            raise _LoginFailed(DomainError("invalid_credentials", "Login yoki parol noto'g'ri"))
        if not device_id:
            raise _LoginFailed(DomainError("missing_field", "device_id kiritilmagan", field="device_id"))
        existing = self.users.get_device(device_id)
        if existing is not None and existing["user_id"] != user.user_id:
            raise _LoginFailed(Forbidden("device_belongs_to_other_user", "Bu qurilma boshqa hisobga biriktirilgan"))
        if existing is not None and existing["revoked"]:
            raise _LoginFailed(Forbidden("device_revoked", "Bu qurilma bekor qilingan"))
        self.users.clear_failures(user.user_id)
        if self.users.register_device(device_id, user.user_id, device_label, now):
            self._record(user.user_id, "device_registered",
                         {"device_id": device_id, "user_id": user.user_id, "label": device_label}, device_id)
        task = user.tasks[0]
        self._record(user.user_id, "session_started", {"user_id": user.user_id, "task": task}, device_id)
        return LoginResult(self.tokens.issue(user.user_id, task, device_id), user, task)

    def switch_task(self, actor: Actor, new_task: str) -> str:
        """Vazifa almashtirish (System 02). Yangi token qaytaradi."""
        with self.j.transaction():
            user = self.users.validate_actor(actor)
            if new_task not in user.tasks:
                raise Forbidden("task_not_assigned", "Bu vazifa sizga biriktirilmagan")
            self._record(user.user_id, "session_task_switched",
                         {"user_id": user.user_id, "from": actor.task, "to": new_task}, actor.device_id)
            return self.tokens.issue(user.user_id, new_task, actor.device_id)

    def authenticate(self, token: str) -> Actor:
        claims = self.tokens.verify(token)
        actor = Actor(claims["sub"], claims["tsk"], claims["dev"])
        try:
            self.users.validate_actor(actor)
        except Forbidden:
            raise DomainError("unauthenticated", "Qayta kiring")
        return actor
