from backend.balances import keys
from backend.balances.projection import projects


@projects("return_declared")
def _return_declared(ev, bal, docs):
    p = ev.payload
    for line in p["lines"]:
        for allocation in line["allocations"]:
            bal.add(keys.hold(p["agent_id"], line["product_id"], allocation["batch_id"]),
                    allocation["quantity"])
    docs.put(f"return:{p['return_id']}", "return", p["sale_id"], "pending",
             {**p, "declared_at": ev.occurred_at, "event_id": ev.event_id})


@projects("return_accepted")
def _return_accepted(ev, bal, docs):
    p = ev.payload
    returned = docs.get(f"return:{p['return_id']}")
    for line in p["lines"]:
        for allocation in line["allocations"]:
            product_id, batch_id = line["product_id"], allocation["batch_id"]
            quantity = allocation["quantity"]
            bal.add(keys.hold(returned.data["agent_id"], product_id, batch_id), -quantity)
            if p["disposition"] == "restock":
                bal.add(keys.fg(product_id, batch_id), quantity)
            else:
                bal.add(keys.quarantine(product_id, batch_id), quantity)
    docs.put(returned.doc_id, "return", returned.ref, "accepted",
             {**returned.data, "disposition": p["disposition"], "accepted_by": ev.actor_id,
              "accepted_at": ev.occurred_at, "accept_event_id": ev.event_id})


@projects("return_settled")
def _return_settled(ev, bal, docs):
    p = ev.payload
    returned = docs.get(f"return:{p['return_id']}")
    sale = docs.get(f"sale:{p['sale_id']}")
    outstanding = sale.data["outstanding"] - p["debt_reduction"]
    if p["debt_reduction"]:
        bal.add(keys.debt(p["store_id"]), -p["debt_reduction"])
    if p["advance_added"]:
        bal.add(keys.advance(p["store_id"]), p["advance_added"])
    docs.put(sale.doc_id, "sale", sale.ref, "paid" if outstanding == 0 else "open",
             {**sale.data, "outstanding": outstanding,
              "return_credit_total": sale.data.get("return_credit_total", 0) + p["return_amount"]})
    docs.put(returned.doc_id, "return", returned.ref, "settled",
             {**returned.data, "debt_reduction": p["debt_reduction"],
              "advance_added": p["advance_added"], "settled_at": ev.occurred_at,
              "settled_by": ev.actor_id, "settle_event_id": ev.event_id})


@projects("return_exchanged")
def _return_exchanged(ev, bal, docs):
    p = ev.payload
    returned = docs.get(f"return:{p['return_id']}")
    sale = docs.get(f"sale:{p['original_sale_id']}")
    outstanding = sale.data["outstanding"] - p["debt_reduction"]
    if p["debt_reduction"]:
        bal.add(keys.debt(p["store_id"]), -p["debt_reduction"])
    if p["return_credit_to_advance"]:
        bal.add(keys.advance(p["store_id"]), p["return_credit_to_advance"])
    docs.put(sale.doc_id, "sale", sale.ref, "paid" if outstanding == 0 else "open",
             {**sale.data, "outstanding": outstanding,
              "return_credit_total": sale.data.get("return_credit_total", 0) + p["return_amount"]})
    docs.put(returned.doc_id, "return", returned.ref, "exchanged",
             {**returned.data, "exchange_id": p["exchange_id"],
              "debt_reduction": p["debt_reduction"], "advance_used": p["advance_used"],
              "exchanged_at": ev.occurred_at, "exchanged_by": ev.actor_id,
              "exchange_event_id": ev.event_id})
    docs.put(f"exchange:{p['exchange_id']}", "exchange", p["store_id"], "approved",
             {**p, "approved_at": ev.occurred_at, "approved_by": ev.actor_id,
              "event_id": ev.event_id})


@projects("writeoff_approved")
def _writeoff_approved(ev, bal, docs):
    p = ev.payload
    bal.add(keys.quarantine(p["product_id"], p["batch_id"]), -p["quantity"])
    docs.put(f"writeoff:{p['writeoff_id']}", "writeoff", p["product_id"], "approved",
             {**p, "approved_at": ev.occurred_at, "approved_by": ev.actor_id,
              "event_id": ev.event_id})


@projects("custody_return_prepared")
def _custody_return_prepared(ev, bal, docs):
    _ = bal
    p = ev.payload
    docs.put(f"transfer:{p['transfer_id']}", "transfer", p["agent_id"], "pending",
             {**p, "event_id": ev.event_id})


@projects("custody_return_accepted")
def _custody_return_accepted(ev, bal, docs):
    p = ev.payload
    transfer = docs.get(f"transfer:{p['transfer_id']}")
    for line in p["lines"]:
        for allocation in line["allocations"]:
            product_id, batch_id = line["product_id"], allocation["batch_id"]
            quantity = allocation["quantity"]
            bal.add(keys.custody(p["agent_id"], product_id, batch_id), -quantity)
            if p["disposition"] == "restock":
                bal.add(keys.fg(product_id, batch_id), quantity)
            else:
                bal.add(keys.quarantine(product_id, batch_id), quantity)
    docs.put(transfer.doc_id, "transfer", transfer.ref, "accepted",
             {**transfer.data, "disposition": p["disposition"], "accepted_by": ev.actor_id,
              "accepted_at": ev.occurred_at, "accept_event_id": ev.event_id})
