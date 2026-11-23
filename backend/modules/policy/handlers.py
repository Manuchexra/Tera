"""policy.update: sozlamalarni (limit, muddat, 21:00, ...) egasi sabab bilan o'zgartiradi.

O'zgarish faqat keyingi buyruqlarga ta'sir qiladi. Eski hodisalarda hisoblangan qiymatlar (muddat, limit tekshiruvi)
hodisa ichida saqlangani uchun qayta hisoblanmaydi.
"""
from backend.core.errors import DomainError
from backend.core.policy import PolicyError
from backend.core.validate import as_str
from backend.modules.sync.registry import command

from . import projections  # noqa: F401


@command("policy.update")
def update(ctx, p):
    ctx.require_fresh()
    reason = as_str(p, "reason", max_len=300)
    changes = p.get("changes")
    current = ctx.policy
    try:
        new = current.validated_with(changes)
    except PolicyError as e:
        raise DomainError("invalid_field", str(e), field=e.field)
    cur, nxt = current.to_dict(), new.to_dict()
    effective = {k: v for k, v in changes.items() if cur[k] != nxt[k]}
    if not effective:
        raise DomainError("no_changes", "Hech narsa o'zgarmadi")
    ctx.emit("policy_changed", {"changes": effective, "previous": {k: cur[k] for k in effective}, "reason": reason})
    return {"changed": sorted(effective)}
