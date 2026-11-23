from bisect import bisect_right

from backend.balances.projection import projects


@projects("price_changed")
def _price_changed(ev, bal, docs):
    """Narx tarixi `at` (biznes vaqti) bo'yicha tartiblanadi; bir xil vaqtda keyingi hodisa keyin turadi."""
    p = ev.payload
    pid = p["product_id"]
    doc = docs.get(f"price:{pid}")
    history = doc.data["history"] if doc else []
    entry = {"at": ev.occurred_at, "price": p["price"], "by": ev.actor_id, "reason": p["reason"]}
    history.insert(bisect_right([h["at"] for h in history], ev.occurred_at), entry)
    current = history[-1]["price"]
    docs.put(f"price:{pid}", "price", pid, "active", {"product_id": pid, "price": current, "history": history})
