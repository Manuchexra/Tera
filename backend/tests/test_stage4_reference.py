from backend.balances import keys
from backend.balances.rebuild import verify
from backend.modules import load_all
from backend.modules.reference import queries
from backend.modules.reference.rules import product_stock

from .helpers import AppCase


class CreateTests(AppCase):
    def test_create_all_kinds_with_server_ids(self):
        s = self.seed()
        self.assertEqual((s["raw"], s["pm"], s["product"], s["store"], s["supplier"]),
                         ("RM-001", "PM-001", "P-001", "S-001", "SUP-001"))
        self.assertEqual(self.ref("material", {"name": "Pistachios"}), "RM-002")

    def test_defaults_and_auto_name_and_normalization(self):
        s = self.seed()
        prod = queries.get(self.j.docs, "product", s["product"])
        self.assertEqual((prod["name"], prod["status"], prod["materials"]),
                         ("Almonds 50 g", "active", [{"material_id": "PM-001", "qty": 1}]))
        store = queries.get(self.j.docs, "store", s["store"])
        self.assertEqual((store["phone"], store["lang"], store["active"]), ("+998901234567", "uz", True))
        mat = queries.get(self.j.docs, "material", s["raw"])
        self.assertEqual(mat["min_stock_g"], 0)

    def test_doc_refs_support_lookup_by_agent_and_material(self):
        s = self.seed()
        self.assertEqual([d.doc_id for d in self.j.docs.query("store", ref=self.agent_user.user_id)], ["store:S-001"])
        self.assertEqual([d.doc_id for d in self.j.docs.query("product", ref=s["raw"])], ["product:P-001"])

    def test_event_records_creation(self):
        self.seed()
        ev = self.j.ledger.get("c1")
        self.assertEqual((ev.type, ev.payload["created"], ev.payload["changes"]), ("reference_changed", True, {}))
        self.assertEqual(ev.actor_id, self.owner.user_id)


class PermissionAndFreshnessTests(AppCase):
    def test_only_owner(self):
        for actor in (self.agent, self.wh):
            self.fail("reference.upsert", {"kind": "material", "data": {"name": "X"}}, "forbidden", actor=actor)

    def test_offline_queued_or_suspicious_time_rejected(self):
        self.fail("reference.upsert", {"kind": "material", "data": {"name": "X"}}, "online_required",
                  device_time="2026-11-18T05:00:00.000Z")                           # soat 1 soat orqada
        self.fail("reference.upsert", {"kind": "material", "data": {"name": "X"}}, "online_required",
                  device_time="2026-11-18T03:00:00.000Z", est_server_time="2026-11-18T03:00:30.000Z")   # soat to'g'ri, lekin 3 soat kechikib keldi
        self.assertEqual(self.j.docs.query("material"), [])


class StrictnessTests(AppCase):
    def test_unknown_field_and_kind(self):
        self.fail("reference.upsert", {"kind": "material", "data": {"name": "X", "color": "red"}}, "unknown_field")
        self.fail("reference.upsert", {"kind": "warehouse", "data": {"name": "X"}}, "invalid_field")
        self.fail("reference.upsert", {"kind": "store", "data": {"name": "X", "credit_limit": 5}}, "unknown_field")  # limit hamma uchun bir xil

    def test_bad_types(self):
        s = self.seed()
        cases = [
            ("material", {"name": ""}, "invalid_field"),
            ("material", {"name": 5}, "invalid_field"),
            ("material", {"name": "Y", "shelf_life_days": 0}, "invalid_field"),
            ("material", {"name": "Y", "shelf_life_days": 1.5}, "invalid_field"),
            ("material", {"name": "Y", "active": "yes"}, "invalid_field"),
            ("material", {}, "invalid_field"),
            ("material", {"shelf_life_days": 30}, "missing_field"),
            ("store", {"name": "Z", "agent_id": self.agent_user.user_id, "phone": "12"}, "invalid_field"),
            ("store", {"name": "Z", "agent_id": self.agent_user.user_id, "lang": "de"}, "invalid_field"),
            ("store", {"name": "Z", "agent_id": self.agent_user.user_id, "lat_e6": 91_000_000, "lon_e6": 0}, "invalid_field"),
            ("product", {"raw_material_id": s["raw"], "pack_g": 0}, "invalid_field"),
            ("product", {"raw_material_id": s["raw"], "pack_g": 25, "materials": [{"material_id": s["pm"]}]}, "invalid_field"),
            ("product", {"raw_material_id": s["raw"], "pack_g": 25,
                         "materials": [{"material_id": s["pm"], "qty": 1}, {"material_id": s["pm"], "qty": 2}]}, "invalid_field"),
            ("supplier", {"name": "Q", "supplies": [s["raw"], s["raw"]]}, "invalid_field"),
        ]
        for kind, data, code in cases:
            self.fail("reference.upsert", {"kind": kind, "data": data}, code)

    def test_ids_are_server_assigned(self):
        self.fail("reference.upsert", {"kind": "material", "id": "RM-777", "data": {"name": "X"}}, "not_found")


