"""Egasining bir martalik ruxsatlari (talablar §10.3, §3.3).

* discount          - chegirma (agent chegirma bera olmaydi: egasi aniq do'kon uchun foiz yoki summa chegarasi bilan ruxsat beradi)
* credit_exception  - do'kon limitdan oshib ketganda aniq summagacha istisno
* labor             - qadoqlovchining ish haqi summasi (qadoqlovchi o'zi belgilamaydi)
Ruxsat bir marta ishlatiladi, muddati bor (standart 24 soat, ko'pi bilan 7 kun), bekor qilinishi mumkin.
"""
from datetime import timedelta

from backend.core.errors import DomainError
from backend.core.timing import fmt_ts, parse_ts
from backend.core.validate import as_int, as_str
from backend.modules.auth.users import UserStore
from backend.modules.sync.registry import command

from . import projections  # noqa: F401

KINDS = ("discount", "credit_exception", "labor")
MAX_HOURS = 168


@command("approval.grant")
def grant(ctx, p):
    ctx.require_fresh()
    kind = as_str(p, "kind", allowed=KINDS)
    reason = as_str(p, "reason", max_len=300)
    hours = as_int(p, "expires_in_hours", minimum=1, maximum=MAX_HOURS, default=24)
    store_id = p.get("store_id")
    for_user = p.get("for_user_id")
    max_amount, max_percent = p.get("max_amount"), p.get("max_percent")

    if kind == "discount":
        if (max_amount is None) == (max_percent is None):
            raise DomainError("invalid_field", "Chegirma uchun max_percent yoki max_amount dan faqat bittasi kerak")
    elif max_percent is not None:
        raise DomainError("invalid_field", "max_percent faqat chegirma uchun", field="max_percent")
    if kind in ("credit_exception", "labor") and max_amount is None:
        raise DomainError("missing_field", "max_amount kiritilmagan", field="max_amount")
    if max_amount is not None:
        max_amount = as_int(p, "max_amount", minimum=1, maximum=1_000_000_000)
    if max_percent is not None:
        max_percent = as_int(p, "max_percent", minimum=1, maximum=100)

    if kind in ("discount", "credit_exception"):
        if not store_id:
            raise DomainError("missing_field", "store_id kiritilmagan", field="store_id")
    elif store_id is not None:
        raise DomainError("invalid_field", "Ishchi haqi ruxsati do'konga bog'lanmaydi", field="store_id")
    if store_id is not None:
        st = ctx.docs.get(f"store:{store_id}")
        if st is None or st.status != "active":
            raise DomainError("not_found", "Faol do'kon topilmadi", field="store_id")
    if for_user is not None:
        u = UserStore(ctx.db).get(for_user)
        if u is None or not u.active:
            raise DomainError("not_found", "Faol xodim topilmadi", field="for_user_id")

    approval_id = ctx.new_id("AP")
    expires_at = fmt_ts(parse_ts(ctx.occurred_at) + timedelta(hours=hours))
    ctx.emit("approval_granted", {"approval_id": approval_id, "kind": kind, "store_id": store_id,
                                  "for_user_id": for_user, "max_amount": max_amount, "max_percent": max_percent,
                                  "reason": reason, "expires_at": expires_at})
    return {"approval_id": approval_id, "expires_at": expires_at}


@command("approval.revoke", action="approval.grant")
def revoke(ctx, p):
    ctx.require_fresh()
    aid = as_str(p, "approval_id", max_len=60)
    reason = as_str(p, "reason", max_len=300)
    d = ctx.docs.get(f"approval:{aid}")
    if d is None:
        raise DomainError("not_found", "Ruxsat topilmadi")
    if d.status != "open":
        raise DomainError("approval_not_open", "Faqat ochiq ruxsatni bekor qilish mumkin", status=d.status)
    ctx.emit("approval_revoked", {"approval_id": aid, "reason": reason})
    return {"approval_id": aid}
