from backend.balances import keys
from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.closing import queries

from .helpers import AppCase


class ClosingTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.ok("purchase.raw", {
            "material_id": self.s["raw"], "weight_g": 1_000, "unit_price_per_kg": 100_000,
            "supplier_id": self.s["supplier"], "payment_type": "credit", "paid_amount": 0,
        })
        self.ok("purchase.packaging", {
            "material_id": self.s["pm"], "quantity": 20, "unit_price": 100,
            "supplier_id": self.s["supplier"], "payment_type": "credit",
        })
        self.ok("price.set", {"product_id": self.s["product"], "price": 8_000, "reason": "narx"})
        batch_id = self.ok("packaging.post", {
            "material_id": self.s["raw"], "input_g": 500,
            "lines": [{"product_id": self.s["product"], "quantity": 10}],
            "workers": [self.agent_user.user_id], "labor_cost": 0,
        }).response["batch_id"]
        transfer_id = self.ok("transfer.prepare.loading", {
            "agent_id": self.agent_user.user_id,
            "lines": [{"product_id": self.s["product"], "quantity": 10}],
        }).response["transfer_id"]
        self.ok("transfer.accept.loading", {"transfer_id": transfer_id}, actor=self.agent)
        self.batch_id = batch_id

    def sale(self, payment_type):
        return self.ok("sale.create", {
            "store_id": self.s["store"],
            "lines": [{"product_id": self.s["product"], "quantity": 1}],
            "payment_type": payment_type,
        }, actor=self.agent).response["sale_id"]

    def test_daily_close_snapshots_four_balances_and_cash_handover_is_two_party(self):
        self.sale("cash")
        self.sale("credit")
        self.ok("return.declare", {
            "sale_id": self.j.docs.query("sale", ref=self.s["store"])[0].data["sale_id"],
            "lines": [{"product_id": self.s["product"], "quantity": 1}],
            "reason": "kun yakunidagi qaytim",
        }, actor=self.agent)

        submitted = self.run_cmd("closing.submit", {
            "cash_counted": 8_000,
            "cash_handover_amount": 5_000,
        }, actor=self.agent)
        self.assertEqual(submitted.status, "accepted", (submitted.code, submitted.message))
        close_id = submitted.response["closing_id"]
        transfer_id = submitted.response["cash_handover_id"]
        balances = submitted.response["balances"]
        self.assertEqual(set(balances), {"agent_cash", "custody_stock", "store_balances", "held_returns"})
        self.assertEqual(balances["agent_cash"], {"expected": 8_000, "counted": 8_000, "difference": 0})
        self.assertEqual(balances["custody_stock"]["quantity"], 8)
        self.assertEqual(balances["store_balances"]["debt"], 8_000)
        self.assertEqual(balances["held_returns"]["quantity"], 1)
        self.assertEqual(self.bal(keys.agent_cash(self.agent_user.user_id)), 8_000)

        self.env.clock.advance(days=1)
        self.assertEqual(self.run_cmd("closing.submit", {
            "cash_counted": 8_000,
        }, actor=self.agent).code, "pending_cash_handover")
        accountant = self.env.add_user("accountant", ["accountant"])[1]
        accepted = self.run_cmd("transfer.accept.cash_handover", {"transfer_id": transfer_id},
                                actor=accountant)
        self.assertEqual(accepted.status, "accepted", (accepted.code, accepted.message))
        self.assertEqual(self.bal(keys.agent_cash(self.agent_user.user_id)), 3_000)
        self.assertEqual(self.bal(keys.CASH), 5_000)
        self.assertEqual(queries.get_closing(self.j.docs, close_id)["cash_handover_status"], "accepted")
        self.assertEqual(queries.get_cash_handover(self.j.docs, transfer_id)["status"], "accepted")
        self.assertTrue(any(item["kind"] == "daily_closing_submitted"
                            for item in self.e.outbox.pending("owner")))
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))

    def test_cash_difference_requires_reason_and_handover_cannot_exceed_count(self):
        self.sale("cash")
        missing_reason = self.run_cmd("closing.submit", {"cash_counted": 7_000}, actor=self.agent)
        self.assertEqual(missing_reason.code, "missing_field")
        too_much = self.run_cmd("closing.submit", {
            "cash_counted": 7_000,
            "cash_handover_amount": 7_001,
            "reason": "kassada farq",
        }, actor=self.agent)
        self.assertEqual(too_much.code, "invalid_field")
        result = self.run_cmd("closing.submit", {
            "cash_counted": 7_000,
            "cash_handover_amount": 7_000,
            "reason": "ming so'm kam chiqdi",
        }, actor=self.agent)
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        self.assertEqual(result.response["cash_difference"], -1_000)
        self.assertTrue(any(item["kind"] == "closing_cash_difference"
                            for item in self.e.outbox.pending("owner")))

    def test_agent_cannot_submit_more_than_one_close_per_business_day(self):
        result = self.run_cmd("closing.submit", {"cash_counted": 0}, actor=self.agent)
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        repeated = self.run_cmd("closing.submit", {"cash_counted": 0}, actor=self.agent)
        self.assertEqual(repeated.code, "closing_already_submitted")
