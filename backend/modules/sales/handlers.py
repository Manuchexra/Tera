"""Sotuv, do'kon to'lovi, muddat uzaytirish va qarz bahsi buyruqlari."""
from datetime import date, timedelta
from decimal import Decimal

from backend.balances import keys
from backend.core.errors import Conflict, DomainError
from backend.core.money import allocate_proportionally, round_uzs
from backend.core.timing import add_working_days, local_date
from backend.core.validate import as_int, as_list, as_str
from backend.modules.approvals.rules import check_approval
from backend.modules.sync.registry import command

from . import queries

_MAX_QUANTITY = 100_000_000
_MAX_MONEY = 100_000_000
_MAX_LINES = 50
_PAYMENT_TYPES = ("cash", "card", "credit")


def _known_fields(payload: dict, allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        field = sorted(extra)[0]
        raise DomainError("unknown_field", f"Noma'lum maydon: {field}", field=field,
                          allowed=sorted(allowed))


def _active_store(ctx, store_id: str):
    store = ctx.docs.get(f"store:{store_id}")
    if store is None:
        raise DomainError("not_found", "Do'kon topilmadi", field="store_id", id=store_id)
    if store.status != "active" or not store.data.get("active", True):
        raise DomainError("inactive_reference", "Do'kon faol emas", field="store_id", id=store_id)
    if ctx.actor.task == "agent" and store.data["agent_id"] != ctx.actor.user_id:
        raise DomainError("store_not_assigned", "Bu do'kon sizga biriktirilmagan",
                          store_id=store_id)
    return store


def _sale_lines(ctx, payload: dict, seller_id: str) -> list[dict]:
    raw_lines = as_list(payload, "lines", min_len=1)
    if len(raw_lines) > _MAX_LINES:
        raise DomainError("invalid_field", f"'lines' ko'pi bilan {_MAX_LINES} ta bo'lishi mumkin",
                          field="lines")

    today = local_date(ctx.occurred_at).isoformat()
    seen, lines = set(), []
    for index, item in enumerate(raw_lines):
        if not isinstance(item, dict) or set(item) != {"product_id", "quantity"}:
            raise DomainError("invalid_field", "Har bir qator {product_id, quantity} bo'lishi kerak",
                              field="lines", index=index)
        product_id = as_str(item, "product_id", max_len=60)
        if product_id in seen:
            raise DomainError("invalid_field", "Bir mahsulot ikki marta ko'rsatilgan",
                              field="lines", product_id=product_id)
        seen.add(product_id)
        quantity = as_int(item, "quantity", minimum=1, maximum=_MAX_QUANTITY)
        product = ctx.docs.get(f"product:{product_id}")
        if product is None:
            raise DomainError("not_found", "Mahsulot topilmadi", field="product_id", id=product_id)
        if product.status != "active":
            raise DomainError("inactive_reference", "Mahsulot faol emas", field="product_id",
                              id=product_id)
        unit_price = queries.current_price(ctx.docs, product_id)
        if unit_price is None:
            raise DomainError("price_not_set", "Sotishdan oldin mahsulot narxini belgilang",
                              product_id=product_id)

        remaining, allocations = quantity, []
        for batch in queries.available_custody_batches(
                ctx.bal, ctx.docs, seller_id, product_id, today):
            take = min(remaining, batch["available"])
            if take:
                allocations.append({"batch_id": batch["batch_id"], "quantity": take})
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise DomainError("insufficient_custody_stock", "Agent zimmasidagi yaroqli qoldiq yetarli emas",
                              product_id=product_id, available=quantity - remaining, requested=quantity)
        lines.append({
            "product_id": product_id,
            "quantity": quantity,
            "unit_price": unit_price,
            "gross_amount": unit_price * quantity,
            "allocations": allocations,
        })
    return lines


@command("sale.create")
def create_sale(ctx, payload: dict) -> dict:
    _known_fields(payload, {
        "store_id", "lines", "discount_percent", "discount_approval_id",
        "credit_exception_id", "payment_type", "paid_amount",
    })
    store_id = as_str(payload, "store_id", max_len=60)
    store = _active_store(ctx, store_id)
    seller_id = ctx.actor.user_id if ctx.actor.task == "agent" else store.data["agent_id"]
    lines = _sale_lines(ctx, payload, seller_id)

    subtotal = sum(line["gross_amount"] for line in lines)
    discount_percent = as_int(payload, "discount_percent", minimum=0, maximum=99, default=0)
    discount_amount = round_uzs(Decimal(subtotal) * discount_percent / Decimal(100))
    discount_approval_id = payload.get("discount_approval_id")
    if discount_percent:
        if discount_amount == 0:
            raise DomainError("discount_too_small", "Yaxlitlanganda chegirma 0 so'm bo'lyapti",
                              field="discount_percent")
        discount_approval_id = as_str(payload, "discount_approval_id", max_len=60)
        check_approval(ctx.docs, discount_approval_id, kind="discount", now=ctx.occurred_at,
                       user_id=ctx.actor.user_id, store_id=store_id,
                       amount=discount_amount, percent=discount_percent)
        for line, line_discount in zip(lines, allocate_proportionally(
                discount_amount, [line["gross_amount"] for line in lines])):
            line["discount_amount"] = line_discount
            line["total_amount"] = line["gross_amount"] - line_discount
    else:
        if discount_approval_id is not None:
            raise DomainError("approval_not_needed", "Chegirma bo'lmasa discount_approval_id berilmaydi",
                              field="discount_approval_id")
        for line in lines:
            line["discount_amount"] = 0
            line["total_amount"] = line["gross_amount"]

    total_amount = subtotal - discount_amount
    advance_available = ctx.bal.get(keys.advance(store_id))
    advance_used = min(advance_available, total_amount)
    remaining_after_advance = total_amount - advance_used
    payment_type = as_str(payload, "payment_type", allowed=_PAYMENT_TYPES)
    if payment_type == "credit":
        paid_amount = as_int(payload, "paid_amount", minimum=0, maximum=0, default=0)
    else:
        paid_amount = as_int(payload, "paid_amount", minimum=0, maximum=remaining_after_advance,
                             default=remaining_after_advance)
        if remaining_after_advance and paid_amount == 0:
            raise DomainError("invalid_field", "To'lov 0 bo'lsa payment_type='credit' tanlang",
                              field="paid_amount")
    credit_amount = remaining_after_advance - paid_amount
    current_debt = ctx.bal.get(keys.debt(store_id))
    over_limit = max(0, current_debt + credit_amount - ctx.policy.credit_limit)
    credit_exception_id = payload.get("credit_exception_id")
    if over_limit:
        credit_exception_id = as_str(payload, "credit_exception_id", max_len=60)
        check_approval(ctx.docs, credit_exception_id, kind="credit_exception", now=ctx.occurred_at,
                       user_id=ctx.actor.user_id, store_id=store_id, amount=over_limit)
    elif credit_exception_id is not None:
        raise DomainError("approval_not_needed", "Limit oshmasa credit_exception_id berilmaydi",
                          field="credit_exception_id")

    sale_id = ctx.new_id("SALE")
    due_date = (local_date(ctx.occurred_at) + timedelta(days=ctx.policy.payment_term_days)).isoformat()
    approval_ids = [approval_id for approval_id in (discount_approval_id, credit_exception_id)
                    if approval_id is not None]
    data = {
        "sale_id": sale_id,
        "store_id": store_id,
        "seller_id": seller_id,
        "lines": lines,
        "subtotal": subtotal,
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
        "total_amount": total_amount,
        "advance_used": advance_used,
        "paid_amount": paid_amount,
        "payment_type": payment_type,
        "credit_amount": credit_amount,
        "due_date": due_date,
        "approval_ids": approval_ids,
        "discount_approval_id": discount_approval_id,
        "credit_exception_id": credit_exception_id,
        "cash_recipient": "agent" if ctx.actor.task == "agent" else "company",
    }
    ctx.emit("sale_recorded", data)
    return {
        "sale_id": sale_id,
        "subtotal": subtotal,
        "discount_amount": discount_amount,
        "total_amount": total_amount,
        "advance_used": advance_used,
        "paid_amount": paid_amount,
        "credit_amount": credit_amount,
        "due_date": due_date,
        "lines": lines,
    }


@command("payment.receive")
def receive_payment(ctx, payload: dict) -> dict:
    _known_fields(payload, {"store_id", "amount", "payment_type"})
    store_id = as_str(payload, "store_id", max_len=60)
    _active_store(ctx, store_id)
    amount = as_int(payload, "amount", minimum=1, maximum=_MAX_MONEY)
    payment_type = as_str(payload, "payment_type", allowed=("cash", "card"))

    sales = queries.oldest_open_sales(ctx.docs, store_id)
    documented_debt = sum(sale["outstanding"] for sale in sales)
    balance_debt = ctx.bal.get(keys.debt(store_id))
    if documented_debt != balance_debt:
        raise Conflict("debt_projection_mismatch", "Do'kon qarzi hujjatlar bilan mos emas; egasi tekshirishi kerak",
                       store_id=store_id, balance=balance_debt, documented=documented_debt)

    remaining = amount
    allocations = []
    for sale in sales:
        take = min(remaining, sale["outstanding"])
        if take:
            allocations.append({"sale_id": sale["sale_id"], "amount": take})
            remaining -= take
        if remaining == 0:
            break
    payment_id = ctx.new_id("PAY")
    cash_recipient = "agent" if ctx.actor.task == "agent" else "company"
    data = {
        "payment_id": payment_id,
        "store_id": store_id,
        "amount": amount,
        "payment_type": payment_type,
        "allocations": allocations,
        "advance_added": remaining,
        "cash_recipient": cash_recipient,
        "recipient_id": ctx.actor.user_id,
    }
    ctx.emit("store_payment_received", data)
    return {
        "payment_id": payment_id,
        "store_id": store_id,
        "amount": amount,
        "allocations": allocations,
        "advance_added": remaining,
        "outstanding": balance_debt - (amount - remaining),
    }


@command("term.extend")
def extend_term(ctx, payload: dict) -> dict:
    _known_fields(payload, {"sale_id", "additional_days", "reason"})
    ctx.require_fresh()
    sale_id = as_str(payload, "sale_id", max_len=60)
    sale = ctx.docs.get(f"sale:{sale_id}")
    if sale is None:
        raise DomainError("not_found", "Sotuv topilmadi", field="sale_id")
    if sale.status != "open" or sale.data["outstanding"] <= 0:
        raise DomainError("sale_not_open", "Faqat ochiq qarzning muddatini uzaytirish mumkin",
                          status=sale.status)
    additional_days = as_int(payload, "additional_days", minimum=1, maximum=180)
    reason = as_str(payload, "reason", max_len=300)
    old_due_date = sale.data["due_date"]
    new_due_date = (date.fromisoformat(old_due_date) + timedelta(days=additional_days)).isoformat()
    ctx.emit("sale_term_extended", {
        "sale_id": sale_id,
        "store_id": sale.data["store_id"],
        "old_due_date": old_due_date,
        "new_due_date": new_due_date,
        "additional_days": additional_days,
        "reason": reason,
    })
    return {"sale_id": sale_id, "old_due_date": old_due_date, "new_due_date": new_due_date}


@command("dispute.open")
def open_dispute(ctx, payload: dict) -> dict:
    _known_fields(payload, {"sale_id", "amount", "reason"})
    sale_id = as_str(payload, "sale_id", max_len=60)
    sale = ctx.docs.get(f"sale:{sale_id}")
    if sale is None:
        raise DomainError("not_found", "Sotuv topilmadi", field="sale_id")
    outstanding = sale.data["outstanding"]
    if outstanding <= 0:
        raise DomainError("sale_not_open", "Faqat ochiq qarz bo'yicha bahs ochiladi",
                          sale_id=sale_id)
    amount = as_int(payload, "amount", minimum=1, maximum=outstanding, default=outstanding)
    reason = as_str(payload, "reason", max_len=300)
    for dispute in ctx.docs.query("dispute", ref=sale.data["store_id"], status="open"):
        if dispute.data["sale_id"] == sale_id:
            raise DomainError("dispute_already_open", "Bu sotuv bo'yicha ochiq bahs bor",
                              sale_id=sale_id, dispute_id=dispute.data["dispute_id"])

    dispute_id = ctx.new_id("DIS")
    resolution_due = add_working_days(local_date(ctx.occurred_at), ctx.policy.dispute_working_days).isoformat()
    ctx.emit("dispute_opened", {
        "dispute_id": dispute_id,
        "sale_id": sale_id,
        "store_id": sale.data["store_id"],
        "amount": amount,
        "reason": reason,
        "resolution_due": resolution_due,
    })
    return {"dispute_id": dispute_id, "sale_id": sale_id, "amount": amount,
            "resolution_due": resolution_due}


@command("dispute.resolve")
def resolve_dispute(ctx, payload: dict) -> dict:
    _known_fields(payload, {"dispute_id", "decision", "reason"})
    dispute_id = as_str(payload, "dispute_id", max_len=60)
    dispute = ctx.docs.get(f"dispute:{dispute_id}")
    if dispute is None:
        raise DomainError("not_found", "Bahs topilmadi", field="dispute_id")
    if dispute.status != "open":
        raise DomainError("dispute_not_open", "Faqat ochiq bahsni hal qilish mumkin",
                          status=dispute.status)
    decision = as_str(payload, "decision", allowed=("accepted", "rejected"))
    reason = as_str(payload, "reason", max_len=300)
    ctx.emit("dispute_resolved", {
        "dispute_id": dispute_id,
        "decision": decision,
        "reason": reason,
    })
    return {"dispute_id": dispute_id, "decision": decision}
