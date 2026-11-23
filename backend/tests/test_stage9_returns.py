from backend.balances import keys
from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.returns import queries
from backend.modules.sales import queries as sales_queries

from .helpers import AppCase


class ReturnsTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.buy_raw(2_000)
        self.buy_pack(100)
        self.ok("price.set", {"product_id": self.s["product"], "price": 8_000, "reason": "boshlang'ich"})
        self.batch_id = self.ok("packaging.post", {
            "material_id": self.s["raw"],
            "input_g": 1_000,
            "lines": [{"product_id": self.s["product"], "quantity": 20}],
            "workers": [self.agent_user.user_id],
            "labor_cost": 0,
        }).response["batch_id"]
        transfer_id = self.ok("transfer.prepare.loading", {
            "agent_id": self.agent_user.user_id,
            "lines": [{"product_id": self.s["product"], "quantity": 20}],
        }).response["transfer_id"]
        self.ok("transfer.accept.loading", {"transfer_id": transfer_id}, actor=self.agent)

    def buy_raw(self, weight_g):
        self.ok("purchase.raw", {
            "material_id": self.s["raw"],
            "weight_g": weight_g,
            "unit_price_per_kg": 100_000,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
            "paid_amount": weight_g * 100,
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
        return self.ok("sale.create", payload, actor=self.agent).response["sale_id"]

    def declare(self, sale_id, quantity=1):
        return self.run_cmd("return.declare", {
            "sale_id": sale_id,
            "lines": [{"product_id": self.s["product"], "quantity": quantity}],
            "reason": "do'kon qaytardi",
        }, actor=self.agent)

    def accept(self, return_id, disposition="restock"):
        return self.run_cmd("return.accept", {
            "return_id": return_id, "disposition": disposition,
        }, actor=self.wh)

    def test_return_is_held_then_restocked_and_settled_against_invoice_debt(self):
        discount_approval = self.ok("approval.grant", {
            "kind": "discount", "store_id": self.s["store"],
            "max_percent": 10, "reason": "qaytim testi",
        }).response["approval_id"]
        sale_id = self.sale(quantity=2, discount_percent=10,
                            discount_approval_id=discount_approval)
        declared = self.declare(sale_id)
        self.assertEqual(declared.status, "accepted", (declared.code, declared.message))
        return_id = declared.response["return_id"]
        self.assertEqual(declared.response["return_amount"], 7_200)
        self.assertEqual(self.bal(keys.hold(self.agent_user.user_id, self.s["product"], self.batch_id)), 1)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 14_400)

        accepted = self.accept(return_id)
        self.assertEqual(accepted.status, "accepted", (accepted.code, accepted.message))
        self.assertEqual(self.bal(keys.hold(self.agent_user.user_id, self.s["product"], self.batch_id)), 0)
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.batch_id)), 1)
        settled = self.ok("return.settle", {"return_id": return_id})
        self.assertEqual((settled.response["debt_reduction"], settled.response["advance_added"]),
                         (7_200, 0))
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 7_200)
        self.assertEqual(sales_queries.get_sale(self.j.docs, sale_id)["outstanding"], 7_200)
        self.assertEqual(queries.get_return(self.j.docs, return_id)["status"], "settled")
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))

    def test_paid_sale_return_becomes_store_advance(self):
        sale_id = self.sale(payment_type="cash")
        returned = self.declare(sale_id).response["return_id"]
        self.assertEqual(self.accept(returned, "quarantine").status, "accepted")
        result = self.ok("return.settle", {"return_id": returned})
        self.assertEqual((result.response["debt_reduction"], result.response["advance_added"]),
                         (0, 8_000))
        self.assertEqual(self.bal(keys.advance(self.s["store"])), 8_000)
        self.assertEqual(self.bal(keys.quarantine(self.s["product"], self.batch_id)), 1)

    def test_return_cannot_exceed_sold_quantity_or_be_settled_before_acceptance(self):
        sale_id = self.sale(quantity=2)
        too_many = self.declare(sale_id, quantity=3)
        self.assertEqual(too_many.code, "return_quantity_exceeded")
        returned = self.declare(sale_id).response["return_id"]
        self.assertEqual(self.run_cmd("return.settle", {"return_id": returned}).code,
                         "return_not_accepted")
        self.assertEqual(self.accept(returned, "restock").status, "accepted")
        self.assertEqual(self.accept(returned, "restock").code, "return_not_pending")

    def test_exchange_credits_return_against_old_debt_and_replacement_sale(self):
        original_sale = self.sale(quantity=2)
        return_id = self.declare(original_sale).response["return_id"]
        self.assertEqual(self.accept(return_id, "restock").status, "accepted")
        result = self.ok("exchange.approve", {
            "return_id": return_id,
            "lines": [{"product_id": self.s["product"], "quantity": 1}],
        })
        exchange_id = result.response["exchange_id"]
        replacement_id = result.response["replacement_sale_id"]
        self.assertEqual(result.response["debt_reduction"], 8_000)
        self.assertEqual(result.response["credit_amount"], 8_000)
        self.assertEqual(queries.get_exchange(self.j.docs, exchange_id)["status"], "approved")
        self.assertEqual(queries.get_return(self.j.docs, return_id)["status"], "exchanged")
        self.assertEqual(sales_queries.get_sale(self.j.docs, original_sale)["outstanding"], 8_000)
        self.assertEqual(sales_queries.get_sale(self.j.docs, replacement_id)["outstanding"], 8_000)
        self.assertEqual(self.bal(keys.debt(self.s["store"])), 16_000)
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.batch_id)), 17)
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.batch_id)), 1)

    def test_quarantined_return_can_only_be_written_off_by_owner(self):
        sale_id = self.sale()
        return_id = self.declare(sale_id).response["return_id"]
        self.assertEqual(self.accept(return_id, "quarantine").status, "accepted")
        payload = {"product_id": self.s["product"], "batch_id": self.batch_id,
                   "quantity": 1, "reason": "shikastlangan"}
        self.assertEqual(self.run_cmd("writeoff.approve", payload, actor=self.wh).code, "forbidden")
        result = self.ok("writeoff.approve", payload)
        self.assertEqual(result.response["quantity"], 1)
        self.assertEqual(self.bal(keys.quarantine(self.s["product"], self.batch_id)), 0)

    def test_agent_stock_return_requires_warehouse_acceptance(self):
        prepared = self.run_cmd("transfer.prepare.custody_return", {
            "lines": [{"product_id": self.s["product"], "quantity": 3}],
        }, actor=self.agent)
        self.assertEqual(prepared.status, "accepted", (prepared.code, prepared.message))
        transfer_id = prepared.response["transfer_id"]
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.batch_id)), 20)
        self.assertEqual(self.run_cmd("transfer.accept.custody_return", {
            "transfer_id": transfer_id, "disposition": "restock",
        }, actor=self.agent).code, "forbidden")
        accepted = self.run_cmd("transfer.accept.custody_return", {
            "transfer_id": transfer_id, "disposition": "restock",
        }, actor=self.wh)
        self.assertEqual(accepted.status, "accepted", (accepted.code, accepted.message))
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.batch_id)), 17)
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.batch_id)), 3)
