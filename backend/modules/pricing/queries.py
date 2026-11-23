"""Narxni o'qish. Sotuv shu yerdan narxni oladi va uni hodisaga NUSXALAYDI (talablar §20.2.3): keyin narx
o'zgarsa ham eski sotuv narxi o'zgarmaydi."""


def current_price(docs, product_id: str) -> int | None:
    d = docs.get(f"price:{product_id}")
    return d.data["price"] if d else None


def price_at(docs, product_id: str, ts: str) -> int | None:
    """`ts` (UTC ISO satr) paytida amalda bo'lgan narx. Birinchi narxdan oldingi vaqt uchun None."""
    d = docs.get(f"price:{product_id}")
    if d is None:
        return None
    found = None
    for h in d.data["history"]:
        if h["at"] <= ts:
            found = h["price"]
        else:
            break
    return found


def history(docs, product_id: str) -> list[dict]:
    d = docs.get(f"price:{product_id}")
    return list(d.data["history"]) if d else []


def markup_pct(price: int, unit_cost: int) -> str:
    """Ustama foizi tannarxdan (talablar §15): (narx - tannarx) / tannarx. Bir xona aniqlikda satr, masalan '69.6'."""
    from backend.core.money import pct
    return str(pct(price - unit_cost, unit_cost, 1))
