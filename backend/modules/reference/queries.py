"""Ma'lumotnomani o'qish: bitta yozuv, ro'yxat va mobil ilova uchun to'liq snapshot (offline ishlash uchun)."""
import json

from backend.core.policy import Policy

from .schemas import KINDS


# Snapshotga ta'sir qiladigan hodisa turlari. Sotuv, to'lov kabi hodisalar snapshotni eskirtirmaydi.
REFERENCE_EVENT_TYPES = ("reference_changed", "price_changed", "policy_changed")


def reference_version(journal) -> int:
    """Ma'lumotnoma/narx/sozlama oxirgi marta o'zgargan hodisaning tartib raqami. Qurilma shu bilan
    o'zida yangi ma'lumot bor-yo'qligini bilib oladi."""
    return journal.ledger.last_seq_of(REFERENCE_EVENT_TYPES)


def get(docs, kind: str, ref_id: str) -> dict | None:
    d = docs.get(f"{kind}:{ref_id}")
    return {"id": ref_id, "status": d.status, **d.data} if d else None


def list_all(docs, kind: str, active_only: bool = True) -> list[dict]:
    out = []
    for d in docs.query(kind, status="active" if active_only else None):
        out.append({"id": d.doc_id.split(":", 1)[1], "status": d.status, **d.data})
    return out


def snapshot(journal, policy: Policy, agent_id: str | None = None) -> dict:
    """Qurilma offline ishlashi uchun kerakli barcha ma'lumotnoma, narxlar va sozlamalar.

    `version` — `reference_version`: faqat ma'lumotnoma/narx/sozlama o'zgarganda oshadi.
    Agent uchun faqat o'ziga biriktirilgan do'konlar qaytadi."""
    docs = journal.docs
    prices = {d.doc_id.split(":", 1)[1]: d.data["price"] for d in docs.query("price")}
    products = []
    for item in list_all(docs, "product"):
        products.append({**item, "price": prices.get(item["id"])})
    stores = list_all(docs, "store")
    if agent_id is not None:
        stores = [s for s in stores if s["agent_id"] == agent_id]
    return {
        "version": reference_version(journal),
        "policy": policy.to_dict(),
        "materials": list_all(docs, "material"),
        "packmaterials": list_all(docs, "packmaterial"),
        "products": products,
        "stores": stores,
        "suppliers": list_all(docs, "supplier"),
    }


def cached_snapshot(backend, journal, policy: Policy, agent_id: str | None = None, ttl_seconds: int = 300) -> dict:
    """Redis keshi bilan snapshot. Kesh versiya bo'yicha tekshiriladi: ma'lumotnoma o'zgargan zahoti yaroqsiz bo'ladi,
    shuning uchun eskirgan narx hech qachon qaytmaydi. Redis ishlamasa, to'g'ridan-to'g'ri hisoblanadi."""
    key = f"snapshot:{agent_id or 'all'}"
    raw = backend.get(key)
    if raw:
        try:
            cached = json.loads(raw)
            if cached.get("version") == reference_version(journal):
                return cached
        except ValueError:
            pass
    data = snapshot(journal, policy, agent_id)
    backend.set(key, json.dumps(data, ensure_ascii=False), ttl_seconds)
    return data
