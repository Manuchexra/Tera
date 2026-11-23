"""Keshni jurnaldan qayta qurish va solishtirish (talablar §2.2: qoldiq hisoblangan, jurnaldan tiklanadi)."""
from dataclasses import dataclass, field

from backend.ledger.db import Database
from backend.ledger.events import canonical
from backend.ledger.store import ChainReport, Ledger

from .projection import apply_event
from .store import Balances, Docs, MemBalances, MemDocs


@dataclass
class RebuildReport:
    chain: ChainReport
    events: int
    balance_diffs: list[tuple] = field(default_factory=list)   # (kalit, keshda, jurnaldan)
    doc_diffs: list[tuple] = field(default_factory=list)       # (doc_id, keshda, jurnaldan)

    @property
    def ok(self) -> bool:
        return self.chain.ok and not self.balance_diffs and not self.doc_diffs


def rebuild(ledger: Ledger) -> tuple[MemBalances, MemDocs, int]:
    bal, docs, n = MemBalances(), MemDocs(), 0
    for ev in ledger.iter_events():
        apply_event(ev, bal, docs)
        n += 1
    return bal, docs, n


def verify(db: Database) -> RebuildReport:
    """Jurnalni qayta o'qib, keshdagi qoldiq va hujjatlar bilan solishtiradi. Hech narsani o'zgartirmaydi."""
    ledger = Ledger(db)
    chain = ledger.verify_chain()
    mem_bal, mem_docs, n = rebuild(ledger)
    rep = RebuildReport(chain=chain, events=n)
    cur_b, new_b = Balances(db).snapshot(), mem_bal.snapshot()
    for k in sorted(set(cur_b) | set(new_b)):
        if cur_b.get(k, 0) != new_b.get(k, 0):
            rep.balance_diffs.append((k, cur_b.get(k, 0), new_b.get(k, 0)))
    cur_d, new_d = Docs(db).snapshot(), mem_docs.snapshot()
    for k in sorted(set(cur_d) | set(new_d)):
        if cur_d.get(k) != new_d.get(k):
            rep.doc_diffs.append((k, cur_d.get(k), new_d.get(k)))
    return rep


def repair(db: Database) -> RebuildReport:
    """Keshni jurnal bo'yicha to'liq almashtiradi. Faqat zanjir butun bo'lsa ishlaydi: buzilgan jurnaldan tiklab bo'lmaydi."""
    ledger = Ledger(db)
    chain = ledger.verify_chain()
    if not chain.ok:
        raise RuntimeError(f"jurnal zanjiri buzilgan (seq={chain.broken_seq}): {chain.reason}")
    mem_bal, mem_docs, n = rebuild(ledger)
    with db.transaction():
        db.execute("DELETE FROM balances")
        db.execute("DELETE FROM docs")
        for k, v in mem_bal.snapshot().items():
            db.execute("INSERT INTO balances (key, value) VALUES (?, ?)", (k, v))
        for doc_id, (kind, ref, status, data) in mem_docs.snapshot().items():
            db.execute("INSERT INTO docs (doc_id, kind, ref, status, data) VALUES (?,?,?,?,?)",
                       (doc_id, kind, ref, status, data))
    return verify(db)
