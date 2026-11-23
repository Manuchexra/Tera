"""Ma'lumotnoma qoidalari: ishlatilayotgan narsani o'chirib bo'lmaydi, nomlar takrorlanmaydi."""
from backend.balances import keys
from backend.core.errors import DomainError


def product_stock(bal, product_id: str) -> int:
    """Mahsulotning barcha joylardagi qoldig'i: ombor, agent zimmasi, agentdagi qaytim, karantin."""
    total = 0
    for prefix, idx in (("fg:", 1), ("custody:", 2), ("hold:", 2), ("quarantine:", 1)):
        for key, value in bal.scan(prefix).items():
            if key.split(":")[idx] == product_id:
                total += value
    return total


def ensure_can_deactivate(bal, kind: str, ref_id: str) -> None:
    if kind == "material" and bal.get(keys.raw(ref_id)) != 0:
        raise DomainError("in_use", "Omborda qoldig'i bor xom ashyoni o'chirib bo'lmaydi", stock_g=bal.get(keys.raw(ref_id)))
    if kind == "packmaterial" and bal.get(keys.pack(ref_id)) != 0:
        raise DomainError("in_use", "Omborda qoldig'i bor qadoq materialini o'chirib bo'lmaydi", stock=bal.get(keys.pack(ref_id)))
    if kind == "product":
        stock = product_stock(bal, ref_id)
        if stock != 0:
            raise DomainError("in_use", "Qoldig'i bor mahsulotni o'chirib bo'lmaydi", stock=stock)
    if kind == "store":
        debt, adv = bal.get(keys.debt(ref_id)), bal.get(keys.advance(ref_id))
        if debt != 0 or adv != 0:
            raise DomainError("in_use", "Qarzi yoki oldindan to'lovi bor do'konni o'chirib bo'lmaydi", debt=debt, advance=adv)
    if kind == "supplier" and bal.get(keys.supplier_debt(ref_id)) != 0:
        raise DomainError("in_use", "Qarzi bor yetkazib beruvchini o'chirib bo'lmaydi", debt=bal.get(keys.supplier_debt(ref_id)))


def ensure_unique_name(docs, kind: str, name: str, self_id: str | None) -> None:
    low = name.strip().lower()
    for d in docs.query(kind):
        if d.doc_id != f"{kind}:{self_id}" and (d.data.get("name") or "").strip().lower() == low:
            raise DomainError("duplicate_name", f"Bunday nomli yozuv allaqachon bor: {name}", field="name")
