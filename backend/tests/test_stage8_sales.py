from backend.balances import keys
from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.sales import queries

from .helpers import AppCase


class SalesTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.buy_raw(2_000)
        self.buy_pack(100)
        self.ok("price.set", {"product_id": self.s["product"], "price": 8_000, "reason": "boshlang'ich narx"})
        result = self.ok("packaging.post", {
            "material_id": self.s["raw"],
            "input_g": 1_000,
            "lines": [{"product_id": self.s["product"], "quantity": 18}],
            "workers": [self.agent_user.user_id],
            "labor_cost": 0,
        })
        self.batch_id = result.response["batch_id"]
        self.transfer_id = self.ok("transfer.prepare.loading", {
            "agent_id": self.agent_user.user_id,
            "lines": [{"product_id": self.s["product"], "quantity": 18}],
        }).response["transfer_id"]
        self.ok("transfer.accept.loading", {"transfer_id": self.transfer_id}, actor=self.agent)

    def buy_raw(self, weight_g):
        amount = weight_g * 100_000 // 1_000
        self.ok("purchase.raw", {
            "material_id": self.s["raw"],
            "weight_g": weight_g,
            "unit_price_per_kg": 100_000,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
            "paid_amount": amount,
        })

    def buy_pack(self, quantity):
        self.ok("purchase.packaging", {
            "material_id": self.s["pm"],
            "quantity": quantity,
            "unit_price": 100,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
        })

    def sale(self, quantity=1, **extra):
        payload = {
            "store_id": self.s["store"],
            "lines": [{"product_id": self.s["product"], "quantity": quantity}],
            "payment_type": "credit",
        }
        payload.update(extra)
        return self.run_cmd("sale.create", payload, actor=self.agent)

    def grant(self, **payload):
        return self.ok("approval.grant", payload).response["approval_id"]

    def test_sale_snapshots_price_consumes_fifo_custody_and_uses_discount_approval(self):
        approval_id = self.grant(
            kind="discount", store_id=self.s["store"], max_percent=10, reason="aksiya")
        result = self.sale(quantity=2, discount_percent=10, discount_approval_id=approval_id)
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        sale_id = result.response["sale_id"]
        self.assertEqual((result.response["subtotal"], result.response["discount_amount"],
                          result.response["total_amount"], result.response["credit_amount"]),
                         (16_000, 1_600, 14_400, 14_400))
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.batch_id)), 16)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 14_400)
        self.assertEqual(self.doc(f"approval:{approval_id}").status, "used")
        self.ok("price.set", {"product_id": self.s["product"], "price": 9_000, "reason": "narx yangilandi"})
        sale = queries.get_sale(self.j.docs, sale_id)
        self.assertEqual(sale["lines"][0]["unit_price"], 8_000)
        self.assertEqual(sale["status"], "open")

    def test_discount_and_credit_limit_require_scoped_single_use_approvals(self):
        self.ok("policy.update", {"changes": {"credit_limit": 10_000}, "reason": "test limiti"})
        rejected = self.sale(quantity=2)
        self.assertEqual((rejected.status, rejected.code), ("conflict", "credit_limit_exceeded"))

        approval_id = self.grant(
            kind="credit_exception", store_id=self.s["store"], max_amount=8_000, reason="istisno")
        accepted = self.sale(quantity=2, credit_exception_id=approval_id)
        self.assertEqual(accepted.status, "accepted", (accepted.code, accepted.message))
        self.assertEqual(accepted.response["credit_amount"], 16_000)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 16_000)
        self.assertEqual(self.doc(f"approval:{approval_id}").status, "used")
        self.assertEqual(self.sale(credit_exception_id=approval_id).code, "approval_used")

    def test_store_payment_allocates_oldest_debt_and_excess_becomes_advance(self):
        first = self.sale(quantity=1).response["sale_id"]
        self.env.clock.advance(days=1)
        second = self.sale(quantity=2).response["sale_id"]
        payment = self.run_cmd("payment.receive", {
            "store_id": self.s["store"], "amount": 20_000, "payment_type": "card",
        }, actor=self.agent)
        self.assertEqual(payment.status, "accepted", (payment.code, payment.message))
        self.assertEqual(payment.response["allocations"], [
            {"sale_id": first, "amount": 8_000},
            {"sale_id": second, "amount": 12_000},
        ])
        self.assertEqual(payment.response["advance_added"], 0)
        self.assertEqual(queries.get_sale(self.j.docs, first)["status"], "paid")
        self.assertEqual(queries.get_sale(self.j.docs, second)["outstanding"], 4_000)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 4_000)
        self.assertEqual(self.bal(keys.BANK_UNMATCHED), 20_000)

        extra = self.run_cmd("payment.receive", {
            "store_id": self.s["store"], "amount": 6_000, "payment_type": "cash",
        }, actor=self.agent)
        self.assertEqual(extra.status, "accepted", (extra.code, extra.message))
        self.assertEqual(extra.response["advance_added"], 2_000)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 0)
        self.assertEqual(self.bal(keys.advance(self.s["store"])), 2_000)
        self.assertEqual(self.bal(keys.agent_cash(self.agent_user.user_id)), 6_000)

    def test_sale_uses_store_advance_before_creating_credit(self):
        self.add_balance(keys.advance(self.s["store"]), 10_000)
        result = self.sale(quantity=1)
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        self.assertEqual((result.response["advance_used"], result.response["credit_amount"]),
                         (8_000, 0))
        self.assertEqual(self.bal(keys.advance(self.s["store"])), 2_000)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 0)

    def test_cash_sale_is_collected_by_agent_and_credit_can_be_extended(self):
        cash = self.sale(quantity=1, payment_type="cash")
        self.assertEqual(cash.status, "accepted", (cash.code, cash.message))
        self.assertEqual(cash.response["credit_amount"], 0)
        self.assertEqual(self.bal(keys.agent_cash(self.agent_user.user_id)), 8_000)

        debt = self.sale(quantity=1)
        sale_id = debt.response["sale_id"]
        original_due = queries.get_sale(self.j.docs, sale_id)["due_date"]
        self.assertEqual(original_due, "2026-12-02")
        extended = self.run_cmd("term.extend", {
            "sale_id": sale_id, "additional_days": 7, "reason": "do'kon so'rovi",
        })
        self.assertEqual(extended.status, "accepted", (extended.code, extended.message))
        self.assertEqual(extended.response["new_due_date"], "2026-12-09")
        self.assertEqual(queries.get_sale(self.j.docs, sale_id)["term_extensions"][0]["reason"],
                         "do'kon so'rovi")

    def test_dispute_is_audited_and_does_not_silently_change_debt(self):
        sale_id = self.sale(quantity=1).response["sale_id"]
        result = self.run_cmd("dispute.open", {
            "sale_id": sale_id, "reason": "miqdor bo'yicha e'tiroz",
        })
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        self.assertEqual(result.response["resolution_due"], "2026-11-20")
        dispute_id = result.response["dispute_id"]
        self.assertEqual(self.run_cmd("dispute.open", {
            "sale_id": sale_id, "reason": "takroriy e'tiroz",
        }).code, "dispute_already_open")
        before = self.bal(keys.debt(self.s["store"]))
        resolved = self.run_cmd("dispute.resolve", {
            "dispute_id": dispute_id, "decision": "accepted", "reason": "egasi ko'rib chiqdi",
        })
        self.assertEqual(resolved.status, "accepted", (resolved.code, resolved.message))
        self.assertEqual(queries.get_dispute(self.j.docs, dispute_id)["status"], "resolved")
        self.assertEqual(self.bal(keys.debt(self.s["store"])), before)

    def test_expired_custody_stock_cannot_be_sold(self):
        self.buy_raw(500)
        self.buy_pack(20)
        batch_id = self.ok("packaging.post", {
            "material_id": self.s["raw"],
            "input_g": 500,
            "lines": [{"product_id": self.s["product"], "quantity": 9}],
            "workers": [self.agent_user.user_id],
            "labor_cost": 0,
            "shelf_life_days": 1,
        }).response["batch_id"]
        transfer_id = self.ok("transfer.prepare.loading", {
            "agent_id": self.agent_user.user_id,
            "lines": [{"product_id": self.s["product"], "quantity": 1}],
        }).response["transfer_id"]
        self.ok("transfer.accept.loading", {"transfer_id": transfer_id}, actor=self.agent)
        self.env.clock.advance(days=2)
        result = self.sale(quantity=19)
        self.assertEqual(result.code, "insufficient_custody_stock")
        self.assertEqual(result.details["available"], 18)
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], batch_id)), 1)

    def test_sale_validation_permissions_and_projection_rebuild(self):
        self.assertEqual(self.sale(quantity=99).code, "insufficient_custody_stock")
        other_user = self.env.add_user("other-agent", ["agent"])[0]
        other_store = self.ref("store", {"name": "Other Market", "agent_id": other_user.user_id,
                                        "phone": "+998 90 555 55 55", "lang": "uz"})
        self.assertEqual(self.sale(store_id=other_store).code, "store_not_assigned")
        self.assertEqual(self.run_cmd("sale.create", {
            "store_id": self.s["store"],
            "lines": [{"product_id": self.s["product"], "quantity": 1}],
            "payment_type": "credit",
        }, actor=self.wh).code, "forbidden")
        self.assertEqual(self.sale(unexpected=1).code, "unknown_field")

        sale = self.sale(quantity=2).response["sale_id"]
        self.run_cmd("payment.receive", {
            "store_id": self.s["store"], "amount": 3_000, "payment_type": "cash",
        }, actor=self.agent)
        self.run_cmd("term.extend", {
            "sale_id": sale, "additional_days": 2, "reason": "sinov",
        })
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))
