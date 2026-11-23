from backend.balances import keys
from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.purchase import queries

from .helpers import AppCase


class PurchaseTests(AppCase):
    def setUp(self):
        super().setUp()
        self.s = self.seed()

    def raw(self, actor=None, **overrides):
        payload = {
            "material_id": self.s["raw"],
            "weight_g": 1_000,
            "unit_price_per_kg": 100_000,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
            "paid_amount": 100_000,
        }
        payload.update(overrides)
        return self.run_cmd("purchase.raw", payload, actor=actor)

    def packaging(self, **overrides):
        payload = {
            "material_id": self.s["pm"],
            "quantity": 10,
            "unit_price": 500,
            "supplier_id": self.s["supplier"],
            "payment_type": "cash",
        }
        payload.update(overrides)
        return self.run_cmd("purchase.packaging", payload)

    def test_raw_purchase_calculates_total_and_includes_road_cost_in_average(self):
        result = self.raw(road_cost=10_000)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.response, {
            "purchase_id": "PUR-261118-001",
            "goods_total": 100_000,
            "paid_amount": 100_000,
            "supplier_debt_added": 0,
            "road_cost": 10_000,
            "inventory_value_added": 110_000,
        })
        self.assertEqual(self.bal(keys.raw(self.s["raw"])), 1_000)
        self.assertEqual(self.bal(keys.raw_value(self.s["raw"])), 110_000)
        self.assertEqual(self.bal(keys.CASH), -110_000)
        self.assertEqual(queries.average_raw_cost_per_kg(self.j.bal, self.s["raw"]), 110_000)

    def test_raw_purchase_uses_weighted_average_and_unpaid_amount_becomes_debt(self):
        self.raw(payment_type="credit", paid_amount=0)
        self.raw(weight_g=1_000, unit_price_per_kg=200_000, paid_amount=100_000)
        self.assertEqual(self.bal(keys.raw(self.s["raw"])), 2_000)
        self.assertEqual(self.bal(keys.raw_value(self.s["raw"])), 300_000)
        self.assertEqual(self.bal(keys.supplier_debt(self.s["supplier"])), 200_000)
        self.assertEqual(self.bal(keys.CASH), -100_000)
        self.assertEqual(queries.average_raw_cost_per_kg(self.j.bal, self.s["raw"]), 150_000)

    def test_raw_card_purchase_reduces_unmatched_bank_and_cash_road_cost(self):
        result = self.raw(payment_type="card", road_cost=3_000)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(self.bal(keys.BANK_UNMATCHED), -100_000)
        self.assertEqual(self.bal(keys.CASH), -3_000)

    def test_packaging_purchase_calculates_and_averages_unit_cost(self):
        first = self.packaging()
        second = self.packaging(quantity=10, unit_price=700, payment_type="credit")
        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "accepted")
        self.assertEqual(self.bal(keys.pack(self.s["pm"])), 20)
        self.assertEqual(self.bal(keys.pack_value(self.s["pm"])), 12_000)
        self.assertEqual(queries.average_packaging_cost_per_unit(self.j.bal, self.s["pm"]), 600)
        self.assertEqual(queries.supplier_balance(self.j.bal, self.s["supplier"]), 7_000)

    def test_supplier_payment_reduces_debt_and_is_recorded(self):
        self.packaging(payment_type="credit")
        result = self.run_cmd("supplier.pay", {
            "supplier_id": self.s["supplier"], "amount": 2_000, "payment_type": "card",
        })
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.response["outstanding"], 3_000)
        self.assertEqual(self.bal(keys.supplier_debt(self.s["supplier"])), 3_000)
        self.assertEqual(self.bal(keys.BANK_UNMATCHED), -2_000)
        self.assertEqual(len(queries.list_supplier_payments(self.j.docs, self.s["supplier"])), 1)
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))

    def test_supplier_payment_cannot_exceed_outstanding_debt(self):
        self.assertEqual(self.run_cmd("supplier.pay", {
            "supplier_id": self.s["supplier"], "amount": 1, "payment_type": "cash",
        }).code, "insufficient_supplier_debt")

    def test_purchase_rejects_wrong_supplier_or_inactive_references(self):
        another = self.ref("material", {"name": "Pistachios"})
        self.assertEqual(self.raw(material_id=another).code, "supplier_not_supplies")
        self.ok("reference.upsert", {"kind": "material", "id": self.s["raw"], "data": {"active": False}})
        self.assertEqual(self.raw().code, "inactive_reference")

    def test_purchase_validation(self):
        for values, code in [
            ({"weight_g": 0}, "invalid_field"),
            ({"weight_g": True}, "invalid_field"),
            ({"unit_price_per_kg": 0}, "invalid_field"),
            ({"weight_g": 1, "unit_price_per_kg": 1}, "invalid_field"),
            ({"paid_amount": -1}, "invalid_field"),
            ({"paid_amount": 100_001}, "invalid_field"),
            ({"payment_type": "credit"}, "invalid_field"),
            ({"unexpected": 1}, "unknown_field"),
        ]:
            self.assertEqual(self.raw(**values).code, code, values)
        self.assertEqual(self.raw(paid_amount=None).code, "missing_field")

    def test_packaging_purchase_validation_and_credit_default(self):
        self.assertEqual(self.packaging(payment_type="credit").response["supplier_debt_added"], 5_000)
        self.assertEqual(self.packaging(quantity=0).code, "invalid_field")
        self.assertEqual(self.packaging(payment_type="unknown").code, "invalid_field")
        self.assertEqual(self.packaging(payment_type="cash", paid_amount=0).code, "invalid_field")

    def test_permissions_are_enforced(self):
        self.assertEqual(self.raw(actor=self.agent).code, "forbidden")
        self.assertEqual(self.run_cmd("supplier.pay", {
            "supplier_id": self.s["supplier"], "amount": 1, "payment_type": "cash",
        }, actor=self.wh).code, "forbidden")
        self.assertEqual(self.run_cmd("supplier.pay", {
            "supplier_id": self.s["supplier"], "amount": 1, "payment_type": "cash",
        }, actor=self.agent).code, "forbidden")

    def test_purchase_history_and_rebuild_match_journal(self):
        result = self.raw(road_cost=1_000)
        self.assertEqual(queries.get_purchase(self.j.docs, result.response["purchase_id"])["purchase_type"], "raw")
        self.packaging(payment_type="credit")
        load_all()
        report = verify(self.env.db)
        self.assertTrue(report.ok, (report.balance_diffs, report.doc_diffs))
