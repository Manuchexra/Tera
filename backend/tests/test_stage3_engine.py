import unittest

from backend.balances.projection import projects
from backend.balances.rebuild import verify
from backend.core.errors import Conflict, DomainError, Forbidden
from backend.core.validate import as_int, as_list, as_str, need
from backend.modules import load_all
from backend.modules.auth.actor import Actor
from backend.modules.sync.registry import Command, command, registered_commands

from .helpers import T0, make_env

EXTRA = {
    "test3.deposit": ["owner", "agent"], "test3.double": ["owner"], "test3.limit": ["owner", "agent"],
    "test3.boom": ["owner"], "test3.notify": ["owner"], "test3.twoparty": ["owner", "agent"],
    "test3.info": ["owner"], "test3.float": ["owner"],
}


@projects("test3.deposit")
def _p_deposit(ev, bal, docs):
    bal.add(f"acct:{ev.payload['acct']}", ev.payload["amount"])


@command("test3.deposit")
def h_deposit(ctx, p):
    need(p, "acct")
    ev = ctx.emit("test3.deposit", {"acct": p["acct"], "amount": as_int(p, "amount", minimum=1)})
    return {"seq": ev.seq}


@command("test3.double")
def h_double(ctx, p):
    ctx.emit("test3.deposit", {"acct": "X", "amount": 10})
    ctx.emit("test3.deposit", {"acct": "X", "amount": 20})
    if p.get("fail"):
        raise DomainError("business_rule", "qoida buzildi", x=1)
    return {"emitted": ctx.events_emitted}


@command("test3.limit")
def h_limit(ctx, p):
    ctx.emit("test3.deposit", {"acct": "L", "amount": 5})
    raise Conflict("credit_limit_exceeded", "Limit oshdi", current=1_980_000, queued=55_000, over=35_000)


@command("test3.boom")
def h_boom(ctx, p):
    ctx.emit("test3.deposit", {"acct": "B", "amount": 5})
    raise RuntimeError("kutilmagan xato")


@command("test3.notify")
def h_notify(ctx, p):
    ctx.notify("telegram_store", "receipt", "store-1", {"total": 155_000})
    ctx.emit("test3.deposit", {"acct": "N", "amount": 1})
    if p.get("fail"):
        raise DomainError("business_rule", "x")


@command("test3.twoparty")
def h_two(ctx, p):
    ctx.two_party(p["other"], "loading")
    ctx.emit("test3.deposit", {"acct": "T", "amount": 1})


@command("test3.info")
def h_info(ctx, p):
    return {"limit": ctx.policy.credit_limit, "doc": ctx.new_id("SL"), "flag": ctx.time_flag,
            "occurred_at": ctx.occurred_at}


@command("test3.float")
def h_float(ctx, p):
    ctx.emit("test3.deposit", {"acct": "F", "amount": 1.5})


class EngineBase(unittest.TestCase):
    def setUp(self):
        self.env = make_env(extra_actions=EXTRA)
        self.e = self.env.engine
        self.j = self.env.journal
        self.agent_user, self.agent = self.env.add_user("jasur", ["agent"])
        self.wh_user, self.wh = self.env.add_user("malika", ["warehouse"])
        self.owner = self.env.owner
        self.n = 0

    def run_cmd(self, type_, payload=None, actor=None, **kw):
        self.n += 1
        return self.e.execute(self.env.cmd(self.n, type_, payload, **kw), actor or self.owner)

    def bal(self, key):
        return self.j.bal.get(key)


class AcceptedAndDuplicateTests(EngineBase):
    def test_accepted_returns_seq_and_response_and_updates_balance(self):
        r = self.run_cmd("test3.deposit", {"acct": "A", "amount": 100})
        self.assertEqual(r.status, "accepted")
        self.assertEqual(r.response["seq"], r.seq)
        self.assertEqual(self.bal("acct:A"), 100)
        ev = self.j.ledger.get("c1")
        self.assertEqual((ev.actor_id, ev.device_id), (self.owner.user_id, "dev-owner"))

    def test_same_event_id_twice_is_applied_once(self):
        c = self.env.cmd(1, "test3.deposit", {"acct": "A", "amount": 100})
        r1, r2 = self.e.execute(c, self.owner), self.e.execute(c, self.owner)
        self.assertEqual((r1.status, r2.status), ("accepted", "duplicate"))
        self.assertEqual(r2.seq, r1.seq)
        self.assertEqual(self.bal("acct:A"), 100)

    def test_duplicate_even_if_sent_by_someone_else_or_changed(self):
        self.e.execute(self.env.cmd(1, "test3.deposit", {"acct": "A", "amount": 100}), self.owner)
        r = self.e.execute(self.env.cmd(1, "test3.deposit", {"acct": "A", "amount": 999}), self.agent)
        self.assertEqual(r.status, "duplicate")
        self.assertEqual(self.bal("acct:A"), 100)

    def test_multi_event_ids_and_replay(self):
        r = self.run_cmd("test3.double", {})
        self.assertEqual(r.response["emitted"], 2)
        ids = [e.event_id for e in self.j.ledger.iter_events() if e.type == "test3.deposit"]
        self.assertEqual(ids, ["c1", "c1#1"])
        again = self.e.execute(self.env.cmd(1, "test3.double", {}), self.owner)
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(self.bal("acct:X"), 30)
        self.assertTrue(self.j.ledger.verify_chain().ok)


