import io
import json
import tempfile
import unittest
from pathlib import Path
import urllib.error
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.cache.memory import MemoryBackend
from backend.core.clock import FixedClock
from backend.core.config import Settings
from backend.core.timing import local_date
from backend.ledger.db import DatabaseProvider
from backend.ledger.journal import Journal
from backend.modules.auth.service import AuthService
from backend.modules.auth.tokens import TokenService
from backend.modules.reporting import queries as reports
from backend.modules.sync.relay import relay_outbox
from backend.jobs.nightly import run_nightly_audit
from backend.tests.helpers import AppCase, SECRET, T0
from backend.api.main import create_app
from telegram_bot.worker import (
    TelegramDeliveryError,
    format_notification,
    fetch_updates,
    handle_updates,
    process_owner_messages,
    send_message,
)


class Stage10ApiTests(unittest.TestCase):
    def test_figma_api_routes_are_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "figma-api.sqlite3").as_posix()
            provider = DatabaseProvider(f"sqlite:///{path}")
            settings = Settings(f"sqlite:///{path}", SECRET)
            try:
                with TestClient(create_app(settings, provider)) as client:
                    schema = client.get("/openapi.json").json()["paths"]
                    expected = [
                        "/api/references/{kind}",
                        "/api/references/{kind}/{reference_id}",
                        "/api/sales",
                        "/api/sales/{sale_id}",
                        "/api/purchases",
                        "/api/purchases/{purchase_id}",
                        "/api/batches",
                        "/api/batches/{batch_id}",
                        "/api/warehouse/loadings",
                        "/api/warehouse/stock-counts",
                        "/api/returns",
                        "/api/disputes",
                        "/api/closings",
                        "/api/inbox",
                        "/api/inbox/{event_id}/resolve",
                        "/api/payments",
                        "/api/payments/{payment_id}",
                        "/api/disputes/{dispute_id}",
                        "/api/suppliers/{supplier_id}/payments",
                        "/api/prices/{product_id}/history",
                        "/api/batches/{batch_id}/shrink",
                        "/api/warehouse/custody",
                        "/api/returns/{return_id}",
                        "/api/exchanges/{exchange_id}",
                        "/api/writeoffs",
                        "/api/closings/{closing_id}",
                        "/api/cash-handovers",
                        "/api/cash-handovers/{transfer_id}",
                        "/api/closings/{agent_id}/balance-preview",
                        "/api/policy",
                        "/api/auth/devices",
                        "/api/events",
                        "/api/events/{event_id}",
                    ]
                    missing = [route for route in expected if route not in schema]
                    self.assertEqual(missing, [], missing)
            finally:
                provider.close()

    def test_owner_user_management_and_cors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "auth-api.sqlite3").as_posix()
            provider = DatabaseProvider(f"sqlite:///{path}")
            settings = Settings(
                f"sqlite:///{path}",
                SECRET,
                allowed_origins=("http://localhost:5173",),
            )
            try:
                with TestClient(create_app(settings, provider)) as client:
                    bootstrap = client.post("/api/auth/bootstrap", json={
                        "login": "owner",
                        "password": "password123",
                        "full_name": "Tizim egasi",
                    })
                    self.assertEqual(bootstrap.status_code, 200, bootstrap.text)

                    login = client.post("/api/auth/login", json={
                        "login": "owner",
                        "password": "password123",
                        "device_id": "owner-device",
                    })
                    self.assertEqual(login.status_code, 200, login.text)
                    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

                    created = client.post("/api/auth/users", headers=headers, json={
                        "login": "agent1",
                        "password": "password123",
                        "full_name": "Agent bir",
                        "tasks": ["agent"],
                    })
                    self.assertEqual(created.status_code, 200, created.text)
                    user_id = created.json()["user_id"]

                    listed = client.get("/api/auth/users", headers=headers)
                    self.assertEqual(listed.status_code, 200, listed.text)
                    self.assertEqual(len(listed.json()["users"]), 2)

                    changed = client.patch(
                        f"/api/auth/users/{user_id}/active",
                        headers=headers,
                        json={"active": False},
                    )
                    self.assertEqual(changed.status_code, 200, changed.text)
                    self.assertFalse(changed.json()["active"])

                    cors = client.get(
                        "/health",
                        headers={"Origin": "http://localhost:5173"},
                    )
                    self.assertEqual(cors.headers["access-control-allow-origin"],
                                     "http://localhost:5173")
            finally:
                provider.close()

    def test_owner_can_login_and_read_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "api.sqlite3").as_posix()
            url = f"sqlite:///{path}"
            provider = DatabaseProvider(url)
            clock = FixedClock(T0)
            with provider.session() as db:
                auth = AuthService(Journal(db, clock), TokenService(SECRET, clock))
                auth.bootstrap_owner("owner", "password123", "Tizim egasi")

            try:
                with TestClient(create_app(Settings(url, SECRET), provider)) as client:
                    login = client.post("/api/auth/login", json={
                        "login": "owner",
                        "password": "password123",
                        "device_id": "test-device",
                    })
                    self.assertEqual(login.status_code, 200, login.text)
                    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
                    self.assertEqual(client.get("/health").json(), {"status": "ok"})
                    self.assertEqual(client.get("/api/reports/debts", headers=headers).status_code, 200)
                    audit = client.get("/api/reports/audit", headers=headers)
                    self.assertEqual(audit.status_code, 200, audit.text)
                    self.assertTrue(audit.json()["ok"])
                    schema = client.get("/openapi.json").json()
                    expected_access = [
                        ("/health", "get", "For everybody"),
                        ("/api/auth/login", "post", "For everybody"),
                        ("/api/auth/switch-task", "post", "For everybody (valid token)"),
                        ("/api/commands", "post", "For everybody (valid token)"),
                        ("/api/commands/batch", "post", "For everybody (valid token)"),
                        ("/api/reference/snapshot", "get", "For everybody (valid token)"),
                        ("/api/reports/sales", "get", "Allowed: owner, accountant, agent"),
                        ("/api/reports/debts", "get", "Allowed: owner, accountant, agent"),
                        ("/api/reports/inventory", "get", "Allowed: owner, warehouse, agent"),
                        ("/api/reports/expiring", "get", "Allowed: owner, warehouse"),
                        ("/api/reports/audit", "get", "Allowed: owner"),
                    ]
                    for path, method, access in expected_access:
                        summary = schema["paths"][path][method]["summary"]
                        self.assertTrue(summary.startswith(access), f"{path}: {summary}")
            finally:
                provider.close()


