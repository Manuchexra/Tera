import base64
import json
import unittest

from backend.core.errors import DomainError, Forbidden
from backend.modules.auth.actor import Actor
from backend.modules.auth.passwords import hash_password, verify_password
from backend.modules.auth.permissions import Permissions
from backend.modules.auth.tokens import TokenService
from backend.balances.rebuild import verify
from backend.modules import load_all

from .helpers import SECRET, make_env


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        h = hash_password("sirli-parol-1")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(verify_password("sirli-parol-1", h))
        self.assertFalse(verify_password("sirli-parol-2", h))
        self.assertNotEqual(h, hash_password("sirli-parol-1"))      # har safar boshqa tuz

    def test_short_password_refused_and_garbage_hash_safe(self):
        with self.assertRaises(ValueError):
            hash_password("short")
        self.assertFalse(verify_password("x", "not-a-hash"))
        self.assertFalse(verify_password("x", "bcrypt$1$2$3$a$b"))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.tokens = self.env.auth.tokens

    def test_roundtrip(self):
        c = self.tokens.verify(self.tokens.issue("U001", "agent", "dev1"))
        self.assertEqual((c["sub"], c["tsk"], c["dev"]), ("U001", "agent", "dev1"))

    def test_expiry(self):
        t = self.tokens.issue("U001", "agent", "dev1")
        self.env.clock.advance(hours=13)
        with self.assertRaises(DomainError) as cm:
            self.tokens.verify(t)
        self.assertEqual(cm.exception.code, "token_expired")

    def test_tampered_claims_rejected(self):
        h, b, s = self.tokens.issue("U002", "agent", "dev1").split(".")
        claims = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        claims["tsk"] = "owner"
        b2 = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
        with self.assertRaises(DomainError) as cm:
            self.tokens.verify(f"{h}.{b2}.{s}")
        self.assertEqual(cm.exception.code, "token_invalid")

    def test_alg_none_and_garbage_rejected(self):
        def enc(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        none_tok = f"{enc({'alg': 'none'})}.{enc({'sub': 'U1', 'tsk': 'owner', 'dev': 'd', 'exp': 9999999999})}."
        for bad in [none_tok, "abc", "a.b.c", ""]:
            with self.assertRaises(DomainError):
                self.tokens.verify(bad)

    def test_other_secret_rejected_and_weak_secret_refused(self):
        other = TokenService("z" * 40, self.env.clock)
        with self.assertRaises(DomainError):
            self.tokens.verify(other.issue("U1", "agent", "d"))
        with self.assertRaises(ValueError):
            TokenService("short", self.env.clock)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.p = Permissions.default()

    def test_matrix_follows_requirements(self):
        a = self.p.allowed
        self.assertTrue(a("agent", "sale.create"))
        self.assertFalse(a("warehouse", "sale.create"))
        self.assertTrue(a("owner", "price.set"))
        self.assertFalse(a("agent", "price.set"))                  # narxni agent o'zgartirmaydi
        self.assertFalse(a("packer", "price.set"))
        self.assertEqual(self.p.tasks_for("packaging.post"), ["owner", "packer"])
        self.assertEqual(self.p.tasks_for("closing.submit"), ["agent"])
        self.assertTrue(a("bot", "dispute.open"))
        self.assertFalse(a("bot", "sale.create"))
        self.assertTrue(a("accountant", "transfer.accept.cash_handover"))
        self.assertFalse(a("agent", "transfer.accept.cash_handover"))

    def test_unknown_action_denied_to_everyone(self):
        for t in self.p.tasks:
            self.assertFalse(self.p.allowed(t, "nonexistent.action"))

    def test_extend_validates_tasks(self):
        with self.assertRaises(ValueError):
            self.p.extend({"x.y": ["wizard"]})
        with self.assertRaises(ValueError):
            Permissions(["owner"], {"a": ["agent"]})


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.auth = self.env.auth
        self.owner = self.env.owner

    def test_bootstrap_only_once(self):
        with self.assertRaises(DomainError) as cm:
            self.auth.bootstrap_owner("other", "password123", "Other")
        self.assertEqual(cm.exception.code, "already_initialized")

    def test_owner_creates_user_and_user_logs_in(self):
        user, actor = self.env.add_user("jasur", ["agent", "warehouse"])
        self.assertEqual(user.tasks, ["agent", "warehouse"])
        self.assertEqual(actor.task, "agent")                      # birinchi vazifa
        self.assertEqual(actor.device_id, "dev-jasur")

    def test_only_owner_manages_users(self):
        _, agent = self.env.add_user("jasur", ["agent"])
        with self.assertRaises(Forbidden):
            self.auth.create_user(agent, "malika", "password123", "Malika", ["warehouse"])

    def test_validation(self):
        for kwargs, code in [
            (dict(login="ab", password="password123", full_name="X", tasks=["agent"]), "invalid_field"),
            (dict(login="bad name", password="password123", full_name="X", tasks=["agent"]), "invalid_field"),
            (dict(login="validlogin", password="short", full_name="X", tasks=["agent"]), "weak_password"),
            (dict(login="validlogin", password="password123", full_name="X", tasks=[]), "invalid_field"),
            (dict(login="validlogin", password="password123", full_name="X", tasks=["wizard"]), "invalid_field"),
            (dict(login="validlogin", password="password123", full_name="  ", tasks=["agent"]), "missing_field"),
            (dict(login="AZIZ", password="password123", full_name="X", tasks=["agent"]), "login_taken"),
        ]:
            with self.assertRaises(DomainError) as cm:
                self.auth.create_user(self.owner, **kwargs)
            self.assertEqual(cm.exception.code, code, kwargs)

    def test_wrong_password_and_unknown_login_look_the_same(self):
        for login in ["aziz", "nobody"]:
            with self.assertRaises(DomainError) as cm:
                self.auth.login(login, "wrong-password", "dev-x")
            self.assertEqual(cm.exception.code, "invalid_credentials")

    def test_lockout_after_five_failures_then_expires(self):
        for _ in range(5):
            with self.assertRaises(DomainError):
                self.auth.login("aziz", "wrong-password", "dev-owner")
        with self.assertRaises(DomainError) as cm:
            self.auth.login("aziz", "password123", "dev-owner")    # to'g'ri parol ham o'tmaydi
        self.assertEqual(cm.exception.code, "account_locked")
        self.env.clock.advance(minutes=16)
        self.assertEqual(self.auth.login("aziz", "password123", "dev-owner").user.login, "aziz")

    def test_success_resets_failure_counter(self):
        for _ in range(4):
            with self.assertRaises(DomainError):
                self.auth.login("aziz", "wrong-password", "dev-owner")
        self.auth.login("aziz", "password123", "dev-owner")
        with self.assertRaises(DomainError):
            self.auth.login("aziz", "wrong-password", "dev-owner")
        self.assertEqual(self.auth.login("aziz", "password123", "dev-owner").user.login, "aziz")

    def test_switch_task(self):
        _, actor = self.env.add_user("jasur", ["agent", "warehouse"])
        token = self.auth.switch_task(actor, "warehouse")
        self.assertEqual(self.auth.authenticate(token).task, "warehouse")
        with self.assertRaises(Forbidden):
            self.auth.switch_task(actor, "owner")                  # biriktirilmagan vazifa

    def test_deactivated_user_loses_access_immediately(self):
        user, actor = self.env.add_user("jasur", ["agent"])
        token = self.auth.login("jasur", "password123", "dev-jasur").token
        self.auth.set_active(self.owner, user.user_id, False)
        with self.assertRaises(DomainError) as cm:
            self.auth.authenticate(token)
        self.assertEqual(cm.exception.code, "unauthenticated")
        with self.assertRaises(DomainError):
            self.auth.login("jasur", "password123", "dev-jasur")

    def test_task_removed_invalidates_token_for_that_task(self):
        user, actor = self.env.add_user("jasur", ["agent", "warehouse"])
        self.auth.set_tasks(self.owner, user.user_id, ["warehouse"])
        with self.assertRaises(DomainError):
            self.auth.authenticate(self.auth.tokens.issue(user.user_id, "agent", actor.device_id))

    def test_revoked_device(self):
        _, actor = self.env.add_user("jasur", ["agent"])
        token = self.auth.tokens.issue(actor.user_id, "agent", actor.device_id)
        self.auth.revoke_device(self.owner, actor.device_id)
        with self.assertRaises(DomainError):
            self.auth.authenticate(token)
        with self.assertRaises(Forbidden):
            self.auth.login("jasur", "password123", actor.device_id)

    def test_device_bound_to_one_user(self):
        self.env.add_user("jasur", ["agent"])
        self.env.add_user("malika", ["warehouse"])
        with self.assertRaises(Forbidden):
            self.auth.login("malika", "password123", "dev-jasur")

    def test_last_owner_protected(self):
        user = self.auth.users.get_by_login("aziz")
        with self.assertRaises(DomainError) as cm:
            self.auth.set_active(self.owner, user.user_id, False)
        self.assertEqual(cm.exception.code, "last_owner")
        with self.assertRaises(DomainError):
            self.auth.set_tasks(self.owner, user.user_id, ["agent"])

    def test_change_password(self):
        self.auth.change_password(self.owner.user_id, "password123", "new-password-1")
        self.auth.login("aziz", "new-password-1", "dev-owner")
        with self.assertRaises(DomainError):
            self.auth.login("aziz", "password123", "dev-owner")
        with self.assertRaises(DomainError):
            self.auth.change_password(self.owner.user_id, "wrong-old", "another-pass-1")

    def test_audit_trail_without_secrets_and_rebuild_ok(self):
        self.env.add_user("jasur", ["agent"])
        self.auth.change_password(self.owner.user_id, "password123", "new-password-1")
        events = list(self.env.journal.ledger.iter_events())
        types = {e.type for e in events}
        self.assertTrue({"user_created", "device_registered", "session_started", "password_changed"} <= types)
        blob = json.dumps([e.payload for e in events])
        self.assertNotIn("password123", blob)
        self.assertNotIn("scrypt", blob)
        load_all()
        self.assertTrue(verify(self.env.db).ok)


if __name__ == "__main__":
    unittest.main()