class RejectionTests(EngineBase):
    def inbox(self, status=None):
        return self.e.inbox.list(status)

    def test_unknown_command_rejected_and_stored(self):
        before = self.j.ledger.count()
        r = self.run_cmd("no.such.command", {"a": 1})
        self.assertEqual((r.status, r.code), ("rejected", "unknown_command"))
        self.assertEqual(self.j.ledger.count(), before)
        row = self.inbox("rejected")[0]
        self.assertEqual(row["command"]["payload"], {"a": 1})

    def test_rejected_replay_returns_same_answer(self):
        c = self.env.cmd(1, "no.such.command")
        r1, r2 = self.e.execute(c, self.owner), self.e.execute(c, self.owner)
        self.assertEqual((r2.status, r2.code, r2.replayed), ("rejected", "unknown_command", True))
        self.assertEqual(len(self.inbox()), 1)

    def test_forbidden_task(self):
        r = self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}, actor=self.wh)   # omborchi
        self.assertEqual((r.status, r.code), ("rejected", "forbidden"))
        self.assertEqual(self.bal("acct:A"), 0)
        self.assertEqual(self.inbox("rejected")[0]["actor_id"], self.wh.user_id)

    def test_deactivated_user(self):
        self.env.auth.set_active(self.owner, self.agent_user.user_id, False)
        r = self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}, actor=self.agent)
        self.assertEqual((r.code, r.details["reason"]), ("actor_not_allowed", "user_inactive"))

    def test_task_not_assigned_to_user(self):
        fake = Actor(self.agent.user_id, "owner", self.agent.device_id)
        r = self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}, actor=fake)
        self.assertEqual(r.details["reason"], "task_not_assigned")

    def test_revoked_device(self):
        self.env.auth.revoke_device(self.owner, self.agent.device_id)
        r = self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}, actor=self.agent)
        self.assertEqual(r.details["reason"], "device_not_allowed")

    def test_device_of_other_user(self):
        stolen = Actor(self.agent.user_id, "agent", self.wh.device_id)
        r = self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}, actor=stolen)
        self.assertEqual(r.details["reason"], "device_not_allowed")

    def test_bad_payloads(self):
        for payload, code in [({"acct": "A"}, "missing_field"), ({"acct": "A", "amount": "5"}, "invalid_field"),
                              ({"acct": "A", "amount": 0}, "invalid_field"), ({"acct": "A", "amount": True}, "invalid_field")]:
            r = self.run_cmd("test3.deposit", payload)
            self.assertEqual((r.status, r.code), ("rejected", code), payload)
        r = self.e.execute(Command("cx", "test3.deposit", ["not", "dict"], T0), self.owner)
        self.assertEqual(r.code, "invalid_payload")

    def test_float_in_event_payload_rejected(self):
        r = self.run_cmd("test3.float")
        self.assertEqual(r.code, "invalid_payload")

    def test_invalid_event_id_not_stored(self):
        for bad in ["", "has space"]:
            r = self.e.execute(Command(bad, "test3.deposit", {"acct": "A", "amount": 1}, T0), self.owner)
            self.assertEqual(r.code, "invalid_event_id")
        self.assertEqual(self.inbox(), [])

    def test_domain_error_after_emit_rolls_back_partial_work(self):
        before = self.j.ledger.count()
        r = self.run_cmd("test3.double", {"fail": True})
        self.assertEqual((r.status, r.code, r.details), ("rejected", "business_rule", {"x": 1}))
        self.assertEqual(self.j.ledger.count(), before)
        self.assertEqual(self.bal("acct:X"), 0)
        self.assertEqual(len(self.inbox("rejected")), 1)

    def test_unexpected_exception_propagates_and_leaves_nothing(self):
        before = self.j.ledger.count()
        with self.assertRaises(RuntimeError):
            self.run_cmd("test3.boom")
        self.assertEqual((self.j.ledger.count(), self.bal("acct:B"), self.inbox()), (before, 0, []))
        self.assertEqual(self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}).status, "accepted")  # baza ishlayveradi