class Stage10JobTests(AppCase):
    def test_nightly_audit_alerts_overdue_debt_without_repairing(self):
        seed = self.seed()
        self.ok("purchase.raw", {
            "material_id": seed["raw"], "weight_g": 1_000, "unit_price_per_kg": 100_000,
            "supplier_id": seed["supplier"], "payment_type": "credit", "paid_amount": 0,
        })
        self.ok("purchase.packaging", {
            "material_id": seed["pm"], "quantity": 20, "unit_price": 100,
            "supplier_id": seed["supplier"], "payment_type": "credit",
        })
        self.ok("price.set", {"product_id": seed["product"], "price": 8_000, "reason": "narx"})
        self.ok("packaging.post", {
            "material_id": seed["raw"], "input_g": 500,
            "lines": [{"product_id": seed["product"], "quantity": 10}],
            "workers": [self.agent_user.user_id], "labor_cost": 0,
        })
        transfer_id = self.ok("transfer.prepare.loading", {
            "agent_id": self.agent_user.user_id,
            "lines": [{"product_id": seed["product"], "quantity": 10}],
        }).response["transfer_id"]
        self.ok("transfer.accept.loading", {"transfer_id": transfer_id}, actor=self.agent)
        sale_id = self.ok("sale.create", {
            "store_id": seed["store"],
            "lines": [{"product_id": seed["product"], "quantity": 1}],
            "payment_type": "credit",
        }, actor=self.agent).response["sale_id"]

        sale = self.j.docs.get(f"sale:{sale_id}")
        report_date = local_date(sale.data["sold_at"]).isoformat()
        sales = reports.sales_summary(self.j.docs, report_date, report_date)
        debts = reports.debt_report(self.j.bal, self.j.docs, "2028-01-01")
        self.assertEqual(sales["totals"]["sale_count"], 1)
        self.assertEqual(sales["totals"]["outstanding"], sale.data["outstanding"])
        self.assertEqual(debts["debt_total"], sale.data["outstanding"])
        self.assertEqual(reports.debt_report(
            self.j.bal, self.j.docs, "2028-01-01", {"unassigned-store"},
        )["debt_total"], 0)

        before_balances = self.j.bal.snapshot()
        before_docs = self.j.docs.snapshot()
        result = run_nightly_audit(self.env.db, "2028-01-01T00:00:00.000Z")

        self.assertFalse(result["ok"])
        self.assertIn("overdue_debt", result["issues"])
        self.assertIn("expired_stock", result["issues"])
        self.assertTrue(any(message["kind"] == "nightly_audit_alert"
                            for message in self.e.outbox.pending("owner")))
        self.assertEqual(self.j.bal.snapshot(), before_balances)
        self.assertEqual(self.j.docs.snapshot(), before_docs)


