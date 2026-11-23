from datetime import timedelta  # noqa: F401

from backend.balances.rebuild import verify
from backend.core.errors import DomainError
from backend.modules import load_all
from backend.modules.approvals.projections import mark_approval_used
from backend.modules.approvals.rules import check_approval

from .helpers import AppCase


class GrantTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()

    def grant(self, **kw):
        payload = {"kind": "discount", "store_id": self.s["store"], "max_percent": 5, "reason": "doimiy mijoz"}
        payload.update(kw)
        payload = {k: v for k, v in payload.items() if v is not ...}
        return self.run_cmd("approval.grant", payload)

    def test_discount_by_percent_creates_open_approval(self):
        r = self.grant()
        self.assertEqual(r.status, "accepted")
        self.assertEqual(r.response["approval_id"], "AP-261118-001")
        d = self.doc("approval:AP-261118-001")
        self.assertEqual((d.status, d.ref), ("open", self.s["store"]))
        self.assertEqual((d.data["kind"], d.data["max_percent"], d.data["max_amount"], d.data["granted_by"]),
                         ("discount", 5, None, self.owner.user_id))
        self.assertEqual(r.response["expires_at"], d.data["expires_at"])

    def test_default_expiry_is_24h_and_max_is_a_week(self):
        r = self.grant()
        granted = self.doc("approval:" + r.response["approval_id"]).data["granted_at"]
        self.assertTrue(r.response["expires_at"].startswith("2026-11-19T06:0"), (granted, r.response["expires_at"]))
        self.assertEqual(self.grant(expires_in_hours=168).status, "accepted")
        self.assertEqual(self.grant(expires_in_hours=169).code, "invalid_field")
        self.assertEqual(self.grant(expires_in_hours=0).code, "invalid_field")

    def test_discount_needs_exactly_one_limit(self):
        self.assertEqual(self.grant(max_percent=..., max_amount=None).code, "invalid_field")      # ikkalasi ham yo'q
        self.assertEqual(self.grant(max_amount=20_000).code, "invalid_field")                     # ikkalasi bor
        self.assertEqual(self.grant(max_percent=..., max_amount=20_000).status, "accepted")
        self.assertEqual(self.grant(max_percent=0).code, "invalid_field")
        self.assertEqual(self.grant(max_percent=101).code, "invalid_field")

    def test_credit_exception_and_labor_rules(self):
        self.assertEqual(self.grant(kind="credit_exception", max_percent=..., max_amount=35_000).status, "accepted")
        self.assertEqual(self.grant(kind="credit_exception", max_percent=5).code, "invalid_field")
        self.assertEqual(self.grant(kind="credit_exception", max_percent=...).code, "missing_field")
        self.assertEqual(self.grant(kind="labor", store_id=..., max_percent=..., max_amount=28_000).status, "accepted")
        self.assertEqual(self.grant(kind="labor", max_percent=..., max_amount=28_000).code, "invalid_field")   # do'konga bog'lanmaydi
        self.assertEqual(self.grant(kind="labor", store_id=..., max_percent=...).code, "missing_field")
        self.assertEqual(self.grant(kind="bonus").code, "invalid_field")

    def test_store_and_user_must_exist(self):
        self.assertEqual(self.grant(store_id="S-404").code, "not_found")
        self.assertEqual(self.grant(store_id=...).code, "missing_field")
        self.assertEqual(self.grant(for_user_id="U999").code, "not_found")
        self.assertEqual(self.grant(for_user_id=self.agent_user.user_id).status, "accepted")
        self.ok("reference.upsert", {"kind": "store", "id": self.s["store"], "data": {"active": False}})
        self.assertEqual(self.grant().code, "not_found")

    def test_reason_required_owner_only_and_online(self):
        self.assertEqual(self.grant(reason=...).code, "missing_field")
        self.assertEqual(self.grant(reason=" ").code, "missing_field")
        payload = {"kind": "discount", "store_id": self.s["store"], "max_percent": 5, "reason": "x"}
        self.assertEqual(self.run_cmd("approval.grant", payload, actor=self.agent).code, "forbidden")
        self.assertEqual(self.run_cmd("approval.grant", payload, actor=self.wh).code, "forbidden")
        self.assertEqual(self.run_cmd("approval.grant", payload, device_time="2026-11-18T05:00:00.000Z").code,
                         "online_required")

    def test_revoke(self):
        aid = self.grant().response["approval_id"]
        self.assertEqual(self.run_cmd("approval.revoke", {"approval_id": aid}).code, "missing_field")
        self.assertEqual(self.run_cmd("approval.revoke", {"approval_id": aid, "reason": "fikr o'zgardi"},
                                      actor=self.agent).code, "forbidden")
        self.ok("approval.revoke", {"approval_id": aid, "reason": "fikr o'zgardi"})
        d = self.doc(f"approval:{aid}")
        self.assertEqual((d.status, d.data["revoke_reason"], d.data["revoked_by"]), ("revoked", "fikr o'zgardi", self.owner.user_id))
        self.assertEqual(self.run_cmd("approval.revoke", {"approval_id": aid, "reason": "yana"}).code, "approval_not_open")
        self.assertEqual(self.run_cmd("approval.revoke", {"approval_id": "AP-404", "reason": "x"}).code, "not_found")

    def test_rebuild_matches(self):
        aid = self.grant().response["approval_id"]
        self.grant(kind="labor", store_id=..., max_percent=..., max_amount=28_000)
        self.ok("approval.revoke", {"approval_id": aid, "reason": "x"})
        load_all()
        rep = verify(self.env.db)
        self.assertTrue(rep.ok, (rep.balance_diffs, rep.doc_diffs))


