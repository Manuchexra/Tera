from backend.balances.projection import projects


@projects("reference_changed")
def _reference_changed(ev, bal, docs):
    """`data` — o'zgarishdan keyingi TO'LIQ holat; kim, qachon, nima o'zgargani esa hodisaning o'zida (changes, reason)."""
    p = ev.payload
    kind, rid, data = p["kind"], p["id"], p["data"]
    ref = data.get("agent_id") if kind == "store" else data.get("raw_material_id") if kind == "product" else None
    docs.put(f"{kind}:{rid}", kind, ref, "active" if data.get("active", True) else "inactive", data)
