from backend.balances.projection import projects


@projects("approval_granted")
def _granted(ev, bal, docs):
    p = ev.payload
    docs.put(f"approval:{p['approval_id']}", "approval", p.get("store_id"), "open", {
        "approval_id": p["approval_id"], "kind": p["kind"], "store_id": p.get("store_id"),
        "for_user_id": p.get("for_user_id"), "max_amount": p.get("max_amount"), "max_percent": p.get("max_percent"),
        "reason": p["reason"], "granted_by": ev.actor_id, "granted_at": ev.occurred_at, "expires_at": p["expires_at"],
        "used_by_event": None, "used_at": None})


@projects("approval_revoked")
def _revoked(ev, bal, docs):
    docs.update(f"approval:{ev.payload['approval_id']}", status="revoked", revoked_by=ev.actor_id,
                revoked_at=ev.occurred_at, revoke_reason=ev.payload["reason"])


def mark_approval_used(docs, approval_id: str, event_id: str, at: str) -> None:
    """Ruxsatni ishlatgan hodisaning projeksiyasi chaqiradi (sotuv, qadoqlash). Ruxsat bir martalik."""
    docs.update(f"approval:{approval_id}", status="used", used_by_event=event_id, used_at=at)
