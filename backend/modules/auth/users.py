"""Foydalanuvchilar va qurilmalar jadvali (parol va holat o'zgaruvchan, lekin har o'zgarish jurnalga yoziladi)."""
import json
from dataclasses import dataclass

from backend.core.errors import Forbidden
from backend.ledger.db import Database

from .actor import Actor


@dataclass
class User:
    user_id: str
    login: str
    full_name: str
    phone: str | None
    tasks: list[str]
    active: bool


def _user(r) -> User:
    return User(r["user_id"], r["login"], r["full_name"], r["phone"], json.loads(r["tasks"]), bool(r["active"]))


class UserStore:
    def __init__(self, db: Database):
        self.db = db

    def count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS c FROM users")["c"]

    def get(self, user_id: str) -> User | None:
        r = self.db.one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return _user(r) if r else None

    def get_by_login(self, login: str) -> User | None:
        r = self.db.one("SELECT * FROM users WHERE login = ?", (login.strip().lower(),))
        return _user(r) if r else None

    def all_users(self) -> "list[User]":
        return [_user(r) for r in self.db.query("SELECT * FROM users ORDER BY user_id")]

    def create(self, user_id, login, full_name, phone, pw_hash, tasks, created_at) -> User:
        self.db.execute(
            "INSERT INTO users (user_id, login, full_name, phone, pw_hash, tasks, active, created_at) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (user_id, login.strip().lower(), full_name, phone, pw_hash, json.dumps(tasks), created_at))
        return self.get(user_id)

    def pw_hash(self, user_id: str) -> str:
        return self.db.one("SELECT pw_hash FROM users WHERE user_id = ?", (user_id,))["pw_hash"]

    def set_password(self, user_id: str, pw_hash: str) -> None:
        self.db.execute("UPDATE users SET pw_hash = ?, failed_logins = 0, locked_until = NULL WHERE user_id = ?",
                        (pw_hash, user_id))

    def set_tasks(self, user_id: str, tasks: list[str]) -> None:
        self.db.execute("UPDATE users SET tasks = ? WHERE user_id = ?", (json.dumps(tasks), user_id))

    def set_active(self, user_id: str, active: bool) -> None:
        self.db.execute("UPDATE users SET active = ? WHERE user_id = ?", (1 if active else 0, user_id))

    def active_owner_count(self, excluding: str | None = None) -> int:
        n = 0
        for u in self.all_users():
            if u.active and "owner" in u.tasks and u.user_id != excluding:
                n += 1
        return n

    # --- kirishga urinishlar ---
    def lock_info(self, user_id: str):
        r = self.db.one("SELECT failed_logins, locked_until FROM users WHERE user_id = ?", (user_id,))
        return r["failed_logins"], r["locked_until"]

    def record_failure(self, user_id: str, failures: int, locked_until: str | None) -> None:
        self.db.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE user_id = ?",
                        (failures, locked_until, user_id))

    def clear_failures(self, user_id: str) -> None:
        self.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE user_id = ?", (user_id,))

    # --- qurilmalar ---
    def get_device(self, device_id: str):
        return self.db.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))

    def register_device(self, device_id: str, user_id: str, label: str | None, now: str) -> bool:
        """True qaytsa, qurilma yangi ro'yxatdan o'tdi."""
        d = self.get_device(device_id)
        if d is None:
            self.db.execute("INSERT INTO devices (device_id, user_id, label, created_at, last_seen) VALUES (?,?,?,?,?)",
                            (device_id, user_id, label, now, now))
            return True
        self.touch_device(device_id, now)
        return False

    def touch_device(self, device_id: str, now: str) -> None:
        self.db.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now, device_id))

    def revoke_device(self, device_id: str) -> None:
        self.db.execute("UPDATE devices SET revoked = 1 WHERE device_id = ?", (device_id,))

    # --- har buyruqda tekshiriladi ---
    def validate_actor(self, actor: Actor) -> User:
        u = self.get(actor.user_id)
        if u is None or not u.active:
            raise Forbidden("actor_not_allowed", "Hisob faol emas", reason="user_inactive")
        if actor.task not in u.tasks:
            raise Forbidden("actor_not_allowed", "Bu vazifa sizga biriktirilmagan", reason="task_not_assigned")
        d = self.get_device(actor.device_id)
        if d is None or d["user_id"] != actor.user_id or d["revoked"]:
            raise Forbidden("actor_not_allowed", "Qurilma ruxsat etilmagan", reason="device_not_allowed")
        return u
