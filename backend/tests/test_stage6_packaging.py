from datetime import timedelta

from backend.balances import keys
from backend.balances.rebuild import verify
from backend.core.timing import local_date
from backend.modules import load_all
from backend.modules.packaging import queries

from .helpers import AppCase


class PackagingTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()

    def buy_raw(self, weight_g, unit_price_per_kg):
        total = weight_g * unit_price_per_kg // 1000
        return self.ok("purchase.raw", {
            "material_id": self.s["raw"],
            "weight_g": weight_g,
            "unit_price_per_kg": unit_price_per_kg,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
            "paid_amount": total,
        }).response

    def buy_pack(self, quantity, unit_price):
        return self.ok("purchase.packaging", {
            "material_id": self.s["pm"],
            "quantity": quantity,
            "unit_price": unit_price,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
        })

    def post(self, input_g=1_000, lines=None, **extra):
        payload = {
            "material_id": self.s["raw"],
            "input_g": input_g,
            "lines": lines or [{"product_id": self.s["product"], "quantity": 18}],
            "workers": [self.agent_user.user_id],
            "labor_cost": 0,
        }
        payload.update(extra)
        return self.run_cmd("packaging.post", payload)

    def test_batch_consumes_stock_and_allocates_cost_by_weight_and_units(self):
        self.buy_raw(10_000, 100_000)
        self.buy_pack(500, 500)
        second_product = self.ref("product", {
            "raw_material_id": self.s["raw"],
            "pack_g": 100,
            "materials": [{"material_id": self.s["pm"], "qty": 1}],
        })

        result = self.post(
            input_g=10_000,
            lines=[
                {"product_id": self.s["product"], "quantity": 80},
                {"product_id": second_product, "quantity": 40},
            ],
            waste_g=1_500,
            labor_cost=12_000,
        )
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        batch_id = result.response["batch_id"]
        self.assertEqual(result.response["output_weight_g"], 8_000)
        self.assertEqual(result.response["shrink_g"], 2_000)
        self.assertEqual(result.response["shrink_pct"], "20.00")
        self.assertEqual(self.bal(keys.raw(self.s["raw"])), 0)
        self.assertEqual(self.bal(keys.raw_value(self.s["raw"])), 0)
        self.assertEqual(self.bal(keys.pack(self.s["pm"])), 380)
        self.assertEqual(self.bal(keys.pack_value(self.s["pm"])), 190_000)
        self.assertEqual(self.bal(keys.fg(self.s["product"], batch_id)), 80)
        self.assertEqual(self.bal(keys.fg(second_product, batch_id)), 40)

        batch = queries.get_batch(self.j.docs, batch_id)
        self.assertEqual(batch["waste_g"], 1_500)
        self.assertEqual(batch["unclassified_loss_g"], 500)
        self.assertEqual(batch["source_purchase_ids"], ["PUR-261118-001"])
        self.assertEqual([line["unit_cost"] for line in batch["lines"]], [6_850, 13_100])
        self.assertEqual(batch["production_cost_total"], 1_072_000)
        expected_expiry = (local_date(batch["packaged_at"]) + timedelta(days=180)).isoformat()
        self.assertEqual(batch["expires_on"], expected_expiry)
        self.assertEqual(queries.expiring_batches(self.j.docs, expected_expiry, expected_expiry)[0]["batch_id"],
                         batch_id)

    def test_raw_source_purchase_ids_are_tracked_across_lots(self):
        first = self.buy_raw(5_000, 100_000)["purchase_id"]
        second = self.buy_raw(5_000, 200_000)["purchase_id"]
        self.buy_pack(200, 100)

        result = self.post(input_g=6_000, lines=[{"product_id": self.s["product"], "quantity": 100}])
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        batch = queries.get_batch(self.j.docs, result.response["batch_id"])
        self.assertEqual(batch["source_purchase_ids"], [first, second])
        self.assertEqual(batch["raw_lot_allocations"], [
            {"purchase_id": first, "weight_g": 5_000},
            {"purchase_id": second, "weight_g": 1_000},
        ])
        self.assertEqual(self.j.docs.get(f"raw_lot:{first}").status, "depleted")
        self.assertEqual(self.j.docs.get(f"raw_lot:{second}").data["remaining_g"], 4_000)
        self.assertEqual(batch["raw_cost_total"], 900_000)

    def test_shortage_invalid_yield_and_bad_waste_reject_without_posting(self):
        self.buy_raw(1_000, 100_000)
        self.buy_pack(1, 100)
        before = self.j.bal.snapshot()
        self.assertEqual(self.post(input_g=1_001).code, "insufficient_raw_stock")
        self.assertEqual(self.post(lines=[{"product_id": self.s["product"], "quantity": 19}]).code,
                         "insufficient_raw_stock")
        self.assertEqual(self.post(waste_g=101).code, "waste_exceeds_shrink")
        self.assertEqual(self.post(lines=[{"product_id": self.s["product"], "quantity": 21}]).code,
                         "output_exceeds_input")
        self.assertEqual(self.j.bal.snapshot(), before)
        self.assertEqual(queries.list_batches(self.j.docs), [])

    def test_packaging_material_shortage_and_malformed_lines_reject(self):
        self.buy_raw(1_000, 100_000)
        self.buy_pack(1, 100)
        self.assertEqual(self.post(lines=[{"product_id": self.s["product"], "quantity": 2}]).code,
                         "insufficient_packaging_stock")
        self.assertEqual(self.post(lines=[{"product_id": self.s["product"], "quantity": 1}] * 2).code,
                         "invalid_field")
        self.assertEqual(self.post(lines=[{"product_id": "P-404", "quantity": 1}]).code, "not_found")

    def test_packaging_packer_needs_single_use_owner_labor_approval(self):
        self.buy_raw(1_000, 100_000)
        self.buy_pack(100, 100)
        packer_user, packer = self.env.add_user("packer", ["packer"])
        approval = self.ok("approval.grant", {
            "kind": "labor",
            "max_amount": 1_000,
            "for_user_id": packer_user.user_id,
            "reason": "qadoqlash ish haqi",
        }).response["approval_id"]
        payload = {
            "material_id": self.s["raw"],
            "input_g": 1_000,
            "lines": [{"product_id": self.s["product"], "quantity": 18}],
            "workers": [packer_user.user_id],
            "labor_cost": 500,
        }
        self.assertEqual(self.run_cmd("packaging.post", payload, actor=packer).code, "missing_field")
        payload["approval_id"] = approval
        result = self.run_cmd("packaging.post", payload, actor=packer)
        self.assertEqual(result.status, "accepted", (result.code, result.message))
        self.assertEqual(self.doc(f"approval:{approval}").status, "used")
        self.assertEqual(self.run_cmd("packaging.post", payload, actor=packer).code, "approval_used")

    def test_shrink_statistics_wait_for_both_thresholds_then_alert_on_outlier(self):
        self.buy_raw(100_000, 100_000)
        self.buy_pack(2_000, 100)
        for _ in range(7):
            self.assertEqual(self.post(input_g=10_000,
                                       lines=[{"product_id": self.s["product"], "quantity": 180}]).status,
                             "accepted")
        summary = queries.shrink_summary(self.j.docs, self.s["raw"], self.e.current_policy())
        self.assertEqual(summary["status"], "collecting")
        self.assertEqual(summary["weighted_average_pct"], None)

        self.assertEqual(self.post(input_g=10_000,
                                   lines=[{"product_id": self.s["product"], "quantity": 180}]).status,
                         "accepted")
        summary = queries.shrink_summary(self.j.docs, self.s["raw"], self.e.current_policy())
        self.assertEqual((summary["status"], summary["batch_count"], summary["total_input_g"]), ("ready", 8, 80_000))
        self.assertEqual((summary["weighted_average_pct"], summary["median_pct"]), ("10.00", "10.00"))

        outlier = self.post(input_g=10_000, lines=[{"product_id": self.s["product"], "quantity": 80}])
        self.assertEqual(outlier.status, "accepted")
        alerts = [item for item in self.e.outbox.pending("owner") if item["kind"] == "shrink_deviation"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["payload"]["batch_id"], outlier.response["batch_id"])
        summary = queries.shrink_summary(self.j.docs, self.s["raw"], self.e.current_policy())
        self.assertEqual(summary["batch_count"], 9)
        self.assertEqual([item["batch_id"] for item in summary["outliers"]], [outlier.response["batch_id"]])

    def test_rebuild_recreates_batches_lot_consumption_and_cost_balances(self):
        self.buy_raw(2_000, 100_000)
        self.buy_pack(100, 100)
        result = self.post(input_g=1_000)
        self.assertEqual(result.status, "accepted")
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))

