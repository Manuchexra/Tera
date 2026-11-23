"""Testlar uchun umumiy muhit: baza, soat, kirish xizmati, dvigatel va tayyor foydalanuvchilar."""
from dataclasses import dataclass, field

from backend.core.clock import FixedClock
from backend.ledger.journal import Journal
from backend.tests.dbtest import open_test_db
from backend.modules.auth.actor import Actor
from backend.modules.auth.permissions import Permissions
from backend.modules.auth.service import AuthService
from backend.modules.auth.tokens import TokenService
from backend.modules.sync.engine import Engine
from backend.modules.sync.registry import Command

T0 = "2026-11-18T06:00:00.000Z"
SECRET = "k" * 40


@dataclass
class Env:
    db: object
    clock: FixedClock
    journal: Journal
    auth: AuthService
    engine: Engine
    perms: Permissions
    owner: Actor
    actors: dict = field(default_factory=dict)

    def add_user(self, login: str, tasks: list[str], password: str = "password123"):
        user = self.auth.create_user(self.owner, login, password, login.title(), tasks)
        res = self.auth.login(login, password, f"dev-{login}")
        actor = self.auth.authenticate(res.token)
        self.actors[login] = actor
        return user, actor

    def cmd(self, n, type_, payload=None, **kw) -> Command:
        return Command(event_id=f"c{n}", type=type_, payload=payload if payload is not None else {},
                       device_time=kw.pop("device_time", self.clock_now()), **kw)

    def clock_now(self) -> str:
        # device vaqti server soatiga yaqin bo'lsin (testda soat oldinga ketadi)
        t = self.clock.now()
        return t


def make_env(path: str = ":memory:", extra_actions: dict | None = None) -> Env:
    clock = FixedClock(T0, step_ms=1)
    db = open_test_db()
    journal = Journal(db, clock)
    perms = Permissions.default()
    if extra_actions:
        perms.extend(extra_actions)
    auth = AuthService(journal, TokenService(SECRET, clock), perms)
    auth.bootstrap_owner("aziz", "password123", "Aziz Rahimov")
    res = auth.login("aziz", "password123", "dev-owner")
    owner = auth.authenticate(res.token)
    return Env(db=db, clock=clock, journal=journal, auth=auth, engine=Engine(journal, perms), perms=perms, owner=owner)


import unittest  # noqa: E402


class AppCase(unittest.TestCase):
    """Dvigatel + egasi + agent (Jasur) + omborchi (Malika) tayyor bo'lgan sinov muhiti."""

    extra_actions: dict | None = None

    def setUp(self):
        self.env = make_env(extra_actions=self.extra_actions)
        self.e, self.j = self.env.engine, self.env.journal
        self.owner = self.env.owner
        self.agent_user, self.agent = self.env.add_user("jasur", ["agent"])
        self.wh_user, self.wh = self.env.add_user("malika", ["warehouse"])
        self._n = 0

    def run_cmd(self, type_, payload=None, actor=None, **kw):
        self._n += 1
        return self.e.execute(self.env.cmd(self._n, type_, payload, **kw), actor or self.owner)

    def ok(self, type_, payload=None, actor=None, **kw):
        r = self.run_cmd(type_, payload, actor, **kw)
        self.assertEqual(r.status, "accepted", (r.code, r.message, r.details))
        return r

    def fail(self, type_, payload=None, code=None, actor=None, **kw):
        r = self.run_cmd(type_, payload, actor, **kw)
        self.assertNotEqual(r.status, "accepted", "qabul qilinmasligi kerak edi")
        if code:
            self.assertEqual(r.code, code, (r.code, r.message, r.details))
        return r

    # --- tez yaratish ---
    def ref(self, kind, data, **kw):
        return self.ok("reference.upsert", {"kind": kind, "data": data, **kw}).response["id"]

    def seed(self):
        """Almonds, 50 g paket, 'Almonds 50 g' mahsulot, do'kon, yetkazib beruvchi."""
        s = {}
        s["raw"] = self.ref("material", {"name": "Almonds", "shelf_life_days": 180})
        s["pm"] = self.ref("packmaterial", {"name": "Pouch 50 g", "min_stock": 500})
        s["product"] = self.ref("product", {"raw_material_id": s["raw"], "pack_g": 50,
                                            "materials": [{"material_id": s["pm"], "qty": 1}]})
        s["store"] = self.ref("store", {"name": "Baraka Market", "agent_id": self.agent_user.user_id,
                                        "phone": "+998 90 123 45 67", "lang": "uz"})
        s["supplier"] = self.ref("supplier", {"name": "Samarkand Agro", "supplies": [s["raw"]]})
        return s

    def doc(self, key):
        return self.j.docs.get(key)

    def bal(self, key):
        return self.j.bal.get(key)

    def add_balance(self, key, value):
        with self.j.transaction():
            self.j.bal.add(key, value)
