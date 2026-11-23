from backend.balances import keys
from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.warehouse import queries

from .helpers import AppCase


class WarehouseTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()
        self.buy_raw(3_000)
        self.buy_pack(100)
        self.ok("price.set", {"product_id": self.s["product"], "price": 8_000, "reason": "yuk narxi"})
        self.first_batch = self.pack(1_000)
        self.env.clock.advance(days=1)
        self.second_batch = self.pack(1_000)

    def buy_raw(self, weight_g):
        total = weight_g * 100_000 // 1000
        self.ok("purchase.raw", {
            "material_id": self.s["raw"],
            "weight_g": weight_g,
            "unit_price_per_kg": 100_000,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
            "paid_amount": total,
        })

    def buy_pack(self, quantity):
        self.ok("purchase.packaging", {
            "material_id": self.s["pm"],
            "quantity": quantity,
            "unit_price": 100,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
        })

    def pack(self, input_g, quantity=20, **extra):
        return self.ok("packaging.post", {
            "material_id": self.s["raw"],
            "input_g": input_g,
            "lines": [{"product_id": self.s["product"], "quantity": quantity}],
            "workers": [self.agent_user.user_id],
            "labor_cost": 0,
            **extra,
        }).response["batch_id"]

    def prepare(self, quantity=25, actor=None, **extra):
        return self.run_cmd("transfer.prepare.loading", {
            "agent_id": self.agent_user.user_id,
            "lines": [{"product_id": self.s["product"], "quantity": quantity}],
            **extra,
        }, actor=actor or self.wh)

    def accept(self, transfer_id, actor=None):
        return self.run_cmd("transfer.accept.loading", {"transfer_id": transfer_id}, actor=actor or self.agent)

    def test_loading_uses_fifo_and_changes_custody_only_after_agent_accepts(self):
        result = self.prepare()
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        transfer_id = result.response["transfer_id"]
        self.assertEqual(result.response["total_amount"], 25 * 8_000)
        self.assertEqual(result.response["lines"][0]["allocations"], [
            {"batch_id": self.first_batch, "quantity": 20},
            {"batch_id": self.second_batch, "quantity": 5},
        ])
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.first_batch)), 20)
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.second_batch)), 20)
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.first_batch)), 0)
        self.assertEqual(queries.get_transfer(self.j.docs, transfer_id)["status"], "pending")

        self.assertEqual(self.accept(transfer_id, actor=self.wh).code, "forbidden")
        accepted = self.accept(transfer_id)
        self.assertEqual(accepted.status, "accepted", (accepted.code, accepted.message))
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.first_batch)), 0)
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.second_batch)), 15)
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.first_batch)), 20)
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.second_batch)), 5)
        self.assertEqual(sum(row["quantity"] for row in
                             queries.custody_inventory(self.j.bal, self.agent_user.user_id)), 25)
        self.assertEqual(queries.get_transfer(self.j.docs, transfer_id)["status"], "accepted")
        self.assertEqual(self.accept(transfer_id).code, "transfer_not_pending")

    def test_loading_rejects_missing_price_stock_and_invalid_recipient(self):
        self.assertEqual(self.prepare(quantity=41).code, "insufficient_finished_stock")
        self.assertEqual(self.prepare(agent_id="U-404").code, "invalid_field")
        product = self.ref("product", {"raw_material_id": self.s["raw"], "pack_g": 25})
        self.assertEqual(self.prepare(lines=[{"product_id": product, "quantity": 1}]).code, "price_not_set")
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.first_batch)), 20)

    def test_expired_batches_cannot_be_loaded(self):
        initial = self.prepare(quantity=40)
        self.assertEqual(initial.status, "accepted")
        self.assertEqual(self.accept(initial.response["transfer_id"]).status, "accepted")
        short_lived = self.pack(500, quantity=10, shelf_life_days=1)
        self.env.clock.advance(days=2)
        self.assertEqual(self.prepare(quantity=1).code, "insufficient_finished_stock")
        self.assertEqual(self.bal(keys.fg(self.s["product"], short_lived)), 10)

    def test_pending_loading_cannot_be_accepted_after_batch_expiry(self):
        batch_id = self.pack(500, quantity=10, shelf_life_days=1)
        prepared = self.prepare(quantity=1)
        self.assertEqual(prepared.status, "accepted")
        self.assertEqual(prepared.response["lines"][0]["allocations"], [
            {"batch_id": self.first_batch, "quantity": 1},
        ])
        self.env.clock.advance(days=180)
        self.assertEqual(self.accept(prepared.response["transfer_id"]).code, "batch_expired")
        self.assertEqual(self.bal(keys.fg(self.s["product"], batch_id)), 10)

    def test_inventory_count_waits_for_owner_and_applies_signed_difference(self):
        transfer = self.prepare().response["transfer_id"]
        self.assertEqual(self.accept(transfer).status, "accepted")
        submit = self.run_cmd("stock_count.submit", {
            "location": "warehouse",
            "product_id": self.s["product"],
            "batch_id": self.second_batch,
            "counted_quantity": 12,
            "reason": "jismoniy sanash",
        }, actor=self.wh)
        self.assertEqual(submit.status, "accepted", (submit.code, submit.message))
        count_id = submit.response["count_id"]
        self.assertEqual(submit.response["difference"], -3)
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.second_batch)), 15)
        self.assertTrue(any(item["kind"] == "inventory_difference"
                            for item in self.e.outbox.pending("owner")))
        self.assertEqual(self.run_cmd("stock_count.approve", {"count_id": count_id}, actor=self.wh).code,
                         "forbidden")
        approved = self.run_cmd("stock_count.approve", {"count_id": count_id}, actor=self.owner)
        self.assertEqual(approved.status, "accepted", (approved.code, approved.message))
        self.assertEqual(self.bal(keys.fg(self.s["product"], self.second_batch)), 12)
        self.assertEqual(queries.get_stock_count(self.j.docs, count_id)["status"], "approved")

    def test_inventory_count_requires_reason_and_rejects_stale_approval(self):
        self.assertEqual(self.run_cmd("stock_count.submit", {
            "location": "warehouse",
            "product_id": self.s["product"],
            "batch_id": self.first_batch,
            "counted_quantity": 19,
        }, actor=self.wh).code, "missing_field")
        submitted = self.run_cmd("stock_count.submit", {
            "location": "warehouse",
            "product_id": self.s["product"],
            "batch_id": self.first_batch,
            "counted_quantity": 19,
            "reason": "qayta sanash",
        }, actor=self.wh)
        self.assertEqual(submitted.status, "accepted")
        transfer = self.prepare(quantity=1).response["transfer_id"]
        self.assertEqual(self.accept(transfer).status, "accepted")
        result = self.run_cmd("stock_count.approve", {
            "count_id": submitted.response["count_id"],
        }, actor=self.owner)
        self.assertEqual((result.status, result.code), ("conflict", "stock_changed_since_count"))

    def test_custody_inventory_can_be_counted_separately(self):
        transfer = self.prepare(quantity=5).response["transfer_id"]
        self.assertEqual(self.accept(transfer).status, "accepted")
        submit = self.run_cmd("stock_count.submit", {
            "location": "custody",
            "user_id": self.agent_user.user_id,
            "product_id": self.s["product"],
            "batch_id": self.first_batch,
            "counted_quantity": 4,
            "reason": "bitta qadoq yo'q",
        })
        self.assertEqual(submit.status, "accepted")
        self.assertEqual(self.run_cmd("stock_count.approve", {
            "count_id": submit.response["count_id"],
        }).status, "accepted")
        self.assertEqual(self.bal(keys.custody(self.agent_user.user_id, self.s["product"], self.first_batch)), 4)

    def test_loading_and_inventory_projection_rebuild_matches_ledger(self):
        transfer = self.prepare().response["transfer_id"]
        self.assertEqual(self.accept(transfer).status, "accepted")
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))

