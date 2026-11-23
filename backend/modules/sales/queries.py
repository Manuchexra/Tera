"""Sotuv, qarz, to'lov va bahslarni o'qish."""
from backend.balances import keys


def current_price(docs, product_id: str) -> int | None:
    doc = docs.get(f"price:{product_id}")
    return doc.data["price"] if doc else None


def available_custody_batches(balances, docs, user_id: str, product_id: str,
                              as_of_date: str) -> list[dict]:
    """Agent zimmasidagi yaroqli partiyalarni FIFO tartibida qaytaradi."""
    out = []
    for batch in docs.query("batch", status="posted"):
        line = next((item for item in batch.data["lines"] if item["product_id"] == product_id), None)
        if line is None or batch.data["expires_on"] < as_of_date:
            continue
        quantity = balances.get(keys.custody(user_id, product_id, batch.data["batch_id"]))
        if quantity > 0:
            out.append({
                "batch_id": batch.data["batch_id"],
                "available": quantity,
                "packaged_at": batch.data["packaged_at"],
                "expires_on": batch.data["expires_on"],
            })
    return sorted(out, key=lambda item: (item["packaged_at"], item["batch_id"]))


def get_sale(docs, sale_id: str) -> dict | None:
    doc = docs.get(f"sale:{sale_id}")
    return {"id": sale_id, "status": doc.status, **doc.data} if doc else None


def list_sales(docs, store_id: str | None = None, status: str | None = None) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("sale", ref=store_id, status=status)]


def oldest_open_sales(docs, store_id: str) -> list[dict]:
    sales = [sale for sale in list_sales(docs, store_id, "open") if sale["outstanding"] > 0]
    return sorted(sales, key=lambda sale: (sale["sold_at"], sale["sale_id"]))


def store_balance(balances, store_id: str) -> dict:
    return {"store_id": store_id, "debt": balances.get(keys.debt(store_id)),
            "advance": balances.get(keys.advance(store_id))}


def get_payment(docs, payment_id: str) -> dict | None:
    doc = docs.get(f"payment:{payment_id}")
    return {"id": payment_id, "status": doc.status, **doc.data} if doc else None


def list_payments(docs, store_id: str) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("payment", ref=store_id)]


def get_dispute(docs, dispute_id: str) -> dict | None:
    doc = docs.get(f"dispute:{dispute_id}")
    return {"id": dispute_id, "status": doc.status, **doc.data} if doc else None


def list_disputes(docs, store_id: str | None = None, status: str | None = None) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("dispute", ref=store_id, status=status)]
