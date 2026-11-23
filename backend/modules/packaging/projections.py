from backend.balances import keys
from backend.balances.projection import projects
from backend.modules.approvals import projections as approval_projections


@projects("packaging_recorded")
def _packaging_recorded(ev, bal, docs):
    p = ev.payload
    bal.add(keys.raw(p["material_id"]), -p["input_g"])
    bal.add(keys.raw_value(p["material_id"]), -p["raw_cost_total"])
    for allocation in p["raw_lot_allocations"]:
        lot_id = f"raw_lot:{allocation['purchase_id']}"
        lot = docs.get(lot_id)
        remaining = lot.data["remaining_g"] - allocation["weight_g"]
        docs.put(lot_id, "raw_lot", p["material_id"], "open" if remaining else "depleted",
                 {**lot.data, "remaining_g": remaining})
    for item in p["material_consumption"]:
        bal.add(keys.pack(item["material_id"]), -item["quantity"])
        bal.add(keys.pack_value(item["material_id"]), -item["inventory_value"])
    for line in p["lines"]:
        bal.add(keys.fg(line["product_id"], p["batch_id"]), line["quantity"])
    docs.put(f"batch:{p['batch_id']}", "batch", p["material_id"], "posted",
             {**p, "packaged_at": ev.occurred_at, "event_id": ev.event_id, "actor_id": ev.actor_id})
    if p["approval_id"]:
        approval_projections.mark_approval_used(docs, p["approval_id"], ev.event_id, ev.occurred_at)
