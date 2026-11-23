"""Sklad qoldig'i, yuklar va inventarizatsiya yozuvlarini o'qish."""
from backend.balances import keys


def available_batches(balances, docs, product_id: str, as_of_date: str) -> list[dict]:
    """Yaroqli tayyor partiyalarni FIFO tartibida, real ombor qoldig'i bilan qaytaradi."""
    out = []
    for batch in docs.query("batch", status="posted"):
        line = next((item for item in batch.data["lines"] if item["product_id"] == product_id), None)
        if line is None or batch.data["expires_on"] < as_of_date:
            continue
        quantity = balances.get(keys.fg(product_id, batch.data["batch_id"]))
        if quantity > 0:
            out.append({
                "batch_id": batch.data["batch_id"],
                "available": quantity,
                "packaged_at": batch.data["packaged_at"],
                "expires_on": batch.data["expires_on"],
                "unit_cost": line["unit_cost"],
            })
    return sorted(out, key=lambda item: (item["packaged_at"], item["batch_id"]))


def custody_inventory(balances, user_id: str | None = None) -> list[dict]:
    prefix = keys.prefix("custody", user_id) if user_id else keys.prefix("custody")
    out = []
    for balance_key, quantity in balances.scan(prefix).items():
        _, owner_id, product_id, batch_id = balance_key.split(":")
        out.append({"user_id": owner_id, "product_id": product_id,
                    "batch_id": batch_id, "quantity": quantity})
    return out


def get_transfer(docs, transfer_id: str) -> dict | None:
    doc = docs.get(f"transfer:{transfer_id}")
    return {"id": transfer_id, "status": doc.status, **doc.data} if doc else None


def pending_loadings(docs, agent_id: str | None = None) -> list[dict]:
    transfers = docs.query("transfer", status="pending")
    return [dict(doc.data) for doc in transfers
            if doc.data["transfer_type"] == "loading"
            and (agent_id is None or doc.data["agent_id"] == agent_id)]


def get_stock_count(docs, count_id: str) -> dict | None:
    doc = docs.get(f"stock_count:{count_id}")
    return {"id": count_id, "status": doc.status, **doc.data} if doc else None


def list_stock_counts(docs, status: str | None = None) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("stock_count", status=status)]
