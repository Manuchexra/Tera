from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.pricing import queries as pq

from .helpers import AppCase


class PriceTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.pid = self.s["product"]

    def set_price(self, price, reason="narx yangilandi", **kw):
        return self.run_cmd("price.set", {"product_id": self.pid, "price": price, "reason": reason}, **kw)

    def test_set_first_price(self):
        r = self.set_price(8_000)
        self.assertEqual(r.status, "accepted")
        self.assertEqual(r.response, {"product_id": self.pid, "price": 8_000, "previous_price": None})
        self.assertEqual(pq.current_price(self.j.docs, self.pid), 8_000)

    def test_history_keeps_every_change_with_who_and_why(self):
        self.set_price(8_000, "boshlang'ich")
        self.set_price(8_500, "xom ashyo qimmatladi")
        h = pq.history(self.j.docs, self.pid)
        self.assertEqual([x["price"] for x in h], [8_000, 8_500])
        self.assertEqual((h[1]["by"], h[1]["reason"]), (self.owner.user_id, "xom ashyo qimmatladi"))
        ev = [e for e in self.j.ledger.iter_events() if e.type == "price_changed"][-1]
        self.assertEqual(ev.payload["previous_price"], 8_000)

    def test_unchanged_price_rejected(self):
        self.set_price(8_000)
        self.assertEqual(self.set_price(8_000).code, "price_unchanged")

    def test_validation(self):
        for payload, code in [
            ({"product_id": self.pid, "price": 0, "reason": "x"}, "invalid_field"),
            ({"product_id": self.pid, "price": -5, "reason": "x"}, "invalid_field"),
            ({"product_id": self.pid, "price": 8000.5, "reason": "x"}, "invalid_field"),
            ({"product_id": self.pid, "price": "8000", "reason": "x"}, "invalid_field"),
            ({"product_id": self.pid, "price": 8000}, "missing_field"),                 # sabab majburiy
            ({"product_id": self.pid, "price": 8000, "reason": "  "}, "missing_field"),
            ({"price": 8000, "reason": "x"}, "missing_field"),
            ({"product_id": "P-404", "price": 8000, "reason": "x"}, "not_found"),
        ]:
            self.assertEqual(self.run_cmd("price.set", payload).code, code, payload)

    def test_inactive_product_cannot_get_price(self):
        self.ok("reference.upsert", {"kind": "product", "id": self.pid, "data": {"active": False}})
        self.assertEqual(self.set_price(8_000).code, "inactive_reference")

    def test_only_owner_and_online(self):
        for actor in (self.agent, self.wh):
            self.assertEqual(self.set_price(8_000, actor=actor).code, "forbidden")
        self.assertEqual(self.set_price(8_000, device_time="2026-11-18T05:00:00.000Z").code, "online_required")
        self.assertIsNone(pq.current_price(self.j.docs, self.pid))

    def test_price_at_returns_price_in_force(self):
        self.set_price(8_000)
        t1 = self.j.ledger.get("c%d" % self._n).occurred_at
        self.env.clock.advance(hours=2)
        self.set_price(9_000)
        t2 = self.j.ledger.get("c%d" % self._n).occurred_at
        self.assertIsNone(pq.price_at(self.j.docs, self.pid, "2026-11-18T05:59:00.000Z"))   # birinchi narxdan oldin
        self.assertEqual(pq.price_at(self.j.docs, self.pid, t1), 8_000)
        self.assertEqual(pq.price_at(self.j.docs, self.pid, "2026-11-18T07:00:00.000Z"), 8_000)
        self.assertEqual(pq.price_at(self.j.docs, self.pid, t2), 9_000)
        self.assertEqual(pq.price_at(self.j.docs, "P-404", t2), None)

    def test_old_sales_keep_their_price_by_time_lookup(self):
        """Narx o'zgargandan keyin ham eski vaqt uchun eski narx topiladi (sotuv narxni nusxalaydi)."""
        self.set_price(8_000)
        before = self.env.clock.now()
        self.env.clock.advance(minutes=30)
        self.set_price(10_000)
        self.assertEqual(pq.price_at(self.j.docs, self.pid, before), 8_000)
        self.assertEqual(pq.current_price(self.j.docs, self.pid), 10_000)

    def test_markup_is_on_cost(self):
        self.assertEqual(pq.markup_pct(2_000, 1_179), "69.6")
        self.assertEqual(pq.markup_pct(1_179, 1_179), "0.0")
        self.assertEqual(pq.markup_pct(1_000, 1_179), "-15.2")

    def test_rebuild_matches(self):
        self.set_price(8_000)
        self.set_price(8_500)
        self.set_price(8_500)                                    # rad etiladi
        load_all()
        rep = verify(self.env.db)
        self.assertTrue(rep.ok, (rep.balance_diffs, rep.doc_diffs))


