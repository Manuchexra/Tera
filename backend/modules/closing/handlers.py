"""Agent kun yopishi va hisobchiga naqd topshirish."""
from backend.balances import keys
from backend.core.errors import Conflict, DomainError
from backend.core.timing import local_date
from backend.core.validate import as_int, as_str
from backend.modules.sync.registry import command

from . import queries

_MAX_MONEY = 100_000_000


def _known_fields(payload: dict, allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        field = sorted(extra)[0]
        raise DomainError("unknown_field", f"Noma'lum maydon: {field}", field=field,
                          allowed=sorted(allowed))


@command("closing.submit")
def submit_closing(ctx, payload: dict) -> dict:
    _known_fields(payload, {"cash_counted", "cash_handover_amount", "reason"})
    agent_id = ctx.actor.user_id
    business_date = local_date(ctx.occurred_at).isoformat()
    for closing in ctx.docs.query("closing", ref=agent_id):
        if closing.data["business_date"] == business_date:
            raise DomainError("closing_already_submitted", "Bu kun uchun yopish allaqachon yuborilgan",
                              closing_id=closing.data["closing_id"], business_date=business_date)
    pending = queries.pending_cash_handovers(ctx.docs, agent_id)
    if pending:
        raise Conflict("pending_cash_handover", "Avvalgi naqd topshirish hali hisobchi tomonidan qabul qilinmagan",
                       transfer_id=pending[0]["transfer_id"])

    cash_counted = as_int(payload, "cash_counted", minimum=0, maximum=_MAX_MONEY)
    snapshot = queries.balance_snapshot(ctx.bal, ctx.docs, agent_id, cash_counted)
    expected_cash = snapshot["agent_cash"]["expected"]
    cash_difference = cash_counted - expected_cash
    reason = as_str(payload, "reason", max_len=300, default="")
    if cash_difference and not reason:
        raise DomainError("missing_field", "Kassa farqi uchun sabab majburiy", field="reason")
    maximum_handover = min(cash_counted, expected_cash)
    cash_handover_amount = as_int(payload, "cash_handover_amount", minimum=0,
                                  maximum=maximum_handover, default=maximum_handover)

    closing_id = ctx.new_id("CL")
    transfer_id = ctx.new_id("TR") if cash_handover_amount else None
    data = {
        "closing_id": closing_id,
        "agent_id": agent_id,
        "business_date": business_date,
        "balances": snapshot,
        "cash_handover_amount": cash_handover_amount,
        "cash_handover_id": transfer_id,
        "cash_handover_status": "pending" if transfer_id else "not_required",
        "cash_difference": cash_difference,
        "reason": reason,
        "submitted_by": ctx.actor.user_id,
        "submitted_device_id": ctx.actor.device_id,
    }
    ctx.emit("closing_submitted", data)
    if transfer_id:
        ctx.emit("cash_handover_prepared", {
            "transfer_id": transfer_id,
            "transfer_type": "cash_handover",
            "closing_id": closing_id,
            "agent_id": agent_id,
            "amount": cash_handover_amount,
            "prepared_by": ctx.actor.user_id,
            "prepared_device_id": ctx.actor.device_id,
            "prepared_at": ctx.occurred_at,
        })
    ctx.notify("owner", "daily_closing_submitted", None, {
        "closing_id": closing_id,
        "agent_id": agent_id,
        "business_date": business_date,
        "cash_difference": cash_difference,
        "cash_handover_amount": cash_handover_amount,
        "cash_handover_id": transfer_id,
    })
    if cash_difference:
        ctx.notify("owner", "closing_cash_difference", None, {
            "closing_id": closing_id,
            "agent_id": agent_id,
            "business_date": business_date,
            "expected": expected_cash,
            "counted": cash_counted,
            "difference": cash_difference,
            "reason": reason,
        })
    return {
        "closing_id": closing_id,
        "business_date": business_date,
        "balances": snapshot,
        "cash_handover_id": transfer_id,
        "cash_handover_amount": cash_handover_amount,
        "cash_difference": cash_difference,
    }


@command("transfer.accept.cash_handover")
def accept_cash_handover(ctx, payload: dict) -> dict:
    _known_fields(payload, {"transfer_id"})
    transfer_id = as_str(payload, "transfer_id", max_len=60)
    transfer = ctx.docs.get(f"transfer:{transfer_id}")
    if transfer is None or transfer.data.get("transfer_type") != "cash_handover":
        raise DomainError("not_found", "Naqd topshirish hujjati topilmadi", field="transfer_id")
    if transfer.status != "pending":
        raise DomainError("transfer_not_pending", "Faqat kutilayotgan naqd topshirishni qabul qilish mumkin",
                          status=transfer.status)
    ctx.two_party(transfer.data["prepared_by"], "cash_handover")
    if ctx.actor.device_id == transfer.data["prepared_device_id"]:
        raise DomainError("same_device_both_sides", "Naqd topshirish boshqa qurilmada tasdiqlanishi kerak",
                          what="cash_handover")
    available = ctx.bal.get(keys.agent_cash(transfer.data["agent_id"]))
    if available < transfer.data["amount"]:
        raise Conflict("agent_cash_changed", "Topshirishdan oldin agent naqd qoldig'i kamaydi",
                       agent_id=transfer.data["agent_id"], available=available,
                       requested=transfer.data["amount"])
    ctx.emit("cash_handover_accepted", {
        "transfer_id": transfer_id,
        "closing_id": transfer.data["closing_id"],
        "agent_id": transfer.data["agent_id"],
        "amount": transfer.data["amount"],
    })
    return {"transfer_id": transfer_id, "agent_id": transfer.data["agent_id"],
            "amount": transfer.data["amount"]}
