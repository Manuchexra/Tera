"""price.set: yagona narx (barcha do'konlar uchun bir xil). Faqat egasi, onlayn, sabab bilan."""
from backend.core.errors import DomainError
from backend.core.validate import as_int, as_str
from backend.modules.sync.registry import command

from . import projections  # noqa: F401


@command("price.set")
def set_price(ctx, p):
    ctx.require_fresh()
    pid = as_str(p, "product_id", max_len=60)
    price = as_int(p, "price", minimum=1, maximum=100_000_000)
    reason = as_str(p, "reason", max_len=300)                       # sabab majburiy
    product = ctx.docs.get(f"product:{pid}")
    if product is None:
        raise DomainError("not_found", "Mahsulot topilmadi", field="product_id")
    if product.status != "active":
        raise DomainError("inactive_reference", "Faol bo'lmagan mahsulotga narx qo'yib bo'lmaydi", field="product_id")
    cur = ctx.docs.get(f"price:{pid}")
    previous = cur.data["price"] if cur else None
    if previous == price:
        raise DomainError("price_unchanged", "Narx o'zgarmadi", price=price)
    ctx.emit("price_changed", {"product_id": pid, "price": price, "previous_price": previous, "reason": reason})
    return {"product_id": pid, "price": price, "previous_price": previous}