class PolicyTests(AppCase):
    def update(self, changes, reason="egasi qarori", **kw):
        return self.run_cmd("policy.update", {"changes": changes, "reason": reason}, **kw)

    def test_default_then_update(self):
        self.assertEqual(self.e.current_policy().credit_limit, 2_000_000)
        r = self.update({"credit_limit": 3_000_000, "payment_term_days": 21})
        self.assertEqual((r.status, r.response["changed"]), ("accepted", ["credit_limit", "payment_term_days"]))
        p = self.e.current_policy()
        self.assertEqual((p.credit_limit, p.payment_term_days, p.dispute_working_days), (3_000_000, 21, 2))

    def test_event_keeps_previous_values_and_reason(self):
        self.update({"credit_limit": 3_000_000}, "yirik do'konlar uchun")
        ev = [e for e in self.j.ledger.iter_events() if e.type == "policy_changed"][0]
        self.assertEqual((ev.payload["previous"], ev.payload["changes"], ev.payload["reason"]),
                         ({"credit_limit": 2_000_000}, {"credit_limit": 3_000_000}, "yirik do'konlar uchun"))

    def test_only_changed_keys_recorded(self):
        r = self.update({"credit_limit": 2_000_000, "payment_term_days": 10})
        self.assertEqual(r.response["changed"], ["payment_term_days"])

    def test_no_op_and_reason_required(self):
        self.assertEqual(self.update({"credit_limit": 2_000_000}).code, "no_changes")
        self.assertEqual(self.run_cmd("policy.update", {"changes": {"credit_limit": 5}}).code, "missing_field")

    def test_invalid_changes(self):
        for changes in [{}, None, {"nonsense": 1}, {"credit_limit": "5"}, {"credit_limit": -1}, {"credit_limit": 1.5},
                        {"payment_term_days": 0}, {"payment_term_days": 1400}, {"day_close_deadline": "25:00"},
                        {"day_close_deadline": 2100}, {"clock_ok_seconds": 600, "clock_max_seconds": 120},
                        {"payment_term_days": 60}]:                    # xavfli qarz yoshi (31) muddatdan kam bo'lib qoladi
            r = self.update(changes)
            self.assertEqual(r.code, "invalid_field", changes)
        self.assertEqual(self.e.current_policy().credit_limit, 2_000_000)

    def test_cross_field_check_uses_current_values(self):
        self.update({"risky_debt_age_days": 60})
        self.assertEqual(self.update({"payment_term_days": 60}).status, "accepted")      # endi 60 <= 60

    def test_only_owner_and_online(self):
        self.assertEqual(self.update({"credit_limit": 1}, actor=self.agent).code, "forbidden")
        self.assertEqual(self.update({"credit_limit": 1}, actor=self.wh).code, "forbidden")
        self.assertEqual(self.update({"credit_limit": 1}, device_time="2026-11-18T05:00:00.000Z").code, "online_required")

    def test_new_policy_applies_to_next_commands_and_survives_rebuild(self):
        self.update({"clock_ok_seconds": 60, "clock_max_seconds": 3600})
        self.assertEqual(self.e.current_policy().clock_ok_seconds, 60)
        # endi 2 daqiqalik farq "shubhali"
        self.env.clock.advance(seconds=1)
        r = self.run_cmd("reference.upsert", {"kind": "material", "data": {"name": "X"}},
                         device_time="2026-11-18T05:58:00.000Z")
        self.assertEqual(r.code, "online_required")
        load_all()
        self.assertTrue(verify(self.env.db).ok)
        self.assertEqual(self.j.docs.get("policy:global").data, {"clock_ok_seconds": 60, "clock_max_seconds": 3600})
