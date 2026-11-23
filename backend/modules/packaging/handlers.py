"""Qadoqlash partiyasi: tekshiruv, o'rtacha tannarx, sarf va yaroqlilik."""
from datetime import timedelta
from decimal import Decimal

from backend.balances import keys
from backend.core.errors import DomainError
from backend.core.money import allocate_proportionally, pct, round_uzs
from backend.core.timing import local_date
from backend.core.validate import as_int, as_list, as_str
from backend.modules.approvals.projections import mark_approval_used
from backend.modules.approvals.rules import check_approval
from backend.modules.auth.users import UserStore
from backend.modules.sync.registry import command

from . import queries

_MAX_WEIGHT_G = 100_000_000
_MAX_PACKAGES = 100_000_000
_MAX_LINES = 50
_MAX_LABOR = 1_000_000_000
_MAX_DURATION_MINUTES = 10_080


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


def _workers(ctx, payload: dict) -> list[str]:
    worker_ids = as_list(payload, "workers", min_len=1)
    if any(not isinstance(user_id, str) or not user_id.strip() for user_id in worker_ids):
        raise DomainError("invalid_field", "'workers' faol xodim ID lari ro'yxati bo'lishi kerak",
                          field="workers")
    if len(set(worker_ids)) != len(worker_ids):
        raise DomainError("invalid_field", "'workers' ichida takroriy xodim bo'lmasligi kerak",
                          field="workers")
    users = UserStore(ctx.db)
    for user_id in worker_ids:
        worker = users.get(user_id)
        if worker is None or not worker.active:
            raise DomainError("not_found", "Faol xodim topilmadi", field="workers", user_id=user_id)
    return worker_ids


def _raw_lot_allocations(ctx, material_id: str, weight_g: int) -> list[dict]:
    lots = [doc for doc in ctx.docs.query("raw_lot", ref=material_id)
            if doc.data["remaining_g"] > 0]
    lots.sort(key=lambda doc: (doc.data["purchased_at"], doc.data["purchase_id"]))
    available = sum(doc.data["remaining_g"] for doc in lots)
    if available < weight_g:
        raise DomainError("stock_ledger_mismatch", "Xom ashyo qoldig'iga mos xarid partiyasi topilmadi",
                          material_id=material_id, requested_g=weight_g, lot_stock_g=available)
    remaining = weight_g
    allocations = []
    for lot in lots:
        take = min(remaining, lot.data["remaining_g"])
        if take:
            allocations.append({"purchase_id": lot.data["purchase_id"], "weight_g": take})
            remaining -= take
        if remaining == 0:
            break
    return allocations


def _labor_total(payload: dict, package_count: int) -> tuple[int, str]:
    has_total = "labor_cost" in payload
    has_per_unit = "labor_cost_per_unit" in payload
    if has_total == has_per_unit:
        code = "missing_field" if not has_total else "invalid_field"
        raise DomainError(code, "Jami ish haqi yoki 1 dona uchun ish haqi — faqat bittasi kerak",
                          field="labor_cost")
    if has_total:
        return as_int(payload, "labor_cost", minimum=0, maximum=_MAX_LABOR), "total"
    per_unit = as_int(payload, "labor_cost_per_unit", minimum=0, maximum=_MAX_LABOR)
    return per_unit * package_count, "per_unit"


