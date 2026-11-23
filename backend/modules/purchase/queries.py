"""Xaridlar, o'rtacha tannarx va yetkazib beruvchi qarzini o'qish."""
from decimal import Decimal

from backend.balances import keys
from backend.core.money import round_uzs


def average_raw_cost_per_kg(balances, material_id: str) -> int | None:
    """Xom ashyo qoldig'ining o'rtacha qiymati, so'm/kg. Qoldiq bo'lmasa None."""
    weight_g = balances.get(keys.raw(material_id))
    if weight_g <= 0:
        return None
    value = balances.get(keys.raw_value(material_id))
    return round_uzs(Decimal(value) * 1000 / weight_g)


def average_packaging_cost_per_unit(balances, material_id: str) -> int | None:
    """Qadoq materiali qoldig'ining o'rtacha qiymati, so'm/dona. Qoldiq bo'lmasa None."""
    quantity = balances.get(keys.pack(material_id))
    if quantity <= 0:
        return None
    return round_uzs(Decimal(balances.get(keys.pack_value(material_id))) / quantity)


def supplier_balance(balances, supplier_id: str) -> int:
    return balances.get(keys.supplier_debt(supplier_id))


def get_purchase(docs, purchase_id: str) -> dict | None:
    doc = docs.get(f"purchase:{purchase_id}")
    return dict(doc.data) if doc else None


def list_purchases(docs, supplier_id: str | None = None, purchase_type: str | None = None) -> list[dict]:
    purchases = docs.query("purchase", ref=supplier_id)
    return [dict(doc.data) for doc in purchases
            if purchase_type is None or doc.data["purchase_type"] == purchase_type]


def list_supplier_payments(docs, supplier_id: str) -> list[dict]:
    return [dict(doc.data) for doc in docs.query("supplier_payment", ref=supplier_id)]
