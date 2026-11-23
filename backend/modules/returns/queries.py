"""Qaytim, almashtirish va write-off hujjatlarini o'qish."""


def get_return(docs, return_id: str) -> dict | None:
    doc = docs.get(f"return:{return_id}")
    return {"id": return_id, "status": doc.status, **doc.data} if doc else None


def list_returns(docs, sale_id: str | None = None, status: str | None = None) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("return", ref=sale_id, status=status)]


def get_exchange(docs, exchange_id: str) -> dict | None:
    doc = docs.get(f"exchange:{exchange_id}")
    return {"id": exchange_id, "status": doc.status, **doc.data} if doc else None


def list_writeoffs(docs, product_id: str | None = None) -> list[dict]:
    return [{"id": doc.doc_id.split(":", 1)[1], "status": doc.status, **doc.data}
            for doc in docs.query("writeoff", ref=product_id)]
