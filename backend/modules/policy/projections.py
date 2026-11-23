from backend.balances.projection import projects


@projects("policy_changed")
def _policy_changed(ev, bal, docs):
    doc = docs.get("policy:global")
    data = dict(doc.data) if doc else {}
    data.update(ev.payload["changes"])
    docs.put("policy:global", "policy", None, "active", data)
