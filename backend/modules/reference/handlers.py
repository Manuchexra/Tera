"""reference.upsert: ma'lumotnomani yaratish va o'zgartirish. Faqat egasi, faqat onlayn.

Yaratishda ID serverda beriladi (RM-001, PM-001, P-001, S-001, SUP-001), qurilma ID o'ylab topmaydi.
O'zgartirishda faqat yuborilgan maydonlar o'zgaradi. Har o'zgarish jurnalga eski va yangi qiymat bilan yoziladi.
"""
from backend.core.errors import DomainError
from backend.core.validate import as_str
from backend.ledger.counters import next_number
from backend.modules.auth.users import UserStore
from backend.modules.sync.registry import command

from . import projections  # noqa: F401
from .rules import ensure_can_deactivate, ensure_unique_name
from .schemas import ID_PREFIX, KINDS, normalize


def _active_doc(ctx, kind: str, ref_id: str, field: str):
    d = ctx.docs.get(f"{kind}:{ref_id}")
    if d is None:
        raise DomainError("not_found", f"{ref_id} topilmadi", field=field, id=ref_id)
    if d.status != "active":
        raise DomainError("inactive_reference", f"{ref_id} faol emas", field=field, id=ref_id)
    return d


def _check_material(ctx, rid, data, existing):
    ensure_unique_name(ctx.docs, "material", data["name"], rid)


def _check_packmaterial(ctx, rid, data, existing):
    ensure_unique_name(ctx.docs, "packmaterial", data["name"], rid)


def _check_supplier(ctx, rid, data, existing):
    ensure_unique_name(ctx.docs, "supplier", data["name"], rid)
    for m in data["supplies"]:
        _active_doc(ctx, "material", m, "supplies")


def _check_product(ctx, rid, data, existing):
    raw = _active_doc(ctx, "material", data["raw_material_id"], "raw_material_id") if existing is None \
        else ctx.docs.get(f"material:{data['raw_material_id']}")
    for item in data["materials"]:
        _active_doc(ctx, "packmaterial", item["material_id"], "materials")
    if not data.get("name"):
        data["name"] = f"{raw.data['name']} {data['pack_g']} g"
    if data["active"]:
        for d in ctx.docs.query("product", ref=data["raw_material_id"], status="active"):
            if d.doc_id != f"product:{rid}" and d.data["pack_g"] == data["pack_g"]:
                raise DomainError("product_exists", "Bu xom ashyo va qadoq og'irligi uchun mahsulot allaqachon bor",
                                  existing_id=d.doc_id.split(":", 1)[1])


def _check_store(ctx, rid, data, existing):
    agent = UserStore(ctx.db).get(data["agent_id"])
    if agent is None or not agent.active or not ({"agent", "owner"} & set(agent.tasks)):
        raise DomainError("invalid_field", "Mas'ul agent faol foydalanuvchi bo'lishi va 'agent' vazifasiga ega bo'lishi kerak",
                          field="agent_id")
    if (data.get("lat_e6") is None) != (data.get("lon_e6") is None):
        raise DomainError("invalid_field", "Kenglik va uzunlik birga kiritiladi", field="lat_e6")
    chat = data.get("telegram_chat_id")
    if chat:
        for d in ctx.docs.query("store"):
            if d.doc_id != f"store:{rid}" and d.data.get("telegram_chat_id") == chat:
                raise DomainError("duplicate_telegram_chat", "Bu Telegram chat boshqa do'konga ulangan", store=d.doc_id.split(":", 1)[1])


_CHECKS = {"material": _check_material, "packmaterial": _check_packmaterial, "product": _check_product,
           "store": _check_store, "supplier": _check_supplier}


@command("reference.upsert")
def upsert(ctx, p):
    ctx.require_fresh()
    kind = as_str(p, "kind", allowed=KINDS)
    reason = as_str(p, "reason", default="", max_len=300)
    rid = p.get("id")
    existing = None
    if rid is not None:
        rid = as_str(p, "id", max_len=60)
        existing = ctx.docs.get(f"{kind}:{rid}")
        if existing is None:
            raise DomainError("not_found", f"{rid} topilmadi", id=rid)
    data, changes = normalize(kind, p.get("data"), existing.data if existing else None)
    if existing is not None and not changes:
        raise DomainError("no_changes", "Hech narsa o'zgarmadi")
    _CHECKS[kind](ctx, rid, data, existing.data if existing else None)
    if existing is not None and existing.data.get("active", True) and data["active"] is False:
        ensure_can_deactivate(ctx.bal, kind, rid)
    created = existing is None
    if created:
        rid = f"{ID_PREFIX[kind]}-{next_number(ctx.db, 'ref:' + kind):03d}"
    ctx.emit("reference_changed", {"kind": kind, "id": rid, "data": data, "created": created,
                                   "changes": changes, "reason": reason})
    return {"kind": kind, "id": rid, "created": created, "name": data.get("name")}
