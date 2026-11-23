"""Kun yopish hujjatlari va agentning to'rt operatsion balansi."""
from backend.balances import keys


def balance_snapshot(balances, docs, agent_id: str, cash_counted: int) -> dict:
    custody = []
    for balance_key, quantity in balances.scan(keys.prefix("custody", agent_id)).items():
        _, user_id, product_id, batch_id = balance_key.split(":")
        custody.append({"user_id": user_id, "product_id": product_id,
                        "batch_id": batch_id, "quantity": quantity})

    assigned_stores = []
    for store in docs.query("store"):
        if store.data.get("agent_id") != agent_id:
            continue
        store_id = store.doc_id.split(":", 1)[1]
        assigned_stores.append({
            "store_id": store_id,
            "debt": balances.get(keys.debt(store_id)),
            "advance": balances.get(keys.advance(store_id)),
        })
    assigned_stores.sort(key=lambda item: item["store_id"])

    held_returns = []
    for balance_key, quantity in balances.scan(keys.prefix("hold", agent_id)).items():
        _, user_id, product_id, batch_id = balance_key.split(":")
        held_returns.append({"user_id": user_id, "product_id": product_id,
                             "batch_id": batch_id, "quantity": quantity})

    expected_cash = balances.get(keys.agent_cash(agent_id))
    return {
        "agent_cash": {
            "expected": expected_cash,
            "counted": cash_counted,
            "difference": cash_counted - expected_cash,
        },
        "custody_stock": {
            "quantity": sum(item["quantity"] for item in custody),
            "items": custody,
        },
        "store_balances": {
            "debt": sum(item["debt"] for item in assigned_stores),
            "advance": sum(item["advance"] for item in assigned_stores),
            "by_store": assigned_stores,
        },
        "held_returns": {
            "quantity": sum(item["quantity"] for item in held_returns),
            "items": held_returns,
        },
    }


def get_closing(docs, closing_id: str) -> dict | None:
    doc = docs.get(f"closing:{closing_id}")
    return {"id": closing_id, "status": doc.status, **doc.data} if doc else None


def list_closings(docs, agent_id: str | None = None) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("closing", ref=agent_id)]


def get_cash_handover(docs, transfer_id: str) -> dict | None:
    doc = docs.get(f"transfer:{transfer_id}")
    if doc is None or doc.data.get("transfer_type") != "cash_handover":
        return None
    return {"id": transfer_id, "status": doc.status, **doc.data}


def pending_cash_handovers(docs, agent_id: str | None = None) -> list[dict]:
    return [
        {"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
        for doc in docs.query("transfer", status="pending")
        if doc.data.get("transfer_type") == "cash_handover"
        and (agent_id is None or doc.data["agent_id"] == agent_id)
    ]
