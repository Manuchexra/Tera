"""Sotuv, qarz, qoldiq va yaroqlilik muddatlari bo'yicha hisobotlar."""
from backend.balances import keys
from datetime import date

from backend.core.timing import local_date


def sales_summary(docs, from_date: str, through_date: str,
                  store_ids: set[str] | None = None) -> dict:
    rows = []
    for doc in docs.query("sale"):
        data = doc.data
        sold_date = local_date(data["sold_at"]).isoformat()
        if not from_date <= sold_date <= through_date:
            continue
        if store_ids is not None and data["store_id"] not in store_ids:
            continue
        rows.append({
            "sale_id": data["sale_id"],
            "store_id": data["store_id"],
            "seller_id": data["seller_id"],
            "sold_at": data["sold_at"],
            "business_date": sold_date,
            "subtotal": data["subtotal"],
            "discount_amount": data["discount_amount"],
            "total_amount": data["total_amount"],
            "return_credit_total": data.get("return_credit_total", 0),
            "net_after_returns": data["total_amount"] - data.get("return_credit_total", 0),
            "paid_total": data["paid_total"],
            "outstanding": data["outstanding"],
            "status": doc.status,
            "lines": data["lines"],
        })
    rows.sort(key=lambda row: (row["sold_at"], row["sale_id"]))
    by_day: dict[str, dict] = {}
    for row in rows:
        day = by_day.setdefault(row["business_date"], {
            "business_date": row["business_date"], "sale_count": 0, "total_amount": 0,
            "return_credit_total": 0, "net_after_returns": 0, "outstanding": 0,
        })
        day["sale_count"] += 1
        day["total_amount"] += row["total_amount"]
        day["return_credit_total"] += row["return_credit_total"]
        day["net_after_returns"] += row["net_after_returns"]
        day["outstanding"] += row["outstanding"]
    totals = {
        "sale_count": len(rows),
        "subtotal": sum(row["subtotal"] for row in rows),
        "discount_amount": sum(row["discount_amount"] for row in rows),
        "total_amount": sum(row["total_amount"] for row in rows),
        "return_credit_total": sum(row["return_credit_total"] for row in rows),
        "net_after_returns": sum(row["net_after_returns"] for row in rows),
        "paid_total": sum(row["paid_total"] for row in rows),
        "outstanding": sum(row["outstanding"] for row in rows),
    }
    return {"from_date": from_date, "through_date": through_date, "totals": totals,
            "by_day": list(by_day.values()), "sales": rows}


def debt_report(balances, docs, as_of_date: str,
                store_ids: set[str] | None = None) -> dict:
    stores = []
    for store in docs.query("store"):
        store_id = store.doc_id.split(":", 1)[1]
        if store_ids is not None and store_id not in store_ids:
            continue
        debt = balances.get(keys.debt(store_id))
        advance = balances.get(keys.advance(store_id))
        if not debt and not advance:
            continue
        invoices = []
        for sale in docs.query("sale", ref=store_id, status="open"):
            if sale.data["outstanding"] <= 0:
                continue
            due_date = sale.data["due_date"]
            invoices.append({
                "sale_id": sale.data["sale_id"],
                "sold_at": sale.data["sold_at"],
                "due_date": due_date,
                "outstanding": sale.data["outstanding"],
                "overdue": due_date < as_of_date,
                "days_overdue": max(0, (date.fromisoformat(as_of_date) -
                                        date.fromisoformat(due_date)).days),
            })
        invoices.sort(key=lambda item: (item["due_date"], item["sold_at"], item["sale_id"]))
        stores.append({
            "store_id": store_id,
            "store_name": store.data["name"],
            "agent_id": store.data["agent_id"],
            "debt": debt,
            "advance": advance,
            "overdue_debt": sum(item["outstanding"] for item in invoices if item["overdue"]),
            "invoices": invoices,
        })
    stores.sort(key=lambda item: item["store_id"])
    return {
        "as_of_date": as_of_date,
        "debt_total": sum(store["debt"] for store in stores),
        "advance_total": sum(store["advance"] for store in stores),
        "overdue_total": sum(store["overdue_debt"] for store in stores),
        "stores": stores,
    }


