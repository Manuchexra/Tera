import unittest
from datetime import date
from decimal import Decimal

from backend.core.errors import Conflict, DomainError, Forbidden
from backend.core.money import allocate_proportionally, pct, round_uzs
from backend.core.policy import Policy
from backend.core.timing import (add_working_days, fmt_ts, local_date, month_of, parse_ts,
                                resolve_occurred_at)


class MoneyTests(unittest.TestCase):
    def test_round_half_up(self):
        self.assertEqual(round_uzs(Decimal("578.9")), 579)
        self.assertEqual(round_uzs(Decimal("289.5")), 290)
        self.assertEqual(round_uzs(Decimal("2.5")), 3)      # banker's rounding emas
        self.assertEqual(round_uzs(7), 7)

    def test_pct(self):
        self.assertEqual(pct(5, 100), Decimal("5.00"))
        self.assertEqual(pct(0, 0), Decimal(0))
        # kam chiqish: (10 000 - 9 500) / 10 000
        self.assertEqual(pct(500, 10_000), Decimal("5.00"))

    def test_allocate_sums_exactly(self):
        for total, weights in [(100, [1, 1, 1]), (35_000, [80_000, 75_000]), (17, [5, 3, 9, 1])]:
            parts = allocate_proportionally(total, weights)
            self.assertEqual(sum(parts), total)
            self.assertTrue(all(p >= 0 for p in parts))

    def test_allocate_discount_example(self):
        # 5% chegirma 360 000 dan = 18 000, qatorlar 200 000 va 160 000
        self.assertEqual(allocate_proportionally(18_000, [200_000, 160_000]), [10_000, 8_000])

    def test_allocate_edge(self):
        self.assertEqual(allocate_proportionally(0, [1, 2]), [0, 0])
        self.assertEqual(allocate_proportionally(5, []), [])
        with self.assertRaises(ValueError):
            allocate_proportionally(5, [0, 0])


class TimingTests(unittest.TestCase):
    REC = "2026-11-18T06:35:00.000Z"

    def test_parse_format_roundtrip(self):
        self.assertEqual(fmt_ts(parse_ts(self.REC)), self.REC)

    def test_local_date_uses_tashkent(self):
        # 19:30 UTC = 00:30 keyingi kun Toshkentda
        self.assertEqual(local_date("2026-11-18T19:30:00.000Z"), date(2026, 11, 19))
        self.assertEqual(local_date("2026-11-18T18:59:00.000Z"), date(2026, 11, 18))
        self.assertEqual(month_of("2026-11-30T20:00:00.000Z"), "2026-12")

    def test_device_time_trusted_when_close(self):
        occ, flag = resolve_occurred_at("2026-11-18T06:33:00.000Z", self.REC)
        self.assertEqual((occ, flag), ("2026-11-18T06:33:00.000Z", "ok"))

    def test_offline_late_upload_keeps_device_time_with_estimate(self):
        # 3 soat oldin yaratilgan, lekin qurilma server vaqtini to'g'ri taxmin qilgan
        occ, flag = resolve_occurred_at("2026-11-18T03:35:00.000Z", self.REC,
                                        est_server_time="2026-11-18T03:36:00.000Z")
        self.assertEqual((occ, flag), ("2026-11-18T03:35:00.000Z", "ok"))

    def test_suspicious_uses_server_time(self):
        occ, flag = resolve_occurred_at("2026-11-18T06:00:00.000Z", self.REC)
        self.assertEqual((occ, flag), (self.REC, "suspicious"))

    def test_clock_way_off_rejected(self):
        with self.assertRaises(DomainError) as cm:
            resolve_occurred_at("2026-11-10T06:35:00.000Z", self.REC)
        self.assertEqual(cm.exception.code, "device_clock_wrong")

    def test_working_days(self):
        # chorshanba + 2 ish kuni = juma; juma + 2 = seshanba
        self.assertEqual(add_working_days(date(2026, 11, 18), 2), date(2026, 11, 20))
        self.assertEqual(add_working_days(date(2026, 11, 20), 2), date(2026, 11, 24))
        self.assertEqual(add_working_days(date(2026, 11, 18), 0), date(2026, 11, 18))


class PolicyTests(unittest.TestCase):
    def test_defaults_match_requirements(self):
        p = Policy.default()
        self.assertEqual((p.credit_limit, p.payment_term_days, p.dispute_working_days), (2_000_000, 14, 2))
        self.assertEqual((p.shrink_min_batches, p.shrink_min_input_g), (8, 50_000))
        self.assertEqual(p.day_close_deadline, "21:00")

    def test_changes_and_roundtrip(self):
        p = Policy.default().with_changes(credit_limit=3_000_000)
        self.assertEqual(Policy.from_dict(p.to_dict()), p)
        self.assertEqual(Policy.default().credit_limit, 2_000_000)   # asl o'zgarmaydi (immutable)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            Policy.default().with_changes(nonsense=1)

    def test_missing_key_rejected(self):
        with self.assertRaises(ValueError):
            Policy.from_dict({"credit_limit": 1})

    def test_merged_ignores_unknown_and_none(self):
        base = Policy.default()
        self.assertEqual(base.merged(None), base)
        self.assertEqual(base.merged({"old_key": 1, "credit_limit": 1}).credit_limit, 1)


class ErrorTests(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(DomainError("x").status, "rejected")
        self.assertEqual(Forbidden("x").status, "rejected")
        self.assertEqual(Conflict("credit_limit_exceeded", over=35_000).status, "conflict")
        self.assertEqual(Conflict("c", over=1).details, {"over": 1})


if __name__ == "__main__":
    unittest.main()
