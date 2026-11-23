from backend.balances import keys
from backend.balances.projection import projects


def _record_purchase_payment(bal, payment_type: str, amount: int) -> None:
    if payment_type == "cash":
        bal.add(keys.CASH, -amount)
    elif payment_type == "card":
        bal.add(keys.BANK_UNMATCHED, -amount)


@projects("raw_purchase_recorded")
def _raw_purchase_recorded(ev, bal, docs):
    p = ev.payload
    bal.add(keys.raw(p["material_id"]), p["weight_g"])
    bal.add(keys.raw_value(p["material_id"]), p["inventory_value_added"])
    bal.add(keys.supplier_debt(p["supplier_id"]), p["supplier_debt_added"])
    _record_purchase_payment(bal, p["payment_type"], p["paid_amount"])
    if p["road_cost"]:
        bal.add(keys.CASH, -p["road_cost"])
    docs.put(f"purchase:{p['purchase_id']}", "purchase", p["supplier_id"], "posted",
             {**p, "purchased_at": ev.occurred_at, "event_id": ev.event_id, "actor_id": ev.actor_id})
    docs.put(f"raw_lot:{p['purchase_id']}", "raw_lot", p["material_id"], "open", {
        "purchase_id": p["purchase_id"], "supplier_id": p["supplier_id"],
        "purchased_at": ev.occurred_at, "quantity_g": p["weight_g"], "remaining_g": p["weight_g"],
    })


@projects("packaging_purchase_recorded")
def _packaging_purchase_recorded(ev, bal, docs):
    p = ev.payload
    bal.add(keys.pack(p["material_id"]), p["quantity"])
    bal.add(keys.pack_value(p["material_id"]), p["inventory_value_added"])
    bal.add(keys.supplier_debt(p["supplier_id"]), p["supplier_debt_added"])
    _record_purchase_payment(bal, p["payment_type"], p["paid_amount"])
    docs.put(f"purchase:{p['purchase_id']}", "purchase", p["supplier_id"], "posted",
             {**p, "purchased_at": ev.occurred_at, "event_id": ev.event_id, "actor_id": ev.actor_id})


@projects("supplier_payment_recorded")
def _supplier_payment_recorded(ev, bal, docs):
    p = ev.payload
    bal.add(keys.supplier_debt(p["supplier_id"]), -p["amount"])
    _record_purchase_payment(bal, p["payment_type"], p["amount"])
    docs.put(f"supplier_payment:{p['payment_id']}", "supplier_payment", p["supplier_id"], "paid",
             {**p, "paid_at": ev.occurred_at, "event_id": ev.event_id, "actor_id": ev.actor_id})
