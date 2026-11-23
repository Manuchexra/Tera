from backend.balances import keys
from backend.balances.projection import projects


@projects("closing_submitted")
def _closing_submitted(ev, bal, docs):
    _ = bal
    p = ev.payload
    docs.put(f"closing:{p['closing_id']}", "closing", p["agent_id"], "submitted",
             {**p, "submitted_at": ev.occurred_at, "event_id": ev.event_id})


@projects("cash_handover_prepared")
def _cash_handover_prepared(ev, bal, docs):
    _ = bal
    p = ev.payload
    docs.put(f"transfer:{p['transfer_id']}", "transfer", p["agent_id"], "pending",
             {**p, "event_id": ev.event_id})


@projects("cash_handover_accepted")
def _cash_handover_accepted(ev, bal, docs):
    p = ev.payload
    transfer = docs.get(f"transfer:{p['transfer_id']}")
    bal.add(keys.agent_cash(p["agent_id"]), -p["amount"])
    bal.add(keys.CASH, p["amount"])
    docs.put(transfer.doc_id, "transfer", transfer.ref, "accepted",
             {**transfer.data, "accepted_by": ev.actor_id, "accepted_device_id": ev.device_id,
              "accepted_at": ev.occurred_at, "accept_event_id": ev.event_id})
    closing = docs.get(f"closing:{p['closing_id']}")
    docs.put(closing.doc_id, "closing", closing.ref, closing.status,
             {**closing.data, "cash_handover_status": "accepted",
              "cash_handover_accepted_at": ev.occurred_at,
              "cash_handover_accepted_by": ev.actor_id})
