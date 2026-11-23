"""FIFO yuklash va egasi tasdiqlaydigan inventarizatsiya."""
from backend.balances import keys
from backend.core.errors import Conflict, DomainError, Forbidden
from backend.core.timing import local_date
from backend.core.validate import as_int, as_list, as_str
from backend.modules.auth.users import UserStore
from backend.modules.pricing.queries import current_price
from backend.modules.sync.registry import command

from . import queries

_MAX_QUANTITY = 100_000_000
_MAX_LINES = 50


def _known_fields(payload: dict, allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        field = sorted(extra)[0]
        raise DomainError("unknown_field", f"Noma'lum maydon: {field}", field=field,
                          allowed=sorted(allowed))


def _active_product(ctx, product_id: str):
    product = ctx.docs.get(f"product:{product_id}")
    if product is None:
        raise DomainError("not_found", "Mahsulot topilmadi", field="product_id", id=product_id)
    if product.status != "active":
        raise DomainError("inactive_reference", "Faol bo'lmagan mahsulotni yuklab bo'lmaydi",
                          field="product_id", id=product_id)
    return product


def _lines(ctx, payload: dict) -> list[dict]:
    raw_lines = as_list(payload, "lines", min_len=1)
    if len(raw_lines) > _MAX_LINES:
        raise DomainError("invalid_field", f"'lines' ko'pi bilan {_MAX_LINES} ta bo'lishi mumkin",
                          field="lines")
    agent_id = as_str(payload, "agent_id", max_len=60)
    agent = UserStore(ctx.db).get(agent_id)
    if agent is None or not agent.active or not ({"agent", "owner"} & set(agent.tasks)):
        raise DomainError("invalid_field", "Yuk oluvchi faol agent bo'lishi kerak", field="agent_id")
    if agent_id == ctx.actor.user_id:
        raise DomainError("same_person_both_sides", "Yukni kiritgan odam o'zi qabul qila olmaydi",
                          what="loading")

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
        _active_product(ctx, product_id)
        price = current_price(ctx.docs, product_id)
        if price is None:
            raise DomainError("price_not_set", "Yuklashdan oldin mahsulot narxini belgilang",
                              product_id=product_id)

        remaining = quantity
        allocations = []
        for batch in queries.available_batches(ctx.bal, ctx.docs, product_id, local_date(ctx.occurred_at).isoformat()):
            take = min(remaining, batch["available"])
            if take:
                allocations.append({"batch_id": batch["batch_id"], "quantity": take})
                remaining -= take
            if remaining == 0:
                break
        if remaining:
            available = quantity - remaining
            raise DomainError("insufficient_finished_stock", "Yaroqli tayyor mahsulot qoldig'i yetarli emas",
                              product_id=product_id, available=available, requested=quantity)
        lines.append({
            "product_id": product_id,
            "quantity": quantity,
            "unit_price": price,
            "total_amount": price * quantity,
            "allocations": allocations,
        })
    return lines


@command("transfer.prepare.loading")
def prepare_loading(ctx, payload: dict) -> dict:
    _known_fields(payload, {"agent_id", "lines"})
    lines = _lines(ctx, payload)
    transfer_id = ctx.new_id("TR")
    quantity = sum(line["quantity"] for line in lines)
    amount = sum(line["total_amount"] for line in lines)
    data = {
        "transfer_id": transfer_id,
        "transfer_type": "loading",
        "agent_id": as_str(payload, "agent_id", max_len=60),
        "prepared_by": ctx.actor.user_id,
        "prepared_device_id": ctx.actor.device_id,
        "prepared_at": ctx.occurred_at,
        "gps": ctx.command.gps,
        "lines": lines,
        "total_quantity": quantity,
        "total_amount": amount,
    }
    ctx.emit("loading_prepared", data)
    return {"transfer_id": transfer_id, "agent_id": data["agent_id"],
            "total_quantity": quantity, "total_amount": amount, "lines": lines}


@command("transfer.accept.loading")
def accept_loading(ctx, payload: dict) -> dict:
    _known_fields(payload, {"transfer_id"})
    transfer_id = as_str(payload, "transfer_id", max_len=60)
    doc = ctx.docs.get(f"transfer:{transfer_id}")
    if doc is None:
        raise DomainError("not_found", "Yuklash hujjati topilmadi", field="transfer_id")
    if doc.status != "pending" or doc.data["transfer_type"] != "loading":
        raise DomainError("transfer_not_pending", "Faqat kutilayotgan yuklashni tasdiqlash mumkin",
                          status=doc.status)
    if ctx.actor.task not in ("agent", "owner") or ctx.actor.user_id != doc.data["agent_id"]:
        raise Forbidden("forbidden", "Yukni faqat belgilangan agent o'z hisobidan tasdiqlaydi",
                        agent_id=doc.data["agent_id"])
    ctx.two_party(doc.data["prepared_by"], "loading")
    if ctx.actor.device_id == doc.data["prepared_device_id"]:
        raise DomainError("same_device_both_sides", "Yuklash boshqa qurilmadan tasdiqlanishi kerak",
                          what="loading")

    today = local_date(ctx.occurred_at).isoformat()
    for line in doc.data["lines"]:
        for allocation in line["allocations"]:
            batch = ctx.docs.get(f"batch:{allocation['batch_id']}")
            if batch.data["expires_on"] < today:
                raise DomainError("batch_expired", "Muddati o'tgan partiyani qabul qilib bo'lmaydi",
                                  batch_id=allocation["batch_id"], expires_on=batch.data["expires_on"])
            available = ctx.bal.get(keys.fg(line["product_id"], allocation["batch_id"]))
            if available < allocation["quantity"]:
                raise Conflict("loading_stock_changed", "Tasdiqlashdan oldin sklad qoldig'i o'zgardi",
                               product_id=line["product_id"], batch_id=allocation["batch_id"],
                               available=available, requested=allocation["quantity"])
    ctx.emit("loading_accepted", {
        "transfer_id": transfer_id,
        "agent_id": doc.data["agent_id"],
        "lines": doc.data["lines"],
        "total_quantity": doc.data["total_quantity"],
        "total_amount": doc.data["total_amount"],
    })
    return {"transfer_id": transfer_id, "agent_id": doc.data["agent_id"],
            "total_quantity": doc.data["total_quantity"], "total_amount": doc.data["total_amount"]}


def _stock_key(ctx, payload: dict) -> tuple[str, str | None]:
    location = as_str(payload, "location", allowed=("warehouse", "custody"))
    product_id = as_str(payload, "product_id", max_len=60)
    batch_id = as_str(payload, "batch_id", max_len=60)
    batch = ctx.docs.get(f"batch:{batch_id}")
    if batch is None or batch.status != "posted":
        raise DomainError("not_found", "Qadoqlash partiyasi topilmadi", field="batch_id")
    if not any(line["product_id"] == product_id for line in batch.data["lines"]):
        raise DomainError("invalid_field", "Mahsulot bu partiyada yo'q", field="product_id",
                          product_id=product_id, batch_id=batch_id)
    if location == "warehouse":
        if payload.get("user_id") is not None:
            raise DomainError("invalid_field", "Ombor hisobida user_id berilmaydi", field="user_id")
        return keys.fg(product_id, batch_id), None
    user_id = as_str(payload, "user_id", max_len=60)
    if UserStore(ctx.db).get(user_id) is None:
        raise DomainError("not_found", "Zimma egasi topilmadi", field="user_id", user_id=user_id)
    return keys.custody(user_id, product_id, batch_id), user_id


@command("stock_count.submit")
def submit_stock_count(ctx, payload: dict) -> dict:
    _known_fields(payload, {"location", "user_id", "product_id", "batch_id", "counted_quantity", "reason"})
    balance_key, user_id = _stock_key(ctx, payload)
    system_quantity = ctx.bal.get(balance_key)
    counted_quantity = as_int(payload, "counted_quantity", minimum=0, maximum=_MAX_QUANTITY)
    difference = counted_quantity - system_quantity
    reason = as_str(payload, "reason", max_len=300, default="")
    if difference and not reason:
        raise DomainError("missing_field", "Inventarizatsiya farqi uchun sabab majburiy", field="reason")
    location = as_str(payload, "location", allowed=("warehouse", "custody"))
    count_id = ctx.new_id("CNT")
    data = {
        "count_id": count_id,
        "location": location,
        "user_id": user_id,
        "product_id": as_str(payload, "product_id", max_len=60),
        "batch_id": as_str(payload, "batch_id", max_len=60),
        "system_quantity": system_quantity,
        "counted_quantity": counted_quantity,
        "difference": difference,
        "reason": reason,
        "submitted_by": ctx.actor.user_id,
        "submitted_at": ctx.occurred_at,
    }
    ctx.emit("stock_count_submitted", data)
    if difference:
        ctx.notify("owner", "inventory_difference", None, data)
    return {"count_id": count_id, "system_quantity": system_quantity,
            "counted_quantity": counted_quantity, "difference": difference}


@command("stock_count.approve")
def approve_stock_count(ctx, payload: dict) -> dict:
    _known_fields(payload, {"count_id"})
    count_id = as_str(payload, "count_id", max_len=60)
    doc = ctx.docs.get(f"stock_count:{count_id}")
    if doc is None:
        raise DomainError("not_found", "Inventarizatsiya hujjati topilmadi", field="count_id")
    if doc.status != "pending":
        raise DomainError("stock_count_not_pending", "Faqat kutilayotgan inventarizatsiyani tasdiqlash mumkin",
                          status=doc.status)
    key = (keys.fg(doc.data["product_id"], doc.data["batch_id"]) if doc.data["location"] == "warehouse"
           else keys.custody(doc.data["user_id"], doc.data["product_id"], doc.data["batch_id"]))
    current_quantity = ctx.bal.get(key)
    if current_quantity != doc.data["system_quantity"]:
        raise Conflict("stock_changed_since_count", "Sanalganidan keyin qoldiq o'zgargan; qayta sanang",
                       expected=doc.data["system_quantity"], current=current_quantity)
    ctx.emit("stock_count_approved", {
        "count_id": count_id,
        "location": doc.data["location"],
        "user_id": doc.data["user_id"],
        "product_id": doc.data["product_id"],
        "batch_id": doc.data["batch_id"],
        "difference": doc.data["difference"],
        "counted_quantity": doc.data["counted_quantity"],
    })
    return {"count_id": count_id, "difference": doc.data["difference"],
            "counted_quantity": doc.data["counted_quantity"]}
