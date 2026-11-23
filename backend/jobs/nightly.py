"""Jurnaldan qoldiqlarni tekshiradigan, kechasi cron/Task Scheduler'dan chaqiriladigan vazifa."""
import json
import os
from datetime import timedelta

from backend.balances.rebuild import verify
from backend.core.clock import SystemClock
from backend.core.timing import local_date
from backend.ledger.db import DatabaseProvider
from backend.ledger.journal import Journal
from backend.modules import load_all
from backend.modules.reporting import queries
from backend.modules.sync.outbox import Outbox


def run_nightly_audit(db, now: str) -> dict:
    load_all()
    audit = verify(db)
    journal = Journal(db)
    today = local_date(now).isoformat()
    overdue = queries.debt_report(journal.bal, journal.docs, today)
    yesterday = (local_date(now) - timedelta(days=1)).isoformat()
    expired = queries.expiring_inventory(journal.bal, journal.docs, "0001-01-01", yesterday)
    overdue_disputes = [
        {"dispute_id": doc.data["dispute_id"], "due_date": doc.data["resolution_due"]}
        for doc in journal.docs.query("dispute", status="open")
        if doc.data["resolution_due"] < today
    ]
    open_returns = [
        {"return_id": doc.data["return_id"], "declared_at": doc.data["declared_at"]}
        for doc in journal.docs.query("return", status="pending")
        if local_date(doc.data["declared_at"]) < local_date(now) - timedelta(days=1)
    ]

    issues = {}
    if not audit.chain.ok:
        issues["ledger_chain"] = {"broken_seq": audit.chain.broken_seq, "reason": audit.chain.reason}
    if audit.balance_diffs:
        issues["balance_diffs"] = audit.balance_diffs[:100]
    if audit.doc_diffs:
        issues["doc_diffs"] = audit.doc_diffs[:100]
    if overdue["overdue_total"]:
        issues["overdue_debt"] = {
            "total": overdue["overdue_total"],
            "stores": [store for store in overdue["stores"] if store["overdue_debt"]],
        }
    if expired:
        issues["expired_stock"] = expired[:100]
    if overdue_disputes:
        issues["overdue_disputes"] = overdue_disputes[:100]
    if open_returns:
        issues["unaccepted_returns"] = open_returns[:100]

    result = {"ok": not issues, "date": today, "events": audit.events, "issues": issues}
    if issues:
        with db.transaction():
            Outbox(db).add("owner", "nightly_audit_alert", None, result, now)
    return result


def main() -> None:
    database_url = os.environ.get("TERA_DATABASE_URL")
    if not database_url:
        raise RuntimeError("TERA_DATABASE_URL o'rnatilmagan")
    provider = DatabaseProvider(database_url)
    try:
        with provider.session() as db:
            result = run_nightly_audit(db, SystemClock().now())
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if not result["ok"]:
            raise SystemExit(1)
    finally:
        provider.close()


if __name__ == "__main__":
    main()