class ConflictTests(EngineBase):
    def test_conflict_is_preserved_and_owner_alerted(self):
        r = self.run_cmd("test3.limit", {"store": "S1"}, actor=self.agent)
        self.assertEqual((r.status, r.code), ("conflict", "credit_limit_exceeded"))
        self.assertEqual(r.details["over"], 35_000)
        self.assertEqual(self.bal("acct:L"), 0)                              # jurnalga yozilmadi
        row = self.e.inbox.list("conflict")[0]
        self.assertEqual(row["command"]["payload"], {"store": "S1"})        # asl yozuv saqlandi
        alert = self.e.outbox.pending("owner")[0]
        self.assertEqual((alert["kind"], alert["payload"]["code"]), ("conflict_needs_review", "credit_limit_exceeded"))

    def test_conflict_replay_stays_conflict(self):
        c = self.env.cmd(1, "test3.limit")
        self.e.execute(c, self.agent)
        r = self.e.execute(c, self.agent)
        self.assertEqual((r.status, r.replayed), ("conflict", True))
        self.assertEqual(len(self.e.outbox.pending("owner")), 1)             # ogohlantirish bir marta

    def test_owner_resolves_and_original_is_kept(self):
        self.run_cmd("test3.limit", actor=self.agent)
        with self.assertRaises(Forbidden):
            self.e.resolve_inbox(self.agent, "c1", "ruxsat")
        with self.assertRaises(DomainError):
            self.e.resolve_inbox(self.owner, "c1", "  ")
        self.e.resolve_inbox(self.owner, "c1", "Do'kon ertaga to'laydi, istisno berildi")
        row = self.e.inbox.list("resolved")[0]
        self.assertEqual((row["resolved_by"], row["command"]["event_id"]), (self.owner.user_id, "c1"))
        self.assertTrue(any(e.type == "inbox_resolved" for e in self.j.ledger.iter_events()))
        with self.assertRaises(DomainError):
            self.e.resolve_inbox(self.owner, "c1", "yana")                    # ikkinchi marta emas
        replay = self.e.execute(self.env.cmd(1, "test3.limit"), self.agent)
        self.assertEqual(replay.status, "rejected")                          # hal qilingan; qayta o'tkazilmaydi

    def test_only_owner_lists_inbox(self):
        with self.assertRaises(Forbidden):
            self.e.list_inbox(self.agent)


class TimeTests(EngineBase):
    def test_device_time_trusted_when_clock_close(self):
        t = self.j.clock.now()
        r = self.run_cmd("test3.info", device_time=t)
        self.assertEqual((r.response["flag"], r.response["occurred_at"]), ("ok", t))

    def test_clock_an_hour_off_uses_server_time_and_flags(self):
        r = self.run_cmd("test3.info", device_time="2026-11-18T05:00:00.000Z")
        self.assertEqual(r.response["flag"], "suspicious")
        self.assertNotEqual(r.response["occurred_at"], "2026-11-18T05:00:00.000Z")

    def test_estimate_lets_late_offline_event_keep_its_time(self):
        r = self.run_cmd("test3.info", device_time="2026-11-18T03:00:00.000Z", est_server_time="2026-11-18T03:01:00.000Z")
        self.assertEqual((r.response["flag"], r.response["occurred_at"]), ("ok", "2026-11-18T03:00:00.000Z"))

    def test_clock_days_off_rejected(self):
        r = self.run_cmd("test3.info", device_time="2026-11-01T06:00:00.000Z")
        self.assertEqual(r.code, "device_clock_wrong")

    def test_invalid_dates_rejected(self):
        self.assertEqual(self.run_cmd("test3.info", device_time="kecha").code, "invalid_field")
        self.assertEqual(self.run_cmd("test3.info", device_time=T0, est_server_time="x").code, "invalid_field")

    def test_flag_saved_on_the_event(self):
        self.run_cmd("test3.deposit", {"acct": "A", "amount": 1}, device_time="2026-11-18T05:00:00.000Z")
        self.assertEqual(self.j.ledger.get("c1").time_flag, "suspicious")
        self.assertEqual(self.j.ledger.get("c1").device_time, "2026-11-18T05:00:00.000Z")   # asl qurilma vaqti ham saqlanadi


