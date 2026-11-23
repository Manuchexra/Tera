"""Xarid va yetkazib beruvchiga to'lov buyruqlari."""
from decimal import Decimal

from backend.balances import keys
from backend.core.errors import DomainError
from backend.core.money import round_uzs
from backend.core.validate import as_int, as_str
from backend.modules.sync.registry import command

_PAYMENT_TYPES = ("cash", "card", "credit")
_MAX_QUANTITY = 100_000_000
_MAX_UNIT_PRICE = 100_000_000
_MAX_MONEY = 100_000_000


def _known_fields(payload: dict, allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        field = sorted(extra)[0]
        raise DomainError("unknown_field", f"Noma'lum maydon: {field}", field=field,
                          allowed=sorted(allowed))


def _active_doc(ctx, kind: str, ref_id: str, field: str):
    doc = ctx.docs.get(f"{kind}:{ref_id}")
    if doc is None:
        raise DomainError("not_found", f"{ref_id} topilmadi", field=field, id=ref_id)
    if doc.status != "active":
        raise DomainError("inactive_reference", f"{ref_id} faol emas", field=field, id=ref_id)
    return doc


def _supplier(ctx, supplier_id: str):
    return _active_doc(ctx, "supplier", supplier_id, "supplier_id")


def _payment(payload: dict, total: int, *, required: bool) -> tuple[str, int]:
    payment_type = as_str(payload, "payment_type", allowed=_PAYMENT_TYPES)
    if "paid_amount" in payload:
        paid = as_int(payload, "paid_amount", minimum=0, maximum=total)
    elif required:
        paid = as_int(payload, "paid_amount", minimum=0, maximum=total)
    else:
        paid = 0 if payment_type == "credit" else total

    if payment_type == "credit" and paid != 0:
        raise DomainError("invalid_field", "Qarz usulida paid_amount 0 bo'lishi kerak", field="paid_amount")
    if payment_type != "credit" and paid == 0:
        raise DomainError("invalid_field", "To'lov usulini tanlaganda paid_amount 0 dan katta bo'lishi kerak",
                          field="paid_amount")
    return payment_type, paid


def _purchase_response(purchase_id: str, goods_total: int, paid: int, road_cost: int = 0) -> dict:
    return {
        "purchase_id": purchase_id,
        "goods_total": goods_total,
        "paid_amount": paid,
        "supplier_debt_added": goods_total - paid,
        "road_cost": road_cost,
        "inventory_value_added": goods_total + road_cost,
    }


@command("purchase.raw")
def purchase_raw(ctx, payload: dict) -> dict:
    _known_fields(payload, {
        "material_id", "weight_g", "unit_price_per_kg", "supplier_id",
        "payment_type", "paid_amount", "road_cost",
    })
    material_id = as_str(payload, "material_id", max_len=60)
    _active_doc(ctx, "material", material_id, "material_id")
    supplier_id = as_str(payload, "supplier_id", max_len=60)
    supplier = _supplier(ctx, supplier_id)
    if material_id not in supplier.data["supplies"]:
        raise DomainError("supplier_not_supplies", "Yetkazib beruvchi bu xom ashyoni yetkazmaydi",
                          field="supplier_id", supplier_id=supplier_id, material_id=material_id)

    weight_g = as_int(payload, "weight_g", minimum=1, maximum=_MAX_QUANTITY)
    unit_price_per_kg = as_int(payload, "unit_price_per_kg", minimum=1, maximum=_MAX_UNIT_PRICE)
    goods_total = round_uzs(Decimal(weight_g) * unit_price_per_kg / Decimal(1000))
    if goods_total < 1:
        raise DomainError("invalid_field", "Xaridning hisoblangan summasi kamida 1 so'm bo'lishi kerak",
                          field="unit_price_per_kg")
    road_cost = as_int(payload, "road_cost", minimum=0, maximum=_MAX_MONEY, default=0)
    payment_type, paid = _payment(payload, goods_total, required=True)
    purchase_id = ctx.new_id("PUR")
    data = {
        "purchase_id": purchase_id,
        "purchase_type": "raw",
        "material_id": material_id,
        "weight_g": weight_g,
        "unit_price_per_kg": unit_price_per_kg,
        "goods_total": goods_total,
        "road_cost": road_cost,
        "inventory_value_added": goods_total + road_cost,
        "supplier_id": supplier_id,
        "payment_type": payment_type,
        "paid_amount": paid,
        "supplier_debt_added": goods_total - paid,
    }
    ctx.emit("raw_purchase_recorded", data)
    return _purchase_response(purchase_id, goods_total, paid, road_cost)


@command("purchase.packaging")
def purchase_packaging(ctx, payload: dict) -> dict:
    _known_fields(payload, {
        "material_id", "quantity", "unit_price", "supplier_id", "payment_type", "paid_amount",
    })
    material_id = as_str(payload, "material_id", max_len=60)
    _active_doc(ctx, "packmaterial", material_id, "material_id")
    supplier_id = as_str(payload, "supplier_id", max_len=60)
    _supplier(ctx, supplier_id)
    quantity = as_int(payload, "quantity", minimum=1, maximum=_MAX_QUANTITY)
    unit_price = as_int(payload, "unit_price", minimum=1, maximum=_MAX_UNIT_PRICE)
    goods_total = quantity * unit_price
    payment_type, paid = _payment(payload, goods_total, required=False)
    purchase_id = ctx.new_id("PUR")
    data = {
        "purchase_id": purchase_id,
        "purchase_type": "packaging",
        "material_id": material_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "goods_total": goods_total,
        "inventory_value_added": goods_total,
        "supplier_id": supplier_id,
        "payment_type": payment_type,
        "paid_amount": paid,
        "supplier_debt_added": goods_total - paid,
    }
    ctx.emit("packaging_purchase_recorded", data)
    return _purchase_response(purchase_id, goods_total, paid)


@command("supplier.pay")
def pay_supplier(ctx, payload: dict) -> dict:
    _known_fields(payload, {"supplier_id", "amount", "payment_type"})
    supplier_id = as_str(payload, "supplier_id", max_len=60)
    _supplier(ctx, supplier_id)
    amount = as_int(payload, "amount", minimum=1, maximum=_MAX_MONEY)
    payment_type = as_str(payload, "payment_type", allowed=("cash", "card"))
    outstanding = ctx.bal.get(keys.supplier_debt(supplier_id))
    if amount > outstanding:
        raise DomainError("insufficient_supplier_debt", "To'lov yetkazib beruvchining qarzidan oshib ketdi",
                          supplier_id=supplier_id, outstanding=outstanding, amount=amount)

    payment_id = ctx.new_id("SP")
    ctx.emit("supplier_payment_recorded", {
        "payment_id": payment_id,
        "supplier_id": supplier_id,
        "amount": amount,
        "payment_type": payment_type,
    })
    return {"payment_id": payment_id, "supplier_id": supplier_id,
            "amount": amount, "outstanding": outstanding - amount}