def inventory_summary(balances, docs, agent_id: str | None = None) -> dict:
    raw = [{
        "material_id": key.split(":", 1)[1],
        "quantity_g": quantity,
        "value": balances.get(keys.raw_value(key.split(":", 1)[1])),
    } for key, quantity in balances.scan(keys.prefix("raw")).items()]
    packaging = [{
        "material_id": key.split(":", 1)[1],
        "quantity": quantity,
        "value": balances.get(keys.pack_value(key.split(":", 1)[1])),
    } for key, quantity in balances.scan(keys.prefix("pack")).items()]

    warehouse, custody, held, quarantine = [], [], [], []
    for balance_key, quantity in balances.scan(keys.prefix("fg")).items():
        _, product_id, batch_id = balance_key.split(":")
        warehouse.append(_batch_item(docs, product_id, batch_id, quantity))
    custody_prefix = keys.prefix("custody", agent_id) if agent_id else keys.prefix("custody")
    for balance_key, quantity in balances.scan(custody_prefix).items():
        _, user_id, product_id, batch_id = balance_key.split(":")
        custody.append({"user_id": user_id, **_batch_item(docs, product_id, batch_id, quantity)})
    held_prefix = keys.prefix("hold", agent_id) if agent_id else keys.prefix("hold")
    for balance_key, quantity in balances.scan(held_prefix).items():
        _, user_id, product_id, batch_id = balance_key.split(":")
        held.append({"user_id": user_id, **_batch_item(docs, product_id, batch_id, quantity)})
    for balance_key, quantity in balances.scan(keys.prefix("quarantine")).items():
        _, product_id, batch_id = balance_key.split(":")
        quarantine.append(_batch_item(docs, product_id, batch_id, quantity))
    return {
        "raw_materials": raw if agent_id is None else [],
        "packaging_materials": packaging if agent_id is None else [],
        "warehouse": warehouse if agent_id is None else [],
        "custody": custody,
        "held_returns": held,
        "quarantine": quarantine if agent_id is None else [],
    }


def _batch_item(docs, product_id: str, batch_id: str, quantity: int) -> dict:
    batch = docs.get(f"batch:{batch_id}")
    line = next((item for item in batch.data["lines"] if item["product_id"] == product_id), None) if batch else None
    return {
        "product_id": product_id,
        "batch_id": batch_id,
        "quantity": quantity,
        "expires_on": batch.data["expires_on"] if batch else None,
        "unit_cost": line["unit_cost"] if line else None,
    }


def expiring_inventory(balances, docs, from_date: str, through_date: str) -> list[dict]:
    rows = []
    custody_by_batch: dict[tuple[str, str], int] = {}
    held_by_batch: dict[tuple[str, str], int] = {}
    for balance_key, quantity in balances.scan(keys.prefix("custody")).items():
        _, _, product_id, batch_id = balance_key.split(":")
        key = (product_id, batch_id)
        custody_by_batch[key] = custody_by_batch.get(key, 0) + quantity
    for balance_key, quantity in balances.scan(keys.prefix("hold")).items():
        _, _, product_id, batch_id = balance_key.split(":")
        key = (product_id, batch_id)
        held_by_batch[key] = held_by_batch.get(key, 0) + quantity
    for batch in docs.query("batch", status="posted"):
        expires_on = batch.data["expires_on"]
        if not from_date <= expires_on <= through_date:
            continue
        for line in batch.data["lines"]:
            product_id = line["product_id"]
            batch_id = batch.data["batch_id"]
            quantity = (
                balances.get(keys.fg(product_id, batch_id))
                + balances.get(keys.quarantine(product_id, batch_id))
                + custody_by_batch.get((product_id, batch_id), 0)
                + held_by_batch.get((product_id, batch_id), 0)
            )
            if quantity:
                rows.append({
                    "batch_id": batch_id, "product_id": product_id, "quantity": quantity,
                    "expires_on": expires_on, "packaged_at": batch.data["packaged_at"],
                })
    return sorted(rows, key=lambda row: (row["expires_on"], row["packaged_at"], row["batch_id"],
                                         row["product_id"]))