class ContextTests(EngineBase):
    def test_two_party_rule(self):
        r = self.run_cmd("test3.twoparty", {"other": self.agent.user_id}, actor=self.agent)
        self.assertEqual((r.status, r.code), ("rejected", "same_person_both_sides"))
        ok = self.run_cmd("test3.twoparty", {"other": self.wh.user_id}, actor=self.agent)
        self.assertEqual(ok.status, "accepted")

    def test_notify_written_only_when_command_succeeds(self):
        self.run_cmd("test3.notify", {"fail": True})
        self.assertEqual(self.e.outbox.pending("telegram_store"), [])
        self.run_cmd("test3.notify", {})
        msg = self.e.outbox.pending("telegram_store")[0]
        self.assertEqual((msg["kind"], msg["recipient"], msg["payload"]), ("receipt", "store-1", {"total": 155_000}))

    def test_outbox_mark_sent_and_failed(self):
        self.run_cmd("test3.notify", {})
        msg = self.e.outbox.pending("telegram_store")[0]
        self.e.outbox.mark_failed(msg["id"], "timeout")
        self.assertEqual(self.e.outbox.pending("telegram_store")[0]["attempts"], 1)
        self.e.outbox.mark_sent([msg["id"]], self.j.clock.now())
        self.assertEqual(self.e.outbox.pending("telegram_store"), [])

    def test_policy_default_and_override(self):
        self.assertEqual(self.run_cmd("test3.info").response["limit"], 2_000_000)
        with self.j.transaction():                                            # 4-bosqichda buni `policy.update` qiladi
            self.j.docs.put("policy:global", "policy", None, "active", {"credit_limit": 3_000_000})
        self.assertEqual(self.run_cmd("test3.info").response["limit"], 3_000_000)

    def test_new_id_per_business_day(self):
        self.assertEqual(self.run_cmd("test3.info").response["doc"], "SL-261118-001")
        self.assertEqual(self.run_cmd("test3.info").response["doc"], "SL-261118-002")


class BatchAndRebuildTests(EngineBase):
    def test_batch_each_command_independent(self):
        cmds = [self.env.cmd(1, "test3.deposit", {"acct": "A", "amount": 10}),
                self.env.cmd(2, "test3.limit"),
                self.env.cmd(3, "test3.deposit", {"acct": "A", "amount": 5}),
                self.env.cmd(1, "test3.deposit", {"acct": "A", "amount": 10})]
        res = self.e.execute_batch(cmds, self.agent)
        self.assertEqual([r.status for r in res], ["accepted", "conflict", "accepted", "duplicate"])
        self.assertEqual(self.bal("acct:A"), 15)

    def test_cache_equals_rebuild_after_mixed_operations(self):
        self.run_cmd("test3.deposit", {"acct": "A", "amount": 10})
        self.run_cmd("test3.double", {"fail": True})
        self.run_cmd("test3.double", {})
        self.run_cmd("test3.limit", actor=self.agent)
        self.run_cmd("test3.notify", {})
        load_all()
        rep = verify(self.env.db)
        self.assertTrue(rep.ok, (rep.balance_diffs, rep.doc_diffs))


class ValidateHelperTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(as_int({"a": 5}, "a", minimum=1, maximum=10), 5)
        self.assertEqual(as_int({}, "a", default=7), 7)
        for p in [{"a": 1.5}, {"a": "1"}, {"a": True}, {"a": 0}, {"a": 11}]:
            with self.assertRaises(DomainError):
                as_int(p, "a", minimum=1, maximum=10)
        self.assertEqual(as_str({"m": "cash"}, "m", allowed=("cash", "card")), "cash")
        with self.assertRaises(DomainError) as cm:
            as_str({"m": "gold"}, "m", allowed=("cash", "card"))
        self.assertEqual(cm.exception.details["allowed"], ["cash", "card"])
        self.assertEqual(as_list({"l": [1]}, "l"), [1])
        with self.assertRaises(DomainError):
            as_list({"l": []}, "l")
        with self.assertRaises(DomainError):
            need({"a": ""}, "a")
        with self.assertRaises(DomainError):
            need({"a": "   "}, "a")                      # faqat bo'sh joy ham "kiritilmagan"
        self.assertEqual(as_str({"r": "  sabab  "}, "r"), "sabab")      # tozalanadi
        with self.assertRaises(DomainError):
            as_str({"r": "   "}, "r")
        self.assertEqual(as_str({"r": " "}, "r", default=""), "")

    def test_registry_contains_test_commands(self):
        self.assertIn("test3.deposit", registered_commands())


if __name__ == "__main__":
    unittest.main()