class CheckApprovalTests(AppCase):
    """Ishlatuvchi buyruqlar (sotuv, qadoqlash) shu tekshiruvga tayanadi."""

    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.discount = self.ok("approval.grant", {"kind": "discount", "store_id": self.s["store"], "max_percent": 5,
                                                   "reason": "x"}).response
        self.credit = self.ok("approval.grant", {"kind": "credit_exception", "store_id": self.s["store"],
                                                 "max_amount": 35_000, "for_user_id": self.agent_user.user_id,
                                                 "reason": "ertaga to'laydi"}).response
        self.now = "2026-11-18T07:00:00.000Z"

    def check(self, aid, **kw):
        base = dict(kind="discount", now=self.now, user_id=self.agent_user.user_id, store_id=self.s["store"])
        base.update(kw)
        return check_approval(self.j.docs, aid, **base)

    def code(self, aid, **kw):
        with self.assertRaises(DomainError) as cm:
            self.check(aid, **kw)
        return cm.exception.code

    def test_valid(self):
        self.assertEqual(self.check(self.discount["approval_id"], percent=5).status, "open")
        self.assertEqual(self.check(self.credit["approval_id"], kind="credit_exception", amount=35_000).status, "open")

    def test_each_failure_has_its_own_code(self):
        d, c = self.discount["approval_id"], self.credit["approval_id"]
        self.assertEqual(self.code("AP-404"), "approval_not_found")
        self.assertEqual(self.code(d, kind="labor"), "approval_wrong_kind")
        self.assertEqual(self.code(d, store_id="S-999"), "approval_wrong_store")
        self.assertEqual(self.code(c, kind="credit_exception", user_id=self.wh_user.user_id), "approval_wrong_user")
        self.assertEqual(self.code(d, percent=6), "approval_amount_exceeded")
        self.assertEqual(self.code(c, kind="credit_exception", amount=35_001), "approval_amount_exceeded")
        self.assertEqual(self.code(d, now="2026-11-19T06:30:00.000Z"), "approval_expired")

    def test_exact_expiry_boundary(self):
        d = self.discount["approval_id"]
        exp = self.discount["expires_at"]
        self.assertEqual(self.check(d, now=exp, percent=5).status, "open")             # aynan tugash vaqtida hali amal qiladi
        self.assertEqual(self.code(d, now="2026-11-19T06:30:00.000Z"), "approval_expired")

    def test_used_and_revoked(self):
        d, c = self.discount["approval_id"], self.credit["approval_id"]
        with self.j.transaction():
            mark_approval_used(self.j.docs, d, "sale-event-1", self.now)
        self.assertEqual(self.code(d), "approval_used")
        stored = self.doc(f"approval:{d}")
        self.assertEqual((stored.status, stored.data["used_by_event"], stored.data["used_at"]), ("used", "sale-event-1", self.now))
        self.ok("approval.revoke", {"approval_id": c, "reason": "x"})
        self.assertEqual(self.code(c, kind="credit_exception"), "approval_revoked")

    def test_unrestricted_user_and_amount_limits(self):
        # for_user_id yo'q: istalgan xodim; max_amount yo'q (foizli chegirma) amount tekshirilmaydi
        d = self.discount["approval_id"]
        self.assertEqual(self.check(d, user_id=self.wh_user.user_id, amount=10_000_000, percent=5).status, "open")
