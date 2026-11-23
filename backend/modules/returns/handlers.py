"""Mijoz qaytimi, agent skladga qaytarishi, almashtirish va write-off."""
from datetime import timedelta

from backend.balances import keys
from backend.core.errors import Conflict, DomainError
from backend.core.timing import local_date
from backend.core.validate import as_int, as_list, as_str
from backend.modules.approvals.rules import check_approval
from backend.modules.auth.users import UserStore
from backend.modules.sync.registry import command

from backend.modules.sales import queries as sales_queries

_MAX_QUANTITY = 100_000_000
_MAX_LINES = 50


def _known_fields(payload: dict, allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        field = sorted(extra)[0]
        raise DomainError("unknown_field", f"Noma'lum maydon: {field}", field=field,
                          allowed=sorted(allowed))


def _custody_batches(balances, docs, agent_id: str, product_id: str,
                     as_of_date: str | None = None) -> list[dict]:
    batches = []
    for batch in docs.query("batch", status="posted"):
        line = next((item for item in batch.data["lines"] if item["product_id"] == product_id), None)
        if line is None or (as_of_date and batch.data["expires_on"] < as_of_date):
            continue
        quantity = balances.get(keys.custody(agent_id, product_id, batch.data["batch_id"]))
        if quantity > 0:
            batches.append({
                "batch_id": batch.data["batch_id"],
                "available": quantity,
                "packaged_at": batch.data["packaged_at"],
                "expires_on": batch.data["expires_on"],
            })
    return sorted(batches, key=lambda item: (item["packaged_at"], item["batch_id"]))


def _replacement_lines(ctx, raw_lines: list, agent_id: str) -> list[dict]:
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
        unit_price = sales_queries.current_price(ctx.docs, product_id)
        if unit_price is None:
            raise DomainError("price_not_set", "Almashtirishdan oldin mahsulot narxini belgilang",
                              product_id=product_id)
        remaining, allocations = quantity, []
        for batch in _custody_batches(ctx.bal, ctx.docs, agent_id, product_id, today):
            take = min(remaining, batch["available"])
            if take:
                allocations.append({"batch_id": batch["batch_id"], "quantity": take})
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise DomainError("insufficient_custody_stock", "Almashtirish uchun agent qoldig'i yetarli emas",
                              product_id=product_id, available=quantity - remaining, requested=quantity)
        lines.append({
            "product_id": product_id,
            "quantity": quantity,
            "unit_price": unit_price,
            "gross_amount": unit_price * quantity,
            "discount_amount": 0,
            "total_amount": unit_price * quantity,
            "allocations": allocations,
        })
    return lines


def _return_plan(ctx, sale, raw_lines: list) -> tuple[list[dict], int]:
    if len(raw_lines) > _MAX_LINES:
        raise DomainError("invalid_field", f"'lines' ko'pi bilan {_MAX_LINES} ta bo'lishi mumkin",
                          field="lines")
    previous_by_batch: dict[tuple[str, str], int] = {}
    previous_by_product: dict[str, int] = {}
    for returned in ctx.docs.query("return", ref=sale.data["sale_id"]):
        for line in returned.data["lines"]:
            previous_by_product[line["product_id"]] = (
                previous_by_product.get(line["product_id"], 0) + line["quantity"])
            for allocation in line["allocations"]:
                key = (line["product_id"], allocation["batch_id"])
                previous_by_batch[key] = previous_by_batch.get(key, 0) + allocation["quantity"]

    seen, lines, total_refund = set(), [], 0
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
        sale_line = next((line for line in sale.data["lines"] if line["product_id"] == product_id), None)
        if sale_line is None:
            raise DomainError("product_not_in_sale", "Mahsulot bu sotuvda yo'q",
                              sale_id=sale.data["sale_id"], product_id=product_id)
        already_returned = previous_by_product.get(product_id, 0)
        if quantity + already_returned > sale_line["quantity"]:
            raise DomainError("return_quantity_exceeded", "Qaytim miqdori sotilgan qoldiqdan oshdi",
                              product_id=product_id, sold=sale_line["quantity"],
                              already_returned=already_returned, requested=quantity)

        remaining, allocations = quantity, []
        for original in sale_line["allocations"]:
            key = (product_id, original["batch_id"])
            available = original["quantity"] - previous_by_batch.get(key, 0)
            take = min(remaining, available)
            if take:
                allocations.append({"batch_id": original["batch_id"], "quantity": take})
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise Conflict("return_allocation_changed", "Sotuv partiyasi bo'yicha qaytim qoldig'i mos emas",
                           sale_id=sale.data["sale_id"], product_id=product_id)

        base, remainder = divmod(sale_line["total_amount"], sale_line["quantity"])
        refund_before = base * already_returned + min(already_returned, remainder)
        refund_after_units = already_returned + quantity
        refund_after = base * refund_after_units + min(refund_after_units, remainder)
        return_amount = refund_after - refund_before
        lines.append({
            "product_id": product_id,
            "quantity": quantity,
            "allocations": allocations,
            "return_amount": return_amount,
        })
        total_refund += return_amount
    return lines, total_refund


def _get_sale(ctx, sale_id: str):
    sale = ctx.docs.get(f"sale:{sale_id}")
    if sale is None:
        raise DomainError("not_found", "Sotuv topilmadi", field="sale_id")
    return sale


def _actor_can_handle_store(ctx, store_id: str) -> None:
    store = ctx.docs.get(f"store:{store_id}")
    if store is None or store.status != "active":
        raise DomainError("not_found", "Faol do'kon topilmadi", field="store_id")
    if ctx.actor.task == "agent" and store.data["agent_id"] != ctx.actor.user_id:
        raise DomainError("store_not_assigned", "Bu do'kon sizga biriktirilmagan",
                          store_id=store_id)


@command("return.declare")
def declare_return(ctx, payload: dict) -> dict:
    _known_fields(payload, {"sale_id", "lines", "reason"})
    sale_id = as_str(payload, "sale_id", max_len=60)
    sale = _get_sale(ctx, sale_id)
    _actor_can_handle_store(ctx, sale.data["store_id"])
    reason = as_str(payload, "reason", max_len=300)
    raw_lines = as_list(payload, "lines", min_len=1)
    lines, return_amount = _return_plan(ctx, sale, raw_lines)
    return_id = ctx.new_id("RET")
    data = {
        "return_id": return_id,
        "sale_id": sale_id,
        "store_id": sale.data["store_id"],
        "agent_id": sale.data["seller_id"],
        "lines": lines,
        "return_amount": return_amount,
        "reason": reason,
        "declared_by": ctx.actor.user_id,
        "declared_device_id": ctx.actor.device_id,
    }
    ctx.emit("return_declared", data)
    return {"return_id": return_id, "sale_id": sale_id,
            "return_amount": return_amount, "lines": lines}


@command("return.accept")
def accept_return(ctx, payload: dict) -> dict:
    _known_fields(payload, {"return_id", "disposition"})
    return_id = as_str(payload, "return_id", max_len=60)
    returned = ctx.docs.get(f"return:{return_id}")
    if returned is None:
        raise DomainError("not_found", "Qaytim topilmadi", field="return_id")
    if returned.status != "pending":
        raise DomainError("return_not_pending", "Faqat kutilayotgan qaytimni qabul qilish mumkin",
                          status=returned.status)
    ctx.two_party(returned.data["declared_by"], "return")
    if ctx.actor.device_id == returned.data["declared_device_id"]:
        raise DomainError("same_device_both_sides", "Qaytim boshqa qurilmada tasdiqlanishi kerak",
                          what="return")
    disposition = as_str(payload, "disposition", allowed=("restock", "quarantine"))
    today = local_date(ctx.occurred_at).isoformat()
    for line in returned.data["lines"]:
        for allocation in line["allocations"]:
            batch_id = allocation["batch_id"]
            batch = ctx.docs.get(f"batch:{batch_id}")
            if batch is None:
                raise DomainError("not_found", "Qaytim partiyasi topilmadi", batch_id=batch_id)
            if disposition == "restock" and batch.data["expires_on"] < today:
                raise DomainError("batch_expired", "Muddati o'tgan qaytimni omborga sotiladigan qilib qo'yib bo'lmaydi",
                                  batch_id=batch_id, expires_on=batch.data["expires_on"])
    ctx.emit("return_accepted", {
        "return_id": return_id,
        "disposition": disposition,
        "lines": returned.data["lines"],
    })
    return {"return_id": return_id, "disposition": disposition,
            "return_amount": returned.data["return_amount"]}


@command("return.settle")
def settle_return(ctx, payload: dict) -> dict:
    _known_fields(payload, {"return_id"})
    ctx.require_fresh()
    return_id = as_str(payload, "return_id", max_len=60)
    returned = ctx.docs.get(f"return:{return_id}")
    if returned is None:
        raise DomainError("not_found", "Qaytim topilmadi", field="return_id")
    if returned.status != "accepted":
        raise DomainError("return_not_accepted", "Faqat sklad qabul qilgan qaytimni hisoblash mumkin",
                          status=returned.status)
    sale = _get_sale(ctx, returned.data["sale_id"])
    debt_reduction = min(returned.data["return_amount"], sale.data["outstanding"])
    advance_added = returned.data["return_amount"] - debt_reduction
    ctx.emit("return_settled", {
        "return_id": return_id,
        "sale_id": sale.data["sale_id"],
        "store_id": sale.data["store_id"],
        "return_amount": returned.data["return_amount"],
        "debt_reduction": debt_reduction,
        "advance_added": advance_added,
    })
    return {"return_id": return_id, "return_amount": returned.data["return_amount"],
            "debt_reduction": debt_reduction, "advance_added": advance_added}


@command("exchange.approve")
def approve_exchange(ctx, payload: dict) -> dict:
    _known_fields(payload, {"return_id", "lines", "credit_exception_id"})
    ctx.require_fresh()
    return_id = as_str(payload, "return_id", max_len=60)
    returned = ctx.docs.get(f"return:{return_id}")
    if returned is None:
        raise DomainError("not_found", "Qaytim topilmadi", field="return_id")
    if returned.status != "accepted":
        raise DomainError("return_not_accepted", "Almashtirish uchun qaytim avval qabul qilinishi kerak",
                          status=returned.status)
    sale = _get_sale(ctx, returned.data["sale_id"])
    lines = _replacement_lines(ctx, as_list(payload, "lines", min_len=1), returned.data["agent_id"])
    replacement_total = sum(line["total_amount"] for line in lines)
    debt_reduction = min(returned.data["return_amount"], sale.data["outstanding"])
    returned_credit = returned.data["return_amount"] - debt_reduction
    available_advance = ctx.bal.get(keys.advance(sale.data["store_id"])) + returned_credit
    advance_used = min(available_advance, replacement_total)
    advance_added = available_advance - advance_used
    new_credit = replacement_total - advance_used
    resulting_debt = ctx.bal.get(keys.debt(sale.data["store_id"])) - debt_reduction + new_credit
    over_limit = max(0, resulting_debt - ctx.policy.credit_limit)
    credit_exception_id = payload.get("credit_exception_id")
    if over_limit:
        credit_exception_id = as_str(payload, "credit_exception_id", max_len=60)
        check_approval(ctx.docs, credit_exception_id, kind="credit_exception", now=ctx.occurred_at,
                       user_id=ctx.actor.user_id, store_id=sale.data["store_id"], amount=over_limit)
    elif credit_exception_id is not None:
        raise DomainError("approval_not_needed", "Limit oshmasa credit_exception_id berilmaydi",
                          field="credit_exception_id")

    exchange_id = ctx.new_id("EX")
    replacement_sale_id = ctx.new_id("SALE")
    due_date = (local_date(ctx.occurred_at) + timedelta(days=ctx.policy.payment_term_days)).isoformat()
    ctx.emit("return_exchanged", {
        "exchange_id": exchange_id,
        "return_id": return_id,
        "original_sale_id": sale.data["sale_id"],
        "replacement_sale_id": replacement_sale_id,
        "store_id": sale.data["store_id"],
        "return_amount": returned.data["return_amount"],
        "debt_reduction": debt_reduction,
        "return_credit_to_advance": returned_credit,
        "advance_used": advance_used,
        "advance_remaining": advance_added,
        "replacement_total": replacement_total,
    })
    ctx.emit("sale_recorded", {
        "sale_id": replacement_sale_id,
        "exchange_id": exchange_id,
        "store_id": sale.data["store_id"],
        "seller_id": returned.data["agent_id"],
        "lines": lines,
        "subtotal": replacement_total,
        "discount_percent": 0,
        "discount_amount": 0,
        "total_amount": replacement_total,
        "advance_used": advance_used,
        "paid_amount": 0,
        "payment_type": "credit",
        "credit_amount": new_credit,
        "due_date": due_date,
        "approval_ids": [credit_exception_id] if credit_exception_id else [],
        "discount_approval_id": None,
        "credit_exception_id": credit_exception_id,
        "cash_recipient": "company",
    })
    return {
        "exchange_id": exchange_id,
        "return_id": return_id,
        "replacement_sale_id": replacement_sale_id,
        "return_amount": returned.data["return_amount"],
        "debt_reduction": debt_reduction,
        "advance_used": advance_used,
        "advance_added": advance_added,
        "replacement_total": replacement_total,
        "credit_amount": new_credit,
        "due_date": due_date,
    }


@command("writeoff.approve")
def approve_writeoff(ctx, payload: dict) -> dict:
    _known_fields(payload, {"product_id", "batch_id", "quantity", "reason"})
    ctx.require_fresh()
    product_id = as_str(payload, "product_id", max_len=60)
    batch_id = as_str(payload, "batch_id", max_len=60)
    quantity = as_int(payload, "quantity", minimum=1, maximum=_MAX_QUANTITY)
    reason = as_str(payload, "reason", max_len=300)
    batch = ctx.docs.get(f"batch:{batch_id}")
    if batch is None or not any(line["product_id"] == product_id for line in batch.data["lines"]):
        raise DomainError("not_found", "Mahsulot/partiya mos kelmadi", field="batch_id")
    available = ctx.bal.get(keys.quarantine(product_id, batch_id))
    if quantity > available:
        raise DomainError("insufficient_quarantine_stock", "Write-off karantin qoldig'idan oshib ketdi",
                          available=available, requested=quantity)
    batch_line = next(line for line in batch.data["lines"] if line["product_id"] == product_id)
    writeoff_value = batch_line["unit_cost"] * quantity
    writeoff_id = ctx.new_id("WO")
    ctx.emit("writeoff_approved", {
        "writeoff_id": writeoff_id,
        "product_id": product_id,
        "batch_id": batch_id,
        "quantity": quantity,
        "writeoff_value": writeoff_value,
        "reason": reason,
    })
    return {"writeoff_id": writeoff_id, "quantity": quantity, "writeoff_value": writeoff_value}


@command("transfer.prepare.custody_return")
def prepare_custody_return(ctx, payload: dict) -> dict:
    _known_fields(payload, {"agent_id", "lines"})
    agent_id = as_str(payload, "agent_id", max_len=60) if ctx.actor.task == "owner" \
        else ctx.actor.user_id
    if ctx.actor.task == "agent" and payload.get("agent_id", agent_id) != agent_id:
        raise DomainError("invalid_field", "Agent faqat o'z zimmasidagi tovarni qaytaradi",
                          field="agent_id")
    if ctx.actor.task == "agent" and "agent_id" in payload:
        as_str(payload, "agent_id", max_len=60)
    agent = UserStore(ctx.db).get(agent_id)
    if agent is None or not agent.active or not ({"agent", "owner"} & set(agent.tasks)):
        raise DomainError("invalid_field", "Qaytaruvchi faol agent bo'lishi kerak", field="agent_id")

    raw_lines = as_list(payload, "lines", min_len=1)
    if len(raw_lines) > _MAX_LINES:
        raise DomainError("invalid_field", f"'lines' ko'pi bilan {_MAX_LINES} ta bo'lishi mumkin",
                          field="lines")
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
        remaining, allocations = quantity, []
        for batch in _custody_batches(ctx.bal, ctx.docs, agent_id, product_id):
            take = min(remaining, batch["available"])
            if take:
                allocations.append({"batch_id": batch["batch_id"], "quantity": take})
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise DomainError("insufficient_custody_stock", "Agent zimmasidagi qoldiq yetarli emas",
                              product_id=product_id, available=quantity - remaining, requested=quantity)
        lines.append({"product_id": product_id, "quantity": quantity, "allocations": allocations})

    transfer_id = ctx.new_id("TR")
    data = {
        "transfer_id": transfer_id,
        "transfer_type": "custody_return",
        "agent_id": agent_id,
        "prepared_by": ctx.actor.user_id,
        "prepared_device_id": ctx.actor.device_id,
        "prepared_at": ctx.occurred_at,
        "lines": lines,
    }
    ctx.emit("custody_return_prepared", data)
    return {"transfer_id": transfer_id, "agent_id": agent_id, "lines": lines}


@command("transfer.accept.custody_return")
def accept_custody_return(ctx, payload: dict) -> dict:
    _known_fields(payload, {"transfer_id", "disposition"})
    transfer_id = as_str(payload, "transfer_id", max_len=60)
    transfer = ctx.docs.get(f"transfer:{transfer_id}")
    if transfer is None:
        raise DomainError("not_found", "Tovar qaytarish hujjati topilmadi", field="transfer_id")
    if transfer.status != "pending" or transfer.data["transfer_type"] != "custody_return":
        raise DomainError("transfer_not_pending", "Faqat kutilayotgan sklad qaytarishini tasdiqlash mumkin",
                          status=transfer.status)
    ctx.two_party(transfer.data["prepared_by"], "custody_return")
    if ctx.actor.device_id == transfer.data["prepared_device_id"]:
        raise DomainError("same_device_both_sides", "Qabul boshqa qurilmada tasdiqlanishi kerak",
                          what="custody_return")
    disposition = as_str(payload, "disposition", allowed=("restock", "quarantine"))
    today = local_date(ctx.occurred_at).isoformat()
    for line in transfer.data["lines"]:
        for allocation in line["allocations"]:
            available = ctx.bal.get(keys.custody(
                transfer.data["agent_id"], line["product_id"], allocation["batch_id"]))
            if available < allocation["quantity"]:
                raise Conflict("custody_stock_changed", "Qabul qilishdan oldin agent qoldig'i o'zgardi",
                               product_id=line["product_id"], batch_id=allocation["batch_id"],
                               available=available, requested=allocation["quantity"])
            batch = ctx.docs.get(f"batch:{allocation['batch_id']}")
            if disposition == "restock" and batch.data["expires_on"] < today:
                raise DomainError("batch_expired", "Muddati o'tgan tovarni sotiladigan skladga qaytarib bo'lmaydi",
                                  batch_id=allocation["batch_id"], expires_on=batch.data["expires_on"])
    ctx.emit("custody_return_accepted", {
        "transfer_id": transfer_id,
        "agent_id": transfer.data["agent_id"],
        "lines": transfer.data["lines"],
        "disposition": disposition,
    })
    return {"transfer_id": transfer_id, "disposition": disposition}