class UpdateTests(AppCase):
    def test_update_merges_and_records_before_after(self):
        s = self.seed()
        self.ok("reference.upsert", {"kind": "store", "id": s["store"], "reason": "yangi raqam",
                                     "data": {"phone": "+998 71 200 00 00", "address": "Chilonzor 9"}})
        store = queries.get(self.j.docs, "store", s["store"])
        self.assertEqual((store["phone"], store["address"], store["name"]), ("+998712000000", "Chilonzor 9", "Baraka Market"))
        ev = [e for e in self.j.ledger.iter_events() if e.type == "reference_changed"][-1]
        self.assertEqual(ev.payload["changes"]["phone"], ["+998901234567", "+998712000000"])
        self.assertEqual((ev.payload["created"], ev.payload["reason"]), (False, "yangi raqam"))

    def test_no_changes_rejected(self):
        s = self.seed()
        self.fail("reference.upsert", {"kind": "store", "id": s["store"], "data": {"name": "Baraka Market"}}, "no_changes")

    def test_unknown_id_not_found(self):
        self.fail("reference.upsert", {"kind": "store", "id": "S-999", "data": {"name": "X"}}, "not_found")

    def test_optional_field_can_be_cleared(self):
        s = self.seed()
        self.ok("reference.upsert", {"kind": "store", "id": s["store"], "data": {"phone": None}})
        self.assertIsNone(queries.get(self.j.docs, "store", s["store"])["phone"])
        self.fail("reference.upsert", {"kind": "store", "id": s["store"], "data": {"name": None}}, "missing_field")

    def test_product_core_fields_immutable(self):
        s = self.seed()
        raw2 = self.ref("material", {"name": "Pistachios"})
        self.fail("reference.upsert", {"kind": "product", "id": s["product"], "data": {"pack_g": 100}}, "immutable_field")
        self.fail("reference.upsert", {"kind": "product", "id": s["product"], "data": {"raw_material_id": raw2}}, "immutable_field")
        self.ok("reference.upsert", {"kind": "product", "id": s["product"], "data": {"pack_g": 50, "name": "Bodom 50 g"}})
        self.assertEqual(queries.get(self.j.docs, "product", s["product"])["name"], "Bodom 50 g")


