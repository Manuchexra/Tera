from backend.balances import keys
from backend.balances.projection import projects
from backend.modules.approvals import projections as approval_projections


@projects("sale_recorded")
def _sale_recorded(ev, bal, docs):
    p = ev.payload
    for line in p["lines"]:
        for allocation in line["allocations"]:
            bal.add(keys.custody(p["seller_id"], line["product_id"], allocation["batch_id"]),
                    -allocation["quantity"])
    if p["advance_used"]:
        bal.add(keys.advance(p["store_id"]), -p["advance_used"])
    if p["paid_amount"]:
        if p["payment_type"] == "card":
            bal.add(keys.BANK_UNMATCHED, p["paid_amount"])
        elif p["cash_recipient"] == "agent":
            bal.add(keys.agent_cash(p["seller_id"]), p["paid_amount"])
        else:
            bal.add(keys.CASH, p["paid_amount"])
    if p["credit_amount"]:
        bal.add(keys.debt(p["store_id"]), p["credit_amount"])
    sale_data = {
        **p,
        "sold_at": ev.occurred_at,
        "event_id": ev.event_id,
        "actor_id": ev.actor_id,
        "paid_total": p["advance_used"] + p["paid_amount"],
        "outstanding": p["credit_amount"],
    }
    docs.put(f"sale:{p['sale_id']}", "sale", p["store_id"],
             "open" if p["credit_amount"] else "paid", sale_data)
    for approval_id in p["approval_ids"]:
        approval_projections.mark_approval_used(docs, approval_id, ev.event_id, ev.occurred_at)


@projects("store_payment_received")
def _store_payment_received(ev, bal, docs):
    p = ev.payload
    allocated = 0
    for allocation in p["allocations"]:
        sale_id = allocation["sale_id"]
        amount = allocation["amount"]
        sale = docs.get(f"sale:{sale_id}")
        outstanding = sale.data["outstanding"] - amount
        paid_total = sale.data["paid_total"] + amount
        docs.put(sale.doc_id, "sale", sale.ref, "paid" if outstanding == 0 else "open",
                 {**sale.data, "outstanding": outstanding, "paid_total": paid_total,
                  "last_payment_at": ev.occurred_at})
        allocated += amount
    if allocated:
        bal.add(keys.debt(p["store_id"]), -allocated)
    if p["advance_added"]:
        bal.add(keys.advance(p["store_id"]), p["advance_added"])
    if p["payment_type"] == "card":
        bal.add(keys.BANK_UNMATCHED, p["amount"])
    elif p["cash_recipient"] == "agent":
        bal.add(keys.agent_cash(p["recipient_id"]), p["amount"])
    else:
        bal.add(keys.CASH, p["amount"])
    docs.put(f"payment:{p['payment_id']}", "payment", p["store_id"], "received",
             {**p, "received_at": ev.occurred_at, "event_id": ev.event_id, "actor_id": ev.actor_id})


@projects("sale_term_extended")
def _sale_term_extended(ev, bal, docs):
    _ = bal
    p = ev.payload
    sale = docs.get(f"sale:{p['sale_id']}")
    history = list(sale.data.get("term_extensions", []))
    history.append({**p, "extended_at": ev.occurred_at, "actor_id": ev.actor_id,
                    "event_id": ev.event_id})
    docs.put(sale.doc_id, "sale", sale.ref, sale.status,
             {**sale.data, "due_date": p["new_due_date"], "term_extensions": history})


@projects("dispute_opened")
def _dispute_opened(ev, bal, docs):
    _ = bal
    p = ev.payload
    docs.put(f"dispute:{p['dispute_id']}", "dispute", p["store_id"], "open",
             {**p, "opened_at": ev.occurred_at, "opened_by": ev.actor_id, "event_id": ev.event_id})


@projects("dispute_resolved")
def _dispute_resolved(ev, bal, docs):
    _ = bal
    p = ev.payload
    dispute = docs.get(f"dispute:{p['dispute_id']}")
    docs.put(dispute.doc_id, "dispute", dispute.ref, "resolved",
             {**dispute.data, "decision": p["decision"], "resolution": p["reason"],
              "resolved_at": ev.occurred_at, "resolved_by": ev.actor_id,
              "resolve_event_id": ev.event_id})
