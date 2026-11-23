from backend.balances import keys
from backend.balances.projection import projects


@projects("loading_prepared")
def _loading_prepared(ev, _bal, docs):
    p = ev.payload
    docs.put(f"transfer:{p['transfer_id']}", "transfer", p["agent_id"], "pending",
             {**p, "event_id": ev.event_id})


@projects("loading_accepted")
def _loading_accepted(ev, bal, docs):
    p = ev.payload
    transfer = docs.get(f"transfer:{p['transfer_id']}")
    for line in p["lines"]:
        for allocation in line["allocations"]:
            quantity = allocation["quantity"]
            product_id, batch_id = line["product_id"], allocation["batch_id"]
            bal.add(keys.fg(product_id, batch_id), -quantity)
            bal.add(keys.custody(p["agent_id"], product_id, batch_id), quantity)
    docs.put(transfer.doc_id, "transfer", transfer.ref, "accepted",
             {**transfer.data, "accepted_by": ev.actor_id, "accepted_device_id": ev.device_id,
              "accepted_at": ev.occurred_at, "accept_event_id": ev.event_id})


def _count_key(p: dict) -> str:
    if p["location"] == "warehouse":
        return keys.fg(p["product_id"], p["batch_id"])
    return keys.custody(p["user_id"], p["product_id"], p["batch_id"])


@projects("stock_count_submitted")
def _stock_count_submitted(ev, _bal, docs):
    p = ev.payload
    docs.put(f"stock_count:{p['count_id']}", "stock_count", p["product_id"], "pending",
             {**p, "submit_event_id": ev.event_id})


@projects("stock_count_approved")
def _stock_count_approved(ev, bal, docs):
    p = ev.payload
    count_id = p["count_id"]
    count = docs.get(f"stock_count:{count_id}")
    if p["difference"]:
        bal.add(_count_key(count.data), p["difference"])
    docs.put(count.doc_id, "stock_count", count.ref, "approved",
             {**count.data, "approved_by": ev.actor_id, "approved_at": ev.occurred_at,
              "approve_event_id": ev.event_id})