class UniquenessAndLinksTests(AppCase):
    def test_duplicate_names(self):
        self.seed()
        self.fail("reference.upsert", {"kind": "material", "data": {"name": "  almonds "}}, "duplicate_name")
        self.fail("reference.upsert", {"kind": "packmaterial", "data": {"name": "POUCH 50 g"}}, "duplicate_name")
        self.fail("reference.upsert", {"kind": "supplier", "data": {"name": "samarkand agro"}}, "duplicate_name")

    def test_product_unique_per_material_and_size(self):
        s = self.seed()
        r = self.fail("reference.upsert", {"kind": "product", "data": {"raw_material_id": s["raw"], "pack_g": 50}},
                      "product_exists")
        self.assertEqual(r.details["existing_id"], "P-001")
        self.ok("reference.upsert", {"kind": "product", "data": {"raw_material_id": s["raw"], "pack_g": 100}})
        # eskisi o'chirilsa, xuddi shu o'lcham qayta yaratilishi mumkin
        self.ok("reference.upsert", {"kind": "product", "id": s["product"], "data": {"active": False}})
        self.ok("reference.upsert", {"kind": "product", "data": {"raw_material_id": s["raw"], "pack_g": 50}})

    def test_reactivating_duplicate_blocked(self):
        s = self.seed()
        self.ok("reference.upsert", {"kind": "product", "id": s["product"], "data": {"active": False}})
        self.ok("reference.upsert", {"kind": "product", "data": {"raw_material_id": s["raw"], "pack_g": 50}})
        self.fail("reference.upsert", {"kind": "product", "id": s["product"], "data": {"active": True}}, "product_exists")

    def test_product_links_must_exist_and_be_active(self):
        s = self.seed()
        self.fail("reference.upsert", {"kind": "product", "data": {"raw_material_id": "RM-999", "pack_g": 25}}, "not_found")
        self.fail("reference.upsert", {"kind": "product", "data": {"raw_material_id": s["raw"], "pack_g": 25,
                  "materials": [{"material_id": "PM-999", "qty": 1}]}}, "not_found")
        self.ok("reference.upsert", {"kind": "packmaterial", "id": s["pm"], "data": {"active": False}})
        self.fail("reference.upsert", {"kind": "product", "data": {"raw_material_id": s["raw"], "pack_g": 25,
                  "materials": [{"material_id": s["pm"], "qty": 1}]}}, "inactive_reference")

    def test_product_may_be_created_without_materials_and_linked_later(self):
        s = self.seed()
        pid = self.ref("product", {"raw_material_id": s["raw"], "pack_g": 25})
        self.assertEqual(queries.get(self.j.docs, "product", pid)["materials"], [])
        self.ok("reference.upsert", {"kind": "product", "id": pid,
                                     "data": {"materials": [{"material_id": s["pm"], "qty": 1}]}})
        self.assertEqual(len(queries.get(self.j.docs, "product", pid)["materials"]), 1)

    def test_supplier_supplies_must_be_active_materials(self):
        self.fail("reference.upsert", {"kind": "supplier", "data": {"name": "X", "supplies": ["RM-404"]}}, "not_found")


class StoreRulesTests(AppCase):
    def store(self, **data):
        return {"kind": "store", "data": {"name": "Fayz savdo", "agent_id": self.agent_user.user_id, **data}}

    def test_agent_must_be_active_agent_or_owner(self):
        self.ok("reference.upsert", self.store())
        self.fail("reference.upsert", self.store(agent_id=self.wh_user.user_id), "invalid_field")      # omborchi agent emas
        self.fail("reference.upsert", self.store(agent_id="U999"), "invalid_field")
        self.ok("reference.upsert", self.store(name="Navruz", agent_id=self.owner.user_id))          # egasi o'zi sotadi
        self.env.auth.set_active(self.owner, self.agent_user.user_id, False)
        self.fail("reference.upsert", self.store(name="Istiqlol"), "invalid_field")

    def test_coordinates_both_or_none(self):
        self.fail("reference.upsert", self.store(lat_e6=41_311_100), "invalid_field")
        self.ok("reference.upsert", self.store(lat_e6=41_311_100, lon_e6=69_279_700))

    def test_telegram_chat_unique(self):
        self.ok("reference.upsert", self.store(telegram_chat_id="555"))
        r = self.fail("reference.upsert", self.store(name="Boshqa", telegram_chat_id="555"), "duplicate_telegram_chat")
        self.assertEqual(r.details["store"], "S-001")


