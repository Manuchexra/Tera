"""Ruxsatni tekshirish. Ishlatuvchi buyruq (sotuv, qadoqlash) `check_approval` ni chaqiradi, so'ng o'z hodisasida
`approval_ids` ni yozadi, projeksiya esa `mark_approval_used` bilan ruxsatni yopadi."""
from backend.core.errors import DomainError


def check_approval(docs, approval_id: str, *, kind: str, now: str, user_id: str | None = None,
                   store_id: str | None = None, amount: int | None = None, percent: int | None = None):
    d = docs.get(f"approval:{approval_id}")
    if d is None:
        raise DomainError("approval_not_found", "Egasining ruxsati topilmadi", approval_id=approval_id)
    a = d.data
    if a["kind"] != kind:
        raise DomainError("approval_wrong_kind", "Ruxsat boshqa amal uchun berilgan", approval_id=approval_id,
                          expected=kind, actual=a["kind"])
    if d.status == "used":
        raise DomainError("approval_used", "Bu ruxsat allaqachon ishlatilgan", approval_id=approval_id)
    if d.status == "revoked":
        raise DomainError("approval_revoked", "Bu ruxsat bekor qilingan", approval_id=approval_id)
    if now > a["expires_at"]:
        raise DomainError("approval_expired", "Ruxsat muddati tugagan", approval_id=approval_id, expires_at=a["expires_at"])
    if a["for_user_id"] and a["for_user_id"] != user_id:
        raise DomainError("approval_wrong_user", "Ruxsat boshqa xodim uchun berilgan", approval_id=approval_id)
    if a["store_id"] and a["store_id"] != store_id:
        raise DomainError("approval_wrong_store", "Ruxsat boshqa do'kon uchun berilgan", approval_id=approval_id)
    if amount is not None and a["max_amount"] is not None and amount > a["max_amount"]:
        raise DomainError("approval_amount_exceeded", "Summa ruxsat etilganidan oshib ketdi",
                          approval_id=approval_id, max_amount=a["max_amount"], amount=amount)
    if percent is not None and a["max_percent"] is not None and percent > a["max_percent"]:
        raise DomainError("approval_amount_exceeded", "Foiz ruxsat etilganidan oshib ketdi",
                          approval_id=approval_id, max_percent=a["max_percent"], percent=percent)
    return d