def _line_items(ctx, raw_material_id: str, payload: dict) -> tuple[list[dict], int, int]:
    raw_lines = as_list(payload, "lines", min_len=1)
    if len(raw_lines) > _MAX_LINES:
        raise DomainError("invalid_field", f"'lines' ko'pi bilan {_MAX_LINES} ta bo'lishi mumkin",
                          field="lines")
    lines, seen = [], set()
    package_count = output_weight_g = 0
    for index, item in enumerate(raw_lines):
        if not isinstance(item, dict) or set(item) != {"product_id", "quantity"}:
            raise DomainError("invalid_field", "Har bir qator {product_id, quantity} bo'lishi kerak",
                              field="lines", index=index)
        product_id = as_str(item, "product_id", max_len=60)
        if product_id in seen:
            raise DomainError("invalid_field", "Bir mahsulot ikki marta ko'rsatilgan",
                              field="lines", product_id=product_id)
        seen.add(product_id)
        quantity = as_int(item, "quantity", minimum=1, maximum=_MAX_PACKAGES)
        product = _active_doc(ctx, "product", product_id, "lines")
        if product.data["raw_material_id"] != raw_material_id:
            raise DomainError("product_raw_material_mismatch",
                              "Partiyadagi barcha mahsulotlar bir xil xom ashyodan bo'lishi kerak",
                              field="lines", product_id=product_id, material_id=raw_material_id)
        materials = product.data["materials"]
        for material in materials:
            _active_doc(ctx, "packmaterial", material["material_id"], "materials")
        pack_g = product.data["pack_g"]
        line_weight = quantity * pack_g
        package_count += quantity
        output_weight_g += line_weight
        if package_count > _MAX_PACKAGES or output_weight_g > _MAX_WEIGHT_G:
            raise DomainError("invalid_field", "Jami qadoq soni yoki og'irligi ruxsat etilgan chegaradan oshdi",
                              field="lines")
        lines.append({
            "product_id": product_id,
            "quantity": quantity,
            "pack_g": pack_g,
            "output_weight_g": line_weight,
            "materials": materials,
            "material_usage": {},
            "raw_material_cost": 0,
            "packaging_material_cost": 0,
            "labor_cost": 0,
        })
    return lines, package_count, output_weight_g