class DeactivationGuardTests(AppCase):
    def deactivate(self, kind, rid):
        return self.run_cmd("reference.upsert", {"kind": kind, "id": rid, "data": {"active": False}})

    def test_material_with_stock(self):
        s = self.seed()
        self.add_balance(keys.raw(s["raw"]), 5_000)
        self.assertEqual(self.deactivate("material", s["raw"]).code, "in_use")
        self.add_balance(keys.raw(s["raw"]), -5_000)
        self.assertEqual(self.deactivate("material", s["raw"]).status, "accepted")

    def test_packmaterial_with_stock(self):
        s = self.seed()
        self.add_balance(keys.pack(s["pm"]), 100)
        self.assertEqual(self.deactivate("packmaterial", s["pm"]).code, "in_use")

    def test_product_stock_anywhere(self):
        s = self.seed()
        for key in (keys.fg("P-001", "B-1"), keys.custody("U002", "P-001", "B-1"),
                    keys.hold("U002", "P-001", "B-1"), keys.quarantine("P-001", "B-1")):
            self.add_balance(key, 3)
            r = self.deactivate("product", s["product"])
            self.assertEqual((r.code, r.details["stock"]), ("in_use", 3), key)
            self.add_balance(key, -3)
        self.assertEqual(self.deactivate("product", s["product"]).status, "accepted")

    def test_product_stock_counts_only_that_product(self):
        self.add_balance(keys.fg("P-002", "B-1"), 9)
        self.add_balance(keys.custody("U002", "P-002", "B-1"), 4)
        self.assertEqual(product_stock(self.j.bal, "P-001"), 0)
        self.assertEqual(product_stock(self.j.bal, "P-002"), 13)

    def test_store_with_debt_or_advance(self):
        s = self.seed()
        self.add_balance(keys.debt(s["store"]), 100_000)
        self.assertEqual(self.deactivate("store", s["store"]).code, "in_use")
        self.add_balance(keys.debt(s["store"]), -100_000)
        self.add_balance(keys.advance(s["store"]), 5_000)
        self.assertEqual(self.deactivate("store", s["store"]).code, "in_use")

    def test_supplier_with_debt_and_reactivation(self):
        s = self.seed()
        self.add_balance(keys.supplier_debt(s["supplier"]), 7_000_000)
        self.assertEqual(self.deactivate("supplier", s["supplier"]).code, "in_use")
        self.add_balance(keys.supplier_debt(s["supplier"]), -7_000_000)
        self.assertEqual(self.deactivate("supplier", s["supplier"]).status, "accepted")
        self.assertEqual(self.doc("supplier:SUP-001").status, "inactive")
        self.ok("reference.upsert", {"kind": "supplier", "id": s["supplier"], "data": {"active": True}})
        self.assertEqual(self.doc("supplier:SUP-001").status, "active")


class SnapshotAndRebuildTests(AppCase):
    def test_snapshot_contents_and_agent_filter(self):
        s = self.seed()
        self.ok("price.set", {"product_id": s["product"], "price": 8_000, "reason": "boshlang'ich narx"})
        other_agent, _ = self.env.add_user("nodir", ["agent"])
        self.ref("store", {"name": "Istiqlol", "agent_id": other_agent.user_id})
        snap = queries.snapshot(self.j, self.e.current_policy())
        self.assertEqual(snap["version"], self.j.ledger.last_seq())
        self.assertEqual(snap["policy"]["credit_limit"], 2_000_000)
        self.assertEqual([p["price"] for p in snap["products"]], [8_000])
        self.assertEqual(len(snap["stores"]), 2)
        mine = queries.snapshot(self.j, self.e.current_policy(), agent_id=self.agent_user.user_id)
        self.assertEqual([x["name"] for x in mine["stores"]], ["Baraka Market"])

    def test_inactive_items_excluded_from_snapshot(self):
        s = self.seed()
        self.ok("reference.upsert", {"kind": "supplier", "id": s["supplier"], "data": {"active": False}})
        self.assertEqual(queries.snapshot(self.j, self.e.current_policy())["suppliers"], [])
        self.assertEqual(len(queries.list_all(self.j.docs, "supplier", active_only=False)), 1)

    def test_cache_equals_rebuild(self):
        s = self.seed()
        self.ok("reference.upsert", {"kind": "store", "id": s["store"], "data": {"address": "X"}})
        self.fail("reference.upsert", {"kind": "material", "data": {"name": "almonds"}})
        load_all()
        rep = verify(self.env.db)
        self.assertTrue(rep.ok, (rep.balance_diffs, rep.doc_diffs))