class Stage10TelegramTests(AppCase):
    def test_start_command_gets_a_reply_only_in_owner_chat(self):
        updates = [
            {"update_id": 21, "message": {"chat": {"id": 123}, "text": "/start"}},
            {"update_id": 22, "message": {"chat": {"id": 456}, "text": "/start"}},
            {"update_id": 23, "message": {"chat": {"id": 123}, "text": "/help"}},
        ]
        with self.assertLogs("tera.telegram", level="INFO") as captured:
            with patch("telegram_bot.worker.send_message") as send:
                offset = handle_updates(updates, "bot-token", "123", offset=20)

        self.assertEqual(offset, 24)
        send.assert_called_once_with(
            "bot-token", "123",
            "Tera xabarnoma boti tayyor. Bildirishnomalar shu chatga keladi.",
        )
        self.assertIn("chat_id=123", captured.output[0])
        self.assertTrue(any("chat_id=456" in line for line in captured.output))

    def test_owner_notification_is_confirmed_after_delivery(self):
        backend = MemoryBackend()
        with self.env.db.transaction():
            outbox_id = self.e.outbox.add("owner", "sample_alert", None, {"note": "salom"}, T0)
        self.assertEqual(relay_outbox(self.env.db, backend, "owner", T0), 1)

        with patch("telegram_bot.worker.send_message") as send:
            delivered = process_owner_messages(
                self.env.db, backend, "bot-token", "owner-chat", now=T0,
            )

        self.assertEqual(delivered, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "owner-chat")
        self.assertEqual(self.env.db.one("SELECT sent_at FROM outbox WHERE id = ?", (outbox_id,))["sent_at"], T0)

    def test_failed_delivery_is_recorded_and_not_marked_sent(self):
        backend = MemoryBackend()
        with self.env.db.transaction():
            outbox_id = self.e.outbox.add("owner", "sample_alert", None, {"note": "salom"}, T0)
        relay_outbox(self.env.db, backend, "owner", T0)

        with self.assertLogs("tera.telegram", level="ERROR") as captured:
            with patch("telegram_bot.worker.send_message", side_effect=TelegramDeliveryError("API down")):
                delivered = process_owner_messages(
                    self.env.db, backend, "bot-token", "owner-chat", now=T0,
                )

        row = self.env.db.one("SELECT sent_at, attempts, last_error FROM outbox WHERE id = ?", (outbox_id,))
        self.assertIn("API down", captured.output[0])
        self.assertEqual(delivered, 0)
        self.assertIsNone(row["sent_at"])
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "API down")

    def test_notification_format_is_plain_text_and_bounded(self):
        text = format_notification("alert", {"value": "x" * 5000})
        self.assertLessEqual(len(text), 4096)
        self.assertIn("[kesib olindi]", text)

    def test_api_error_does_not_expose_bot_token(self):
        token = "secret-bot-token"
        error = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/sendMessage",
            401, "Unauthorized", {}, None,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramDeliveryError) as caught:
                send_message(token, "owner-chat", "alert")
        self.assertNotIn(token, str(caught.exception))

    def test_rate_limit_retry_after_is_honored_without_exposing_token(self):
        token = "secret-bot-token"
        error = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/getUpdates",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"description":"Too Many Requests","parameters":{"retry_after":17}}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramDeliveryError) as caught:
                fetch_updates(token, offset=0)

        self.assertEqual(caught.exception.retry_after, 17)
        self.assertIn("Too Many Requests", str(caught.exception))
        self.assertNotIn(token, str(caught.exception))

    def test_updates_use_long_polling_beyond_telegram_retry_window(self):
        response = io.BytesIO(b'{"ok":true,"result":[]}')
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(fetch_updates("bot-token", offset=0), [])

        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data)["timeout"], 50)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 65)


if __name__ == "__main__":
    unittest.main()