@command("packaging.post")
def post(ctx, payload: dict) -> dict:
    _known_fields(payload, {
        "material_id", "input_g", "lines", "waste_g", "workers", "duration_minutes",
        "labor_cost", "labor_cost_per_unit", "shelf_life_days", "approval_id",
    })
    material_id = as_str(payload, "material_id", max_len=60)
    raw_material = _active_doc(ctx, "material", material_id, "material_id")
    input_g = as_int(payload, "input_g", minimum=1, maximum=_MAX_WEIGHT_G)
    lines, package_count, output_weight_g = _line_items(ctx, material_id, payload)
    if output_weight_g > input_g:
        raise DomainError("output_exceeds_input", "Tayyor mahsulot og'irligi xom ashyo kirimidan oshmasligi kerak",
                          input_g=input_g, output_g=output_weight_g)
    shrink_g = input_g - output_weight_g
    waste_g = as_int(payload, "waste_g", minimum=0, maximum=_MAX_WEIGHT_G, default=0)
    if waste_g > shrink_g:
        raise DomainError("waste_exceeds_shrink", "O'lchangan chiqindi jami kam chiqishdan oshib ketdi",
                          waste_g=waste_g, shrink_g=shrink_g)
    workers = _workers(ctx, payload)
    labor_total, labor_basis = _labor_total(payload, package_count)

    approval_id = None
    if ctx.actor.task == "packer" or payload.get("approval_id") is not None:
        approval_id = as_str(payload, "approval_id", max_len=60)
        check_approval(ctx.docs, approval_id, kind="labor", now=ctx.occurred_at,
                       user_id=ctx.actor.user_id, amount=labor_total)

    duration_minutes = None
    if payload.get("duration_minutes") is not None:
        duration_minutes = as_int(payload, "duration_minutes", minimum=1, maximum=_MAX_DURATION_MINUTES)

    default_shelf_life = raw_material.data.get("shelf_life_days") or ctx.policy.default_shelf_life_days
    shelf_life_days = as_int(payload, "shelf_life_days", minimum=1, maximum=1095,
                              default=default_shelf_life)
    expires_on = (local_date(ctx.occurred_at) + timedelta(days=shelf_life_days)).isoformat()

    available_raw_g = ctx.bal.get(keys.raw(material_id))
    if available_raw_g < input_g:
        raise DomainError("insufficient_raw_stock", "Xom ashyo qoldig'i yetarli emas",
                          material_id=material_id, available_g=available_raw_g, requested_g=input_g)
    raw_value = ctx.bal.get(keys.raw_value(material_id))
    raw_cost_total = round_uzs(Decimal(raw_value) * input_g / available_raw_g)
    raw_lot_allocations = _raw_lot_allocations(ctx, material_id, input_g)

    total_material_usage: dict[str, int] = {}
    for line in lines:
        for bom_item in line["materials"]:
            mid = bom_item["material_id"]
            used = line["quantity"] * bom_item["qty"]
            line["material_usage"][mid] = used
            total_material_usage[mid] = total_material_usage.get(mid, 0) + used

    material_consumption = []
    for packmaterial_id in sorted(total_material_usage):
        used = total_material_usage[packmaterial_id]
        available = ctx.bal.get(keys.pack(packmaterial_id))
        if available < used:
            raise DomainError("insufficient_packaging_stock", "Qadoq materiali qoldig'i yetarli emas",
                              material_id=packmaterial_id, available=available, requested=used)
        value = ctx.bal.get(keys.pack_value(packmaterial_id))
        consumed_value = round_uzs(Decimal(value) * used / available)
        shares = allocate_proportionally(
            consumed_value, [line["material_usage"].get(packmaterial_id, 0) for line in lines])
        for index, share in enumerate(shares):
            lines[index]["packaging_material_cost"] += share
        material_consumption.append({
            "material_id": packmaterial_id,
            "quantity": used,
            "inventory_value": consumed_value,
        })

    raw_shares = allocate_proportionally(raw_cost_total, [line["output_weight_g"] for line in lines])
    labor_shares = allocate_proportionally(labor_total, [line["quantity"] for line in lines])
    for index, line in enumerate(lines):
        line["raw_material_cost"] = raw_shares[index]
        line["labor_cost"] = labor_shares[index]
        line["production_cost"] = (line["raw_material_cost"] + line["packaging_material_cost"]
                                   + line["labor_cost"])
        line["unit_cost"] = round_uzs(Decimal(line["production_cost"]) / line["quantity"])
        line.pop("material_usage")

    historical = queries.shrink_summary(ctx.docs, material_id, ctx.policy)
    batch_id = ctx.new_id("BAT")
    data = {
        "batch_id": batch_id,
        "material_id": material_id,
        "input_g": input_g,
        "output_weight_g": output_weight_g,
        "package_count": package_count,
        "shrink_g": shrink_g,
        "shrink_pct": str(pct(shrink_g, input_g)),
        "waste_g": waste_g,
        "unclassified_loss_g": shrink_g - waste_g,
        "workers": workers,
        "duration_minutes": duration_minutes,
        "labor_cost_basis": labor_basis,
        "labor_cost_total": labor_total,
        "shelf_life_days": shelf_life_days,
        "expires_on": expires_on,
        "source_purchase_ids": [allocation["purchase_id"] for allocation in raw_lot_allocations],
        "raw_lot_allocations": raw_lot_allocations,
        "raw_cost_total": raw_cost_total,
        "material_consumption": material_consumption,
        "packaging_material_cost_total": sum(item["inventory_value"] for item in material_consumption),
        "production_cost_total": (raw_cost_total + sum(item["inventory_value"] for item in material_consumption)
                                  + labor_total),
        "approval_id": approval_id,
        "lines": lines,
    }
    ctx.emit("packaging_recorded", data)

    if queries.is_shrink_outlier(shrink_g, input_g, historical, ctx.policy.shrink_deviation_pct):
        ctx.notify("owner", "shrink_deviation", None, {
            "batch_id": batch_id,
            "material_id": material_id,
            "shrink_g": shrink_g,
            "input_g": input_g,
            "shrink_pct": data["shrink_pct"],
            "historical_average_pct": historical["weighted_average_pct"],
            "deviation_pct": ctx.policy.shrink_deviation_pct,
        })
    return {
        "batch_id": batch_id,
        "output_weight_g": output_weight_g,
        "package_count": package_count,
        "shrink_g": shrink_g,
        "shrink_pct": data["shrink_pct"],
        "expires_on": expires_on,
        "production_cost_total": data["production_cost_total"],
        "lines": lines,
    }
