#!/usr/bin/env python3
"""Periodic steward check: fake-clock schedule, idle rule, retirement and reactivation.

Every test injects ``now``; nothing sleeps, no input or Stop hook runs, and the
real ``session_notice`` carrier writes into a private jobs root.
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import dispatch_pending_delivery as pending_delivery  # noqa: E402
import dispatch_session_sweep as sweep  # noqa: E402
import peer_obligations  # noqa: E402
import session_notice  # noqa: E402
import steward_check as SC  # noqa: E402

REAL_OBSERVE = SC._observe_role
REAL_ROLE_HELD = SC._role_held
REAL_HUMAN_WAIT = SC._human_wait_open
REAL_ROLE_FLAGGED = SC._role_flagged
REAL_SESSION_LIVENESS = SC._session_liveness
T0 = 1_000_000.0
INTERVAL = 7200
HARNESS = "claude"
SID = "sid-steward-A"
CARRIER = "claude-parent-runtime"


def target(name="worker", state="working", liveness="live", harness="claude", sid=None):
    return {"name": name, "harness": harness, "session_id": sid or f"sid-{name}",
            "state": state, "liveness": liveness}


class CheckBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "jobs.log").write_text("")
        self.config = self.tmp / "dispatch-defaults.yaml"
        env = {"AGENT_PEER_LEDGER_ROOT": str(self.tmp), "AGENT_DISPATCH_JOBS": str(self.tmp / "jobs.log"),
               "DISPATCH_DEFAULTS_CONFIG": str(self.config)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        SC._POLICY.update(signature=None, interval=SC.DEFAULT_INTERVAL_SECONDS, warnings=[])
        SC._THROTTLE.clear()
        self.store = peer_obligations.ObligationStore(self.tmp)
        self.role = {"held": True, "reason": None, "successor": None}
        self.seen = {"targets": [target()], "pending_messages": 0}
        self.liveness = "live"
        self.human_wait = False
        self.flagged = True
        for name, replacement in (
                ("_role_held", lambda h, s: dict(self.role)),
                ("_role_flagged", lambda h, s: self.flagged),
                ("_observe_role", lambda h, s: {**self.seen, "targets": list(self.seen["targets"]),
                                                "pending_messages": self.seen["pending_messages"]}),
                ("_session_liveness", lambda h, s, agents=None, observation=None: self.liveness),
                ("_human_wait_open", lambda h, s, now: self.human_wait),
                ("_agents", lambda: [])):
            patch = mock.patch.object(SC, name, side_effect=replacement)
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("ensure_normal_runner", "request_dispatch_reconciliation"):
            patch = mock.patch.object(peer_obligations, name, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        spy = mock.patch.object(session_notice, "notify", wraps=session_notice.notify)
        self.notify = spy.start()
        self.addCleanup(spy.stop)

    # helpers -------------------------------------------------------------
    def write_config(self, seconds):
        shipped = (ROOT / "profiles" / "dispatch-defaults.yaml").read_text(encoding="utf-8")
        text = re.sub(r"check_interval_seconds: \d+", f"check_interval_seconds: {seconds}", shipped)
        self.config.write_text(text, encoding="utf-8")
        stat = self.config.stat()
        os.utime(self.config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def register(self, now=T0, **kwargs):
        return SC.register(HARNESS, SID, now=now, store=self.store, ensure_runner=False, **kwargs)

    def duty(self):
        return self.store.get(SC.duty_id(HARNESS, SID))

    def step(self, now):
        SC.advance(self.duty(), self.store, now=now)
        return self.duty()

    def obs(self):
        return self.duty()["observation"]

    def notice(self, index=-1):
        """The receipt of the nth ``notify`` call, read back from the stored record."""
        call = self.notify.call_args_list[index]
        return call.kwargs

    def record(self):
        return SC._notice_record(SID, self.obs()["notice_delivery_id"])

    def deliver_and_ack(self):
        claimed, _count = sweep.sweep_deliver(self.tmp, CARRIER, SID)
        self.assertEqual(sweep.ack_delivered(self.tmp, SID, claimed, acked_by="test"), len(claimed))
        return claimed

    def check(self, now):
        out = io.StringIO()
        self.assertEqual(SC.cmd_check(HARNESS, SID, now=now, out=out), 0)
        return out.getvalue()


class StewardCheckScheduleTest(CheckBase):
    def test_default_interval_is_two_hours(self):
        self.assertEqual(SC.interval_seconds(), INTERVAL)

    def test_one_notice_at_due_then_busy_and_undelivered_merge(self):
        self.register()
        self.step(T0 + INTERVAL - 1)
        self.assertEqual(self.notify.call_count, 0)
        duty = self.step(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(duty["observation"]["phase"], "notified")
        self.assertEqual(self.notice()["required_action"], "run-steward-check")
        self.assertEqual(self.record()["state"], "pending")
        for later in (T0 + 2 * INTERVAL, T0 + 3 * INTERVAL, T0 + 10 * INTERVAL):
            self.step(later)
        self.assertEqual(self.notify.call_count, 1)  # undelivered: merged, never stacked

    def test_five_hour_gap_after_check_sends_exactly_one(self):
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 120)
        self.assertEqual(self.obs()["phase"], "scheduled")
        t1 = T0 + INTERVAL + 600
        self.check(t1)
        self.assertEqual(self.obs()["last_checked"], t1)
        self.assertEqual(self.obs()["anchor"], t1)
        gap = t1 + 5 * 3600
        self.step(gap)
        self.assertEqual(self.notify.call_count, 2)
        for later in (gap + 60, gap + 120, gap + 3600):
            self.step(later)
        self.assertEqual(self.notify.call_count, 2)  # no catch-up for the missed intervals

    def test_long_silence_without_a_check_still_sends_one(self):
        self.register()
        self.step(T0 + 5 * 3600)
        self.assertEqual(self.notify.call_count, 1)
        self.step(T0 + 5 * 3600 + 60)
        self.assertEqual(self.notify.call_count, 1)

    def test_a_notice_alone_does_not_record_last_checked(self):
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.assertIsNone(self.obs()["last_checked"])
        self.assertEqual(self.obs()["anchor"], T0 + INTERVAL + 100)

    def test_release_stops_every_later_notice(self):
        self.register()
        self.step(T0 + INTERVAL)
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.assertEqual(self.duty()["state"], "complete")
        self.assertEqual(self.store.list(kinds=[SC.DUTY_KIND]), [])  # not a runner duty any more
        for later in (T0 + 2 * INTERVAL, T0 + 9 * INTERVAL, T0 + 30 * INTERVAL):
            self.step(later)
        self.assertEqual(self.notify.call_count, 1)

    def test_register_is_idempotent_for_a_pending_duty(self):
        first = self.register()
        again = self.register(now=T0 + 500)
        self.assertEqual(first["observation"]["epoch"], again["observation"]["epoch"])
        self.assertEqual(again["observation"]["anchor"], T0)


class StewardCheckIntervalTest(CheckBase):
    def test_user_interval_is_read_and_used(self):
        self.write_config(3600)
        self.assertEqual(SC.interval_seconds(), 3600)
        self.register()
        self.step(T0 + 3599)
        self.assertEqual(self.notify.call_count, 0)
        self.step(T0 + 3600)
        self.assertEqual(self.notify.call_count, 1)

    def test_rewritten_file_is_read_again_by_stat(self):
        self.write_config(3600)
        self.assertEqual(SC.interval_seconds(), 3600)
        self.write_config(7200)
        self.assertEqual(SC.interval_seconds(), 7200)
        self.write_config(600)
        self.assertEqual(SC.interval_seconds(), 600)

    def test_changed_interval_applies_from_the_next_pass_without_accumulation(self):
        self.register()
        self.step(T0 + 3700)
        self.assertEqual(self.notify.call_count, 0)
        self.write_config(3600)
        self.step(T0 + 3800)
        self.assertEqual(self.notify.call_count, 1)

    def test_corrupt_file_falls_back_to_the_default_with_a_warning(self):
        self.config.write_text("garbage: [\n", encoding="utf-8")
        self.assertEqual(SC.interval_seconds(), INTERVAL)
        self.assertTrue(any("steward-settings-unreadable" in item for item in SC.policy_warnings()))

    def test_missing_file_uses_the_shipped_default_quietly(self):
        self.assertEqual(SC.interval_seconds(), INTERVAL)
        self.assertEqual(SC.policy_warnings(), [])

    def test_shipped_yaml_default_matches_the_runtime_default(self):
        spec = importlib.util.spec_from_file_location("_dd", str(HERE / "dispatch-defaults.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy, warnings = module.load_steward_policy(
            config_path=str(ROOT / "profiles" / "dispatch-defaults.yaml"))
        self.assertEqual(policy["check_interval_seconds"], INTERVAL)
        self.assertEqual(warnings, [])


class StewardCheckIdleTest(CheckBase):
    def due(self, index):
        return T0 + (index + 1) * INTERVAL + index * 200

    def test_no_target_sends_one_short_release_reminder_then_no_turn(self):
        self.seen = {"targets": [], "pending_messages": 0}
        self.register()
        self.step(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 1)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "release-if-idle")
        self.assertLessEqual(len(sent["text"]), session_notice.MAX_TEXT)
        self.assertIn("peer-steward steward off", sent["text"])
        self.deliver_and_ack()
        acked_at = T0 + INTERVAL + 100
        self.step(acked_at)
        for index in (1, 2):
            now = acked_at + index * INTERVAL
            duty = self.step(now)
            self.assertEqual(self.notify.call_count, 1)  # no model turn for an unchanged idle role
            self.assertEqual(duty["observation"]["skipped"], index)
            self.assertEqual(duty["observation"]["anchor"], now)
            self.assertIsNone(duty["observation"]["last_checked"])
            self.assertEqual(duty["state"], "pending")  # the role is never revoked here
        self.seen = {"targets": [target()], "pending_messages": 0}
        self.step(acked_at + 3 * INTERVAL)
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.notice()["required_action"], "run-steward-check")

    def test_pending_peer_message_is_not_idle(self):
        self.seen = {"targets": [], "pending_messages": 2}
        self.register()
        self.step(T0 + INTERVAL)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "assign-next")
        self.assertIn("2 peer request(s) queued for you → handle them as they arrive", sent["text"])
        self.assertNotIn("steward off", sent["text"])

    def test_all_finished_targets_send_one_tidy_reminder(self):
        self.seen = {"targets": [target("a", state="done"), target("b", liveness="ended")],
                     "pending_messages": 0}
        self.register()
        self.step(T0 + INTERVAL)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "tidy-and-release")
        self.assertIn("all 2 of your targets", sent["text"])
        self.assertLessEqual(len(sent["text"]), session_notice.MAX_TEXT)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.step(T0 + 2 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.obs()["reason"], "quiet-no-change")

    def test_a_state_change_sends_the_reminder_again(self):
        self.seen = {"targets": [target("a", state="done")], "pending_messages": 0}
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.seen = {"targets": [target("a", state="done"), target("b", state="done")],
                     "pending_messages": 0}
        self.step(T0 + 2 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.notice()["required_action"], "tidy-and-release")

    def test_a_working_target_is_checked_every_interval(self):
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.step(T0 + 2 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 2)  # same state still checks: stall is the point
        self.assertIn("peer-steward steward check", self.notice()["text"])

    def test_long_target_lists_stay_within_the_notice_limit(self):
        self.seen = {"targets": [target("x" * 40 + str(i), state="done") for i in range(12)],
                     "pending_messages": 0}
        self.register()
        self.step(T0 + INTERVAL)
        text = self.notice()["text"]
        self.assertLessEqual(len(text), session_notice.MAX_TEXT)
        self.assertIn("peer-steward steward off", text)


class StewardCheckRemainingWorkTest(CheckBase):
    """Remaining work turns a quiet check into concrete next actions, never a release."""

    def assert_no_stop_advice(self, text):
        for phrase in ("start no new work", "Start no new work", "steward off"):
            self.assertNotIn(phrase, text)

    def test_waiting_target_is_paired_with_open_work_and_its_wait_time(self):
        self.seen = {"targets": [target("e7", state="working")], "pending_messages": 0,
                     "open_work": ["#1425 review"]}
        self.register()
        self.check(T0)  # e7 still working: nothing waits yet
        self.seen["targets"] = [target("e7", state="idle")]
        self.check(T0 + 60)  # first seen waiting
        self.step(T0 + 60 + INTERVAL)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "assign-next")
        self.assertIn("[e7] waiting 120 min · #1425 review → assign now", sent["text"])
        self.assert_no_stop_advice(sent["text"])
        self.assertLessEqual(len(sent["text"]), session_notice.MAX_TEXT)

    def test_open_work_without_targets_is_not_a_release(self):
        self.seen = {"targets": [], "pending_messages": 0, "open_work": ["fix steward wording"]}
        self.register()
        self.step(T0 + INTERVAL)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "assign-next")
        self.assertIn("open: fix steward wording → assign or do now", sent["text"])
        self.assert_no_stop_advice(sent["text"])

    def test_finished_targets_with_open_work_get_the_work_not_a_tidy(self):
        self.seen = {"targets": [target("a", state="done"), target("b", liveness="ended")],
                     "pending_messages": 0, "open_work": ["next slice"]}
        self.register()
        self.step(T0 + INTERVAL)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "assign-next")
        self.assertIn("[a] waiting · next slice → assign now", sent["text"])
        self.assertNotIn("[b]", sent["text"])  # an ended session gets no assignment
        self.assert_no_stop_advice(sent["text"])

    def test_a_target_with_a_queued_peer_request_is_not_told_to_resend(self):
        owed = dict(target("e7", state="idle"), owed=1)
        self.seen = {"targets": [owed], "pending_messages": 0}
        self.register()
        self.step(T0 + INTERVAL)
        sent = self.notice()
        self.assertEqual(sent["required_action"], "assign-next")
        self.assertIn("[e7] 1 peer request(s) queued, not yet delivered → do not resend",
                      sent["text"])
        self.assertNotIn("follow up", sent["text"])

    def test_remaining_work_is_sent_again_every_interval(self):
        self.seen = {"targets": [target("e7", state="idle")], "pending_messages": 0,
                     "open_work": ["#1425 review"]}
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.step(T0 + 2 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 2)  # unchanged remaining work is not quiet

    def test_many_actions_stay_within_the_notice_limit(self):
        self.seen = {"targets": [target(f"t{i}", state="idle") for i in range(8)],
                     "pending_messages": 0, "open_work": ["x" * 200 + str(i) for i in range(10)]}
        self.register()
        self.step(T0 + INTERVAL)
        text = self.notice()["text"]
        self.assertLessEqual(len(text), session_notice.MAX_TEXT)
        self.assertIn("more", text)
        self.assertIn("→ assign now", text)

    def test_cmd_check_prints_next_actions_and_no_release_hint(self):
        self.seen = {"targets": [target("e7", state="idle")], "pending_messages": 0,
                     "open_work": ["#1425 review"]}
        self.register()
        out = self.check(T0 + 10)
        self.assertIn("next=[e7] waiting · #1425 review → assign now", out)
        self.assertIn("open_work=1", out)
        self.assertIn("hint=do the next actions now", out)
        self.assertNotIn("steward off", out)

    def test_cmd_check_without_targets_lists_open_work_instead_of_release(self):
        self.seen = {"targets": [], "pending_messages": 0, "open_work": ["#1425 review"]}
        self.register()
        out = self.check(T0 + 10)
        self.assertIn("next=open: #1425 review → assign or do now", out)
        self.assertNotIn("steward off", out)

    def test_nothing_remaining_still_releases(self):
        self.seen = {"targets": [target("a", state="done")], "pending_messages": 0, "open_work": []}
        self.register()
        self.step(T0 + INTERVAL)
        self.assertEqual(self.notice()["required_action"], "tidy-and-release")

    def test_no_notice_text_tells_a_steward_to_stop(self):
        for text in (SC.TEXT_RELEASE_IF_IDLE, SC.TEXT_TIDY_AND_RELEASE, SC.TEXT_RUN_CHECK,
                     SC.TEXT_ASSIGN_NEXT):
            self.assertNotIn("no new work", text.lower())
        self.assertIn("does not mean nothing remains", SC.TEXT_RUN_CHECK)


class StewardCheckOpenWorkSourceTest(unittest.TestCase):
    """The backlog comes from the existing Fleet work source for the holder's own cwd."""

    def test_open_work_reads_session_work_for_the_holder_cwd(self):
        rows = [SimpleNamespace(harness="claude", session_id=SID, cwd="/repo/wt"),
                SimpleNamespace(harness="codex", session_id="other", cwd="/elsewhere")]
        collector = SimpleNamespace(
            _registry_sessions=lambda: rows,
            _session_keys=lambda row: {(row.harness, row.session_id)})
        from fleet import session_work
        observed = mock.Mock(return_value=([{"type": "backlog", "headline": "  #1425   review "},
                                            {"type": "backlog", "headline": ""}], []))
        with mock.patch.object(SC, "_collector", return_value=collector), \
                mock.patch.object(session_work, "observe", observed):
            self.assertEqual(SC._open_work("claude", SID), (["#1425 review"], []))
        observed.assert_called_once_with("/repo/wt")

    def test_unknown_or_ambiguous_cwd_reads_no_backlog(self):
        rows = [SimpleNamespace(harness="claude", session_id=SID, cwd="/a"),
                SimpleNamespace(harness="claude", session_id=SID, cwd="/b")]
        collector = SimpleNamespace(_registry_sessions=lambda: rows,
                                    _session_keys=lambda row: {(row.harness, row.session_id)})
        from fleet import session_work
        with mock.patch.object(SC, "_collector", return_value=collector), \
                mock.patch.object(session_work, "observe") as observed:
            self.assertEqual(SC._open_work("claude", SID), ([], []))
        observed.assert_not_called()

    def test_observe_role_counts_peer_requests_owed_by_each_target(self):
        module = SimpleNamespace(pending_messages=lambda who: (
            [{"state": "pending"}, {"state": "acked"}] if who["session_id"] == "sid-e7" else []))
        collector = SimpleNamespace(
            role_targets=lambda h, s: [{"name": "e7", "harness": "codex", "session_id": "sid-e7"}],
            _peer_message_module=lambda: module)
        with mock.patch.object(SC, "_collector", return_value=collector), \
                mock.patch.object(SC, "_agents", return_value=[]), \
                mock.patch.object(SC, "_end_observation", return_value=None), \
                mock.patch.object(SC, "_session_liveness", return_value="live"), \
                mock.patch.object(SC, "_open_work", return_value=(["w"], [])):
            seen = SC._observe_role("claude", SID)
        self.assertEqual(seen["targets"][0]["owed"], 1)
        self.assertEqual(seen["pending_messages"], 0)
        self.assertEqual(seen["open_work"], ["w"])


class StewardCheckOwnTargetsTest(CheckBase):
    """The real role_targets winner rule: each steward sees only the targets it wins."""

    def setUp(self):
        super().setUp()
        from fleet.collectors import steward
        self.steward = steward
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        self.cwd = str(repo)

    def marker(self, sid, targets):
        return {"schema_version": 1, "harness": "claude", "session_id": sid,
                "generation": "gen-" + sid, "targets": {
                    name: {"harness": "claude", "session_id": f"sid-{name}", "name": name,
                           "kind": "watch", "source": "watch", "ts": ts}
                    for name, ts in targets.items()}}

    def test_cmd_check_lists_only_the_targets_this_steward_wins(self):
        markers = {("claude", "sid-A"): self.marker("sid-A", {"t1": "2026-10-27T01:00:00Z",
                                                              "t3": "2026-10-27T01:00:00Z"}),
                   ("claude", "sid-B"): self.marker("sid-B", {"t1": "2026-10-27T05:00:00Z",
                                                              "t2": "2026-10-27T05:00:00Z"})}
        rows = [SimpleNamespace(harness="claude", session_id=sid, cwd=self.cwd, runtime_name=None)
                for sid in ("sid-A", "sid-B", "sid-t1", "sid-t2", "sid-t3")]
        patches = [
            mock.patch.object(SC, "_observe_role", side_effect=REAL_OBSERVE),
            mock.patch.object(self.steward, "read_markers", return_value=markers),
            mock.patch.object(self.steward, "read_relations", return_value=[]),
            mock.patch.object(self.steward, "_registry_sessions", return_value=rows),
            mock.patch.object(self.steward, "_native_role_sessions", return_value=[])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        outputs = {}
        for sid in ("sid-A", "sid-B"):
            out = io.StringIO()
            self.assertEqual(SC.cmd_check("claude", sid, now=T0, out=out), 0)
            outputs[sid] = out.getvalue()
        names = lambda text: sorted(re.findall(r"^target name=(\S+)", text, re.M))
        self.assertEqual(names(outputs["sid-A"]), ["t3"])
        self.assertEqual(names(outputs["sid-B"]), ["t1", "t2"])
        self.assertIn("role=yes", outputs["sid-A"])
        self.assertIn("next_due_in_minutes=120", outputs["sid-A"])

    def test_not_a_steward_prints_role_no_and_records_nothing(self):
        self.role = {"held": False, "reason": "role-released", "successor": None}
        self.assertEqual(self.check(T0).strip(), "steward-check role=no")
        self.assertIsNone(self.duty())

    def test_changes_none_when_the_observation_is_unchanged(self):
        self.seen = {"targets": [target("a"), target("b")], "pending_messages": 0}
        self.register()
        first = self.check(T0 + 10)
        self.assertIn("changes=2", first)
        self.assertIn("changes=none", self.check(T0 + 20))
        self.seen = {"targets": [target("a", state="done"), target("b")], "pending_messages": 0}
        self.assertIn("changes=1", self.check(T0 + 30))

    def test_no_target_check_is_two_lines_with_the_release_hint(self):
        self.seen = {"targets": [], "pending_messages": 0}
        self.register()
        lines = self.check(T0 + 10).strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("targets=none changes=none", lines[0])
        self.assertIn("peer-steward steward off", lines[1])

    def test_check_retires_an_undelivered_notice_and_records_the_check(self):
        self.register()
        self.step(T0 + INTERVAL)
        delivery_id = self.obs()["notice_delivery_id"]
        self.assertEqual(SC._notice_record(SID, delivery_id)["state"], "pending")
        out = self.check(T0 + INTERVAL + 5)
        self.assertIn("undelivered_notices=1", out)
        self.assertEqual(SC._notice_record(SID, delivery_id)["state"], "rejected")
        self.assertEqual(self.obs()["phase"], "scheduled")
        self.assertEqual(self.obs()["last_checked"], T0 + INTERVAL + 5)


class StewardCheckRetireTest(CheckBase):
    def peer_steward(self):
        spec = importlib.util.spec_from_file_location("_peer_steward_for_retire",
                                                      str(HERE / "peer-steward.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def notified(self):
        self.register()
        self.step(T0 + INTERVAL)
        return self.obs()["notice_delivery_id"]

    def test_release_rejects_the_notice_and_closes_its_run_notice_duty(self):
        delivery_id = self.notified()
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.assertEqual(SC._notice_record(SID, delivery_id)["state"], "rejected")
        run_notice = self.store.list(kinds=["run-notice"])
        self.assertEqual(len(run_notice), 1)
        steward = self.peer_steward()
        with mock.patch.object(steward, "_herdr_agent_list", create=True) as listed, \
             mock.patch("subprocess.run") as run:
            steward._resume_ordinary_notice_obligation(run_notice[0], self.store)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(listed.call_count, 0)
        self.assertEqual(self.store.get(run_notice[0]["id"])["state"], "complete")
        self.assertEqual(self.store.list(kinds=["run-notice"]), [])
        self.assertEqual(sweep.sweep_deliver(self.tmp, CARRIER, SID)[0], [])

    def test_holder_change_moves_the_schedule_to_the_successor(self):
        self.register(now=T0)
        delivery_id = self.notified()
        self.role = {"held": False, "reason": "holder-changed", "successor": ("claude", "sid-succ")}
        self.step(T0 + INTERVAL + 100)  # first notified pass after the throttle window
        self.assertEqual(self.duty()["state"], "complete")
        self.assertEqual(self.obs()["reason"], "holder-changed")
        self.assertEqual(SC._notice_record(SID, delivery_id)["state"], "rejected")
        successor = self.store.get(SC.duty_id("claude", "sid-succ"))
        self.assertEqual(successor["state"], "pending")
        self.assertEqual(successor["observation"]["anchor"], T0)  # the old duty's schedule is inherited
        self.assertEqual(self.store.list(kinds=["steward-check"])[0]["id"], successor["id"])

    def test_an_existing_successor_duty_is_left_alone(self):
        SC.register("claude", "sid-succ", now=T0 + 50, store=self.store, ensure_runner=False)
        epoch = self.store.get(SC.duty_id("claude", "sid-succ"))["observation"]["epoch"]
        self.register()
        self.role = {"held": False, "reason": "holder-changed", "successor": ("claude", "sid-succ")}
        self.step(T0 + INTERVAL)
        successor = self.store.get(SC.duty_id("claude", "sid-succ"))
        self.assertEqual(successor["observation"]["epoch"], epoch)
        self.assertEqual(successor["observation"]["anchor"], T0 + 50)

    def test_proven_session_end_retires_the_duty_and_its_notice(self):
        delivery_id = self.notified()
        self.liveness = "ended"
        self.step(T0 + INTERVAL + 100)
        self.assertEqual(self.duty()["state"], "complete")
        self.assertEqual(self.obs()["reason"], "session-ended")
        self.assertEqual(SC._notice_record(SID, delivery_id)["state"], "rejected")

    def test_ended_session_before_due_is_retired_at_due(self):
        self.register()
        self.liveness = "ended"
        self.step(T0 + INTERVAL)
        self.assertEqual(self.obs()["reason"], "session-ended")
        self.assertEqual(self.notify.call_count, 0)

    def test_unknown_liveness_keeps_the_duty_and_the_notice(self):
        delivery_id = self.notified()
        self.liveness = "unknown"
        self.step(T0 + INTERVAL + 100)
        self.assertEqual(self.duty()["state"], "pending")
        self.assertEqual(SC._notice_record(SID, delivery_id)["state"], "pending")

    def test_notice_is_current_follows_the_duty_through_session_notice(self):
        delivery_id = self.notified()
        record = SC._notice_record(SID, delivery_id)
        self.assertTrue(session_notice.notice_is_current(record))  # real delegation, lazy import
        self.role = {"held": False, "reason": "holder-changed", "successor": None}
        self.assertFalse(session_notice.notice_is_current(record))
        self.role = {"held": True, "reason": None, "successor": None}
        self.assertTrue(session_notice.notice_is_current(record))
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.assertFalse(session_notice.notice_is_current(record))

    def test_a_stale_epoch_notice_is_not_current(self):
        delivery_id = self.notified()
        record = SC._notice_record(SID, delivery_id)
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.register(now=T0 + 10)
        self.assertFalse(session_notice.notice_is_current(record))  # an older life of the duty

    def test_other_notice_keys_stay_current_and_failures_stay_recoverable(self):
        other = session_notice.notify(HARNESS, "sid-other", key="run:host-1", subject="s", text="t",
                                      jobs=self.tmp / "jobs.log")
        self.assertTrue(session_notice.notice_is_current(other))
        self.notified()
        record = SC._notice_record(SID, self.obs()["notice_delivery_id"])
        with mock.patch.object(SC, "_store", side_effect=RuntimeError("store down")):
            self.assertTrue(session_notice.notice_is_current(record))

    def test_a_carrier_claim_drops_a_notice_the_duty_no_longer_vouches_for(self):
        self.notified()
        self.role = {"held": False, "reason": "holder-changed", "successor": None}
        claimed, _count = sweep.sweep_deliver(self.tmp, CARRIER, SID)
        self.assertEqual(claimed, [])
        self.assertEqual(self.record()["state"], "rejected")

    def test_role_held_reads_the_real_relation_ledger(self):
        marker = self.tmp / "peer-steward" / HARNESS / "sid-real.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "schema_version": 1, "harness": HARNESS, "session_id": "sid-real", "generation": "g-real",
            "targets": {"-": {"harness": "unknown", "name": "-", "session_id": None,
                              "kind": "explicit", "source": "explicit", "ts": "2026-10-27T01:00:00Z"}}}),
            encoding="utf-8")
        environ = {"XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.tmp / "home")}
        with mock.patch.dict(os.environ, environ):
            self.assertEqual(REAL_ROLE_HELD(HARNESS, "sid-real")["held"], True)
            released = REAL_ROLE_HELD(HARNESS, "sid-absent")
        self.assertEqual((released["held"], released["reason"]), (False, "role-released"))

    def test_settle_without_a_duty_is_a_noop(self):
        self.assertIsNone(SC.settle_session(HARNESS, SID, reason="role-released", store=self.store))


class StewardCheckReactivationTest(CheckBase):
    def test_register_after_settle_reactivates_with_a_fresh_epoch(self):
        self.register()
        old_epoch = self.obs()["epoch"]
        self.step(T0 + INTERVAL)
        first_delivery = self.obs()["notice_delivery_id"]
        self.deliver_and_ack()
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.assertEqual(self.duty()["state"], "complete")
        revived = self.register(now=T0 + 5000)
        self.assertEqual(revived["state"], "pending")
        self.assertEqual(revived["id"], SC.duty_id(HARNESS, SID))
        self.assertNotEqual(revived["observation"]["epoch"], old_epoch)
        self.assertEqual(revived["observation"]["cycle"], 0)
        self.assertEqual(revived["observation"]["anchor"], T0 + 5000)
        self.assertEqual(revived["observation"]["reason"], "reactivated")
        self.assertEqual(self.store.list(kinds=["steward-check"])[0]["id"], revived["id"])
        self.step(T0 + 5000 + INTERVAL)
        self.assertEqual(self.notify.call_count, 2)
        self.assertNotEqual(self.obs()["notice_delivery_id"], first_delivery)

    def test_reactivation_may_inherit_a_schedule_and_keeps_last_checked(self):
        self.register()
        self.check(T0 + 100)
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        revived = self.register(now=T0 + 9000, anchor=T0 + 300)
        self.assertEqual(revived["observation"]["anchor"], T0 + 300)
        self.assertEqual(revived["observation"]["last_checked"], T0 + 100)

    def test_a_pruned_record_registers_as_new(self):
        self.register()
        old_epoch = self.obs()["epoch"]
        os.unlink(self.store._record_path(SC.duty_id(HARNESS, SID)))
        fresh = self.register(now=T0 + 20)
        self.assertEqual(fresh["state"], "pending")
        self.assertNotEqual(fresh["observation"]["epoch"], old_epoch)

    def test_unknown_harness_and_empty_identity_register_nothing(self):
        self.assertIsNone(SC.register("other", SID, store=self.store, ensure_runner=False))
        self.assertIsNone(SC.register(HARNESS, "", store=self.store, ensure_runner=False))

    def marker(self, sid=SID):
        path = self.tmp / "peer-steward" / HARNESS / f"{sid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    def test_reconcile_self_registers_an_existing_steward_once(self):
        ident = SimpleNamespace(harness=HARNESS, session_id=SID)
        SC.reconcile_self(self.store, identity=ident)  # no marker, no duty: nothing
        self.assertIsNone(self.duty())
        self.marker()
        SC.reconcile_self(self.store, identity=ident)
        self.assertEqual(self.duty()["state"], "pending")
        epoch = self.obs()["epoch"]
        SC.reconcile_self(self.store, identity=ident)
        self.assertEqual(self.obs()["epoch"], epoch)

    def test_reconcile_self_retires_a_duty_whose_marker_is_gone(self):
        self.register()
        self.role = {"held": False, "reason": "role-released", "successor": None}
        SC.reconcile_self(self.store, identity=SimpleNamespace(harness=HARNESS, session_id=SID))
        self.assertEqual(self.duty()["state"], "complete")

    def test_reconcile_all_registers_holders_and_leaves_orphans_to_their_due(self):
        orphan = SC.register("claude", "sid-orphan", now=T0, store=self.store, ensure_runner=False)
        relations = [{"released": False, "origin": {"harness": "claude", "sid": SID},
                      "holder": {"harness": "claude", "sid": SID}, "targets": [{"name": "w"}]},
                     {"released": False, "origin": {"harness": "claude", "sid": "sid-ended"},
                      "holder": {"harness": "claude", "sid": "sid-ended"}, "targets": [{"name": "w"}]}]
        with mock.patch.object(SC, "_relations", return_value=relations), \
             mock.patch.object(SC, "_session_liveness",
                               side_effect=lambda h, s, agents=None, observation=None:
                                         "ended" if s == "sid-ended" else "live"), \
             mock.patch.object(peer_obligations.ObligationStore, "list",
                               side_effect=AssertionError("no store listing at runner start")):
            SC.reconcile_all(self.store)
        self.assertEqual(self.duty()["state"], "pending")
        self.assertIsNone(self.store.get(SC.duty_id("claude", "sid-ended")))
        self.assertEqual(self.store.get(orphan["id"])["state"], "pending")
        self.role = {"held": False, "reason": "role-released", "successor": None}
        SC.advance(self.store.get(orphan["id"]), self.store, now=T0 + INTERVAL)
        self.assertEqual(self.store.get(orphan["id"])["state"], "complete")
        self.assertEqual(self.notify.call_count, 0)

    def test_reconcile_all_without_holders_touches_nothing(self):
        with mock.patch.object(SC, "_relations", return_value=[]), \
             mock.patch.object(SC, "_agents", side_effect=AssertionError("no herdr call")), \
             mock.patch.object(peer_obligations.ObligationStore, "get",
                               side_effect=AssertionError("no record read")):
            SC.reconcile_all(self.store)


class _StubObservation:
    """A fixed Claude/Codex/OpenCode observation, as ``steward.end_observation()`` returns."""

    def __init__(self, codex=None, opencode=None, claude=None):
        self._codex, self._opencode, self._claude = codex, opencode, claude

    def claude(self):
        return self._claude

    def codex(self):
        return self._codex

    def opencode(self):
        return self._opencode


class StewardCheckProcessProofTest(CheckBase):
    """The real ``_session_liveness`` seam: an ended Codex/OpenCode holder retires its duty."""

    CODEX_SID = "11111111-2222-3333-4444-555555555555"
    OPENCODE_SID = "ses_ended0000000000000000"
    CLAUDE_SID = "cccccccc-2222-3333-4444-555555555555"

    def setUp(self):
        super().setUp()
        from fleet.collectors import codex, opencode, steward
        self.steward = steward
        registry = self.tmp / "registry"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(registry),
                                           "FLEET_SESSION_REGISTRY_DIR": str(registry)})
        env.start()
        self.addCleanup(env.stop)
        self.observation = _StubObservation()
        self.factory = mock.Mock(side_effect=lambda: self.observation)
        for target_module, name, replacement in (
                (SC, "_session_liveness", REAL_SESSION_LIVENESS),
                (steward, "end_observation", self.factory),
                (codex, "exact_rollout_for_session_id", lambda sid, homes=None: "/rollout/%s" % sid),
                (opencode, "session_activity", lambda sid: (1_000, None))):
            patch = mock.patch.object(target_module, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)
        herdr = mock.patch("fleet.collectors.herdr.list_agents", return_value=[])
        herdr.start()
        self.addCleanup(herdr.stop)

    def codex_seen(self, *, open_sids=(), complete=True):
        self.observation = _StubObservation(codex={"complete": complete,
                                                   "open": frozenset(open_sids)})

    def opencode_seen(self, *, visible=(), complete=True, earliest=5_000):
        self.observation = _StubObservation(opencode={
            "complete": complete, "visible": frozenset(visible), "earliest_start_ms": earliest})

    def claude_seen(self, **states):
        """The Claude census, with each recipient's state fixed (default ``unknown``)."""
        import recipient_lifetime
        by_sid = {self.CLAUDE_SID if key == "main" else key: value for key, value in states.items()}
        self.observation = _StubObservation(claude=recipient_lifetime.RecipientCensus(
            lambda harness, sid: by_sid.get(sid, "unknown")))

    def duty_of(self, harness, sid):
        return self.store.get(SC.duty_id(harness, sid))

    def due(self, harness, sid):
        SC.register(harness, sid, now=T0, store=self.store, ensure_runner=False)
        SC.advance(self.duty_of(harness, sid), self.store, now=T0 + INTERVAL)
        return self.duty_of(harness, sid)

    def test_s1_ended_codex_holder_retires_at_due_without_a_notice(self):
        self.codex_seen()
        duty = self.due("codex", self.CODEX_SID)
        self.assertEqual((duty["state"], duty["observation"]["reason"]),
                         ("complete", "session-ended"))
        self.assertEqual(self.notify.call_count, 0)

    def test_s2_ended_opencode_holder_retires_at_due(self):
        self.opencode_seen()
        duty = self.due("opencode", self.OPENCODE_SID)
        self.assertEqual((duty["state"], duty["observation"]["reason"]),
                         ("complete", "session-ended"))

    def test_s3_codex_holder_that_ends_after_the_notice_retires_it(self):
        self.codex_seen(open_sids=[self.CODEX_SID])
        duty = self.due("codex", self.CODEX_SID)
        self.assertEqual(duty["observation"]["phase"], "notified")
        delivery_id = duty["observation"]["notice_delivery_id"]
        self.codex_seen()
        SC.advance(self.duty_of("codex", self.CODEX_SID), self.store, now=T0 + INTERVAL + 100)
        duty = self.duty_of("codex", self.CODEX_SID)
        self.assertEqual((duty["state"], duty["observation"]["reason"]),
                         ("complete", "session-ended"))
        self.assertEqual(SC._notice_record(self.CODEX_SID, delivery_id)["state"], "rejected")

    def test_s4_incomplete_observation_keeps_the_duty(self):
        self.codex_seen(complete=False)
        duty = self.due("codex", self.CODEX_SID)
        self.assertEqual(duty["state"], "pending")
        self.assertEqual(duty["observation"]["phase"], "notified")
        self.opencode_seen(complete=False)
        self.assertEqual(self.due("opencode", self.OPENCODE_SID)["state"], "pending")

    def test_s5_reconcile_all_shares_one_observation_and_skips_the_ended(self):
        sids = {"codex-ended": self.CODEX_SID, "opencode-ended": self.OPENCODE_SID,
                "codex-live": "99999999-2222-3333-4444-555555555555"}
        self.observation = _StubObservation(
            codex={"complete": True, "open": frozenset([sids["codex-live"]])},
            opencode={"complete": True, "visible": frozenset(), "earliest_start_ms": 5_000})
        relations = [{"released": False, "origin": {"harness": h, "sid": sid},
                      "holder": {"harness": h, "sid": sid}, "targets": [{"name": "w"}]}
                     for h, sid in (("codex", sids["codex-ended"]), ("opencode", sids["opencode-ended"]),
                                    ("codex", sids["codex-live"]))]
        with mock.patch.object(SC, "_relations", return_value=relations):
            SC.reconcile_all(self.store)
        self.assertEqual(self.factory.call_count, 1)
        self.assertIsNone(self.duty_of("codex", sids["codex-ended"]))
        self.assertIsNone(self.duty_of("opencode", sids["opencode-ended"]))
        self.assertEqual(self.duty_of("codex", sids["codex-live"])["state"], "pending")

    def test_s6_observe_role_reports_each_target_with_one_shared_observation(self):
        self.observation = _StubObservation(
            codex={"complete": True, "open": frozenset()},
            opencode={"complete": True, "visible": frozenset([self.OPENCODE_SID]),
                      "earliest_start_ms": 5_000})
        targets = [{"name": "cx", "harness": "codex", "session_id": self.CODEX_SID},
                   {"name": "oc", "harness": "opencode", "session_id": self.OPENCODE_SID}]
        with mock.patch.object(self.steward, "role_targets", return_value=targets), \
                mock.patch.object(self.steward, "_peer_message_module", return_value=None):
            seen = REAL_OBSERVE(HARNESS, SID)
        self.assertEqual({t["name"]: t["liveness"] for t in seen["targets"]},
                         {"cx": "ended", "oc": "live"})
        self.assertEqual(self.factory.call_count, 1)


    def test_s7_ended_claude_holder_retires_at_due_without_a_notice(self):
        self.claude_seen(main="ended")
        duty = self.due("claude", self.CLAUDE_SID)
        self.assertEqual((duty["state"], duty["observation"]["reason"]),
                         ("complete", "session-ended"))
        self.assertEqual(self.notify.call_count, 0)

    def test_s8_claude_holder_that_ends_after_the_notice_retires_it(self):
        self.claude_seen(main="alive")
        duty = self.due("claude", self.CLAUDE_SID)
        self.assertEqual(duty["observation"]["phase"], "notified")
        delivery_id = duty["observation"]["notice_delivery_id"]
        self.claude_seen(main="ended")
        SC.advance(self.duty_of("claude", self.CLAUDE_SID), self.store, now=T0 + INTERVAL + 100)
        duty = self.duty_of("claude", self.CLAUDE_SID)
        self.assertEqual((duty["state"], duty["observation"]["reason"]),
                         ("complete", "session-ended"))
        self.assertEqual(SC._notice_record(self.CLAUDE_SID, delivery_id)["state"], "rejected")

    def test_s9_alive_or_unknown_claude_holder_keeps_the_duty(self):
        self.claude_seen(main="unknown")
        duty = self.due("claude", self.CLAUDE_SID)
        self.assertEqual((duty["state"], duty["observation"]["phase"]), ("pending", "notified"))
        alive = "dddddddd-2222-3333-4444-555555555555"
        self.claude_seen(**{alive: "alive"})
        self.assertEqual(self.due("claude", alive)["state"], "pending")

    def test_s10_reconcile_all_skips_the_ended_claude_holder_with_one_observation(self):
        gone, kept, unsure = ("ended-" + "0" * 8, "alive-" + "0" * 8, "unsure-" + "0" * 8)
        self.claude_seen(**{gone: "ended", kept: "alive"})
        relations = [{"released": False, "origin": {"harness": "claude", "sid": sid},
                      "holder": {"harness": "claude", "sid": sid}, "targets": [{"name": "w"}]}
                     for sid in (gone, kept, unsure)]
        with mock.patch.object(SC, "_relations", return_value=relations):
            SC.reconcile_all(self.store)
        self.assertEqual(self.factory.call_count, 1)
        self.assertIsNone(self.duty_of("claude", gone))
        self.assertEqual(self.duty_of("claude", kept)["state"], "pending")
        self.assertEqual(self.duty_of("claude", unsure)["state"], "pending")


class StewardCheckWindowClosedTest(CheckBase):
    """A role is released only when native evidence proves its exact SID ended."""

    KEY = "seat-key-1"

    def setUp(self):
        super().setUp()
        self.released = mock.Mock(return_value={"roles": 1})
        peer_message = SimpleNamespace(peer_state_root=lambda: self.tmp,
                                       release_closed_session=self.released)
        for target, name, replacement in (
                (SC, "_collector", lambda: SimpleNamespace(_peer_message_module=lambda: peer_message)),
                (SC, "_relations", lambda: [{"holder": {"harness": HARNESS, "sid": SID},
                                             "origin": {"harness": HARNESS, "sid": SID},
                                             "seat_key": self.KEY, "released": False}]),
                (SC, "_end_observation", lambda: None)):
            patcher = mock.patch.object(target, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.liveness = "unknown"
        patcher = mock.patch.object(SC, "_session_liveness",
                                    side_effect=lambda harness, sid, *args, **kwargs: self.liveness)
        patcher.start()
        self.addCleanup(patcher.stop)

    def advance(self):
        self.register()
        return self.step(T0 + 1)

    def test_unknown_lifetime_keeps_the_role_and_duty(self):
        duty = self.advance()
        self.released.assert_not_called()
        self.assertNotEqual(duty["state"], "complete")

    def test_live_lifetime_keeps_the_role(self):
        self.liveness = "live"
        duty = self.advance()
        self.released.assert_not_called()
        self.assertNotEqual(duty["state"], "complete")

    def test_exact_ended_lifetime_releases_with_session_ended_reason(self):
        self.liveness = "ended"
        duty = self.advance()
        self.released.assert_called_once_with(HARNESS, SID, reason="session-ended")
        self.assertEqual((duty["state"], duty["observation"]["reason"]),
                         ("complete", "session-ended"))

    def test_unresolved_role_release_keeps_the_duty_for_the_next_pass(self):
        self.liveness = "ended"
        self.released.return_value = {"roles": "unresolved", "marker": "kept"}
        duty = self.advance()
        self.released.assert_called_once_with(HARNESS, SID, reason="session-ended")
        self.assertNotEqual(duty["state"], "complete")


class StewardCheckEscTest(CheckBase):
    def test_an_interrupted_idle_session_still_gets_the_notice(self):
        # Esc leaves no steward-side signal: an idle pane is checked like any other.
        self.seen = {"targets": [target(state="idle")], "pending_messages": 0}
        self.register()
        self.step(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 1)

    def test_an_open_question_defers_and_the_closed_one_sends_once(self):
        self.register()
        self.human_wait = True
        self.step(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 0)
        self.assertEqual(self.obs()["reason"], "human-wait-deferred")
        self.assertEqual(self.obs()["anchor"], T0)  # schedule kept, not pushed forward
        self.step(T0 + INTERVAL + 30)  # within the recheck window: nothing is even read
        self.human_wait = False
        self.step(T0 + INTERVAL + 40)
        self.assertEqual(self.notify.call_count, 0)  # still inside the 60 s recheck throttle
        self.step(T0 + INTERVAL + 61)
        self.assertEqual(self.notify.call_count, 1)
        self.step(T0 + INTERVAL + 200)
        self.assertEqual(self.notify.call_count, 1)

    def test_human_wait_signal_is_read_for_this_session_only(self):
        signals = [{"harness": "claude", "session_id": "other", "stale": False},
                   {"harness": "claude", "session_id": SID, "stale": True}]
        import human_wait_state
        with mock.patch.object(human_wait_state, "read_signals", return_value=signals):
            self.assertFalse(REAL_HUMAN_WAIT(HARNESS, SID, T0))
            signals.append({"harness": "claude", "session_id": SID, "stale": False})
            self.assertTrue(REAL_HUMAN_WAIT(HARNESS, SID, T0))



class StewardCheckRoleFlagTest(CheckBase):
    """Only a session Fleet flags as steward receives the periodic request."""

    def test_foreign_repository_evidence_sends_nothing_and_keeps_the_duty(self):
        self.register()
        self.flagged = False
        duty = self.step(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 0)
        self.assertEqual(duty["observation"]["reason"], "no-steward-flag")
        self.assertEqual(duty["observation"]["anchor"], T0 + INTERVAL)
        self.assertIsNone(duty["observation"]["last_checked"])
        self.assertTrue(SC._active(duty))
        self.step(T0 + INTERVAL * 4)
        self.assertEqual(self.notify.call_count, 0)

    def test_flag_raised_later_gets_one_request_at_the_next_due(self):
        self.register()
        self.flagged = False
        self.step(T0 + INTERVAL)
        self.flagged = True
        self.step(T0 + INTERVAL * 2 - 1)
        self.assertEqual(self.notify.call_count, 0)
        self.step(T0 + INTERVAL * 2)
        self.assertEqual(self.notify.call_count, 1)

    def test_real_flag_seam_reads_the_fleet_role_flag(self):
        collector = SimpleNamespace(role_flag=mock.Mock(return_value=False))
        with mock.patch.object(SC, "_collector", return_value=collector):
            self.assertFalse(REAL_ROLE_FLAGGED(HARNESS, SID))
        collector.role_flag.assert_called_once_with(HARNESS, SID)
        with mock.patch.object(SC, "_collector", return_value=None):
            self.assertTrue(REAL_ROLE_FLAGGED(HARNESS, SID))


def _load_peer_steward():
    spec = importlib.util.spec_from_file_location("peer_steward_wiring", str(HERE / "peer-steward.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StewardCheckRunnerWiringTest(CheckBase):
    """The real peer-steward runner path advances the duty with no input or Stop hook."""

    @classmethod
    def setUpClass(cls):
        cls.peer_steward = _load_peer_steward()

    def selected(self):
        return [duty["id"] for duty in self.peer_steward._runner_duties(self.store)]

    def runner_pass(self, now):
        with mock.patch.object(SC, "_now", return_value=now):
            self.peer_steward._reconcile_obligations(
                self.store, self.peer_steward._runner_duties(self.store))

    def test_runner_pass_sends_one_notice_at_due_and_merges_after(self):
        self.register()
        self.assertIn(SC.duty_id(HARNESS, SID), self.selected())
        self.runner_pass(T0 + INTERVAL - 1)
        self.assertEqual(self.notify.call_count, 0)
        self.runner_pass(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 1)
        self.runner_pass(T0 + INTERVAL * 3)
        self.assertEqual(self.notify.call_count, 1)

    def test_released_role_leaves_the_runner_selection(self):
        self.register()
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.assertNotIn(SC.duty_id(HARNESS, SID), self.selected())
        self.runner_pass(T0 + INTERVAL * 5)
        self.assertEqual(self.notify.call_count, 0)

    def test_lifecycle_reconcile_moves_an_existing_steward_onto_the_schedule(self):
        marker = self.tmp / "peer-steward" / HARNESS / f"{SID}.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        self.assertIsNone(self.duty())
        SC.reconcile_self(self.store, identity=SimpleNamespace(harness=HARNESS, session_id=SID))
        self.assertTrue(SC._active(self.duty()))
        self.assertIn(SC.duty_id(HARNESS, SID), self.selected())

    def test_written_grant_starts_the_shared_runner(self):
        self.peer_steward._ensure_steward_runner({"status": "granted", "written": True})
        self.assertEqual(peer_obligations.ensure_normal_runner.call_count, 1)
        self.peer_steward._ensure_steward_runner({"status": "limited", "written": False})
        self.assertEqual(peer_obligations.ensure_normal_runner.call_count, 1)


class StewardCheckReviewCounterexampleTest(CheckBase):
    """PR #1428 review counterexamples: real memory row shapes and undelivered requests."""

    ROWS = (("r1", "completion", '["backlog"]', "W7 백로그 판정 — 17건 닫음"),
            ("r2", "artifact-pointer", "[]", "[해결 확인] 소비한 frame 알림 중복 해결"),
            ("r3", "feedback", '["backlog"]', "백로그는 기억에 항목별로 쓰고 정본 문서를 만들지 않는다"),
            ("r4", "unresolved-obligation", "[]", "사용자 결정 대기 묶음: D-5"))

    def real_open_work(self, project="proj", env=()):
        """``_open_work`` through the real ``session_work.observe`` on a synthetic store."""
        import datetime
        import sqlite3
        from fleet.collectors import memory
        store, repo = self.tmp / "mem", self.tmp / project
        store.mkdir()
        (repo / ".git").mkdir(parents=True)
        con = sqlite3.connect(store / "memory.db")
        con.execute("CREATE TABLE records(id TEXT, canonical_id TEXT, scope TEXT, cwd_origin TEXT,"
                    " type TEXT, tags TEXT, links TEXT, artifact_refs TEXT, headline TEXT,"
                    " updated TEXT, created TEXT, status TEXT)")
        today = datetime.date.today().isoformat()
        for rid, rtype, tags, headline in self.ROWS:
            con.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, rid, "project", str(repo), rtype, tags, "[]", "[]", headline,
                         today, today, "active"))
        con.commit()
        con.close()
        collector = SimpleNamespace(
            _registry_sessions=lambda: [SimpleNamespace(cwd=str(repo))],
            _session_keys=lambda row: {(HARNESS, SID)})
        memory._project_cache.clear()
        self.addCleanup(memory._project_cache.clear)
        with mock.patch.dict(os.environ, {"MEM_STORE": str(store), **dict(env)}), \
                mock.patch.object(SC, "_collector", return_value=collector):
            return SC._open_work(HARNESS, SID)

    def test_parity_register_rows_are_not_steward_work(self):
        """PR #1443 review P1: register rows stay on the Fleet screen, not in every check."""
        from fleet.collectors import memory
        home = self.tmp / "agent-home"
        (home / "core").mkdir(parents=True)
        (home / "core" / "ADAPTATION.md").write_text(
            "<!-- parity-register:start -->\n"
            "| PG-1 | Codex | no wake | open | fix |\n"
            "| PG-2 | Claude | choice | needs-user-decision | ask |\n"
            "<!-- parity-register:end -->\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(home)}):
            self.assertEqual(len(memory.parity_open_items()), 2)   # the Fleet screen still reads them
        work, decisions = self.real_open_work("hearting", {"AGENT_HOME": str(home)})
        self.assertEqual((work, decisions), ([], ["사용자 결정 대기 묶음: D-5"]))

    def test_finished_memory_records_are_not_open_work(self):
        self.assertEqual(self.real_open_work(), ([], ["사용자 결정 대기 묶음: D-5"]))

    def test_user_decision_and_finished_records_are_never_assigned(self):
        work, decisions = self.real_open_work()
        self.seen = {"targets": [target("e7", state="done")], "pending_messages": 0,
                     "open_work": work, "user_decisions": decisions}
        self.register()
        self.step(T0 + INTERVAL)
        text = self.notice()["text"]
        self.assertNotIn("→ assign", text)
        self.assertIn("awaiting user decision: 사용자 결정 대기 묶음: D-5 → raise it to the user", text)
        for finished in ("해결 확인", "W7 백로그", "정본 문서"):
            self.assertNotIn(finished, text)

    def test_unchanged_open_records_send_no_further_turn(self):
        self.seen = {"targets": [target("e7", state="done")], "pending_messages": 0,
                     "open_work": ["next slice"]}
        self.register()
        self.step(T0 + INTERVAL)
        self.assertEqual(self.notice()["required_action"], "assign-next")
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.step(T0 + 2 * INTERVAL + 100)
        self.step(T0 + 3 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.obs()["reason"], "quiet-no-change")
        self.seen["open_work"] = ["next slice", "new item"]
        self.step(T0 + 4 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 2)  # a changed record set is sent again

    def test_ended_target_with_an_undeliverable_request_still_tidies(self):
        ended = dict(target("e7", state="done", liveness="ended"), owed=1)
        seen = {"targets": [ended], "pending_messages": 0, "open_work": []}
        self.assertEqual(SC.next_actions(seen, {}, T0), [])
        self.assertEqual(SC._classify(seen), "tidy-and-release")

    def test_undelivered_requests_are_not_called_unanswered(self):
        seen = {"targets": [dict(target("e7", state="working"), owed=1)], "pending_messages": 2,
                "open_work": []}
        text = " ".join(SC.next_actions(seen, {}, T0))
        for wrong in ("unanswered", "follow up", "do that work now"):
            self.assertNotIn(wrong, text)
        self.assertIn("queued", text)


if __name__ == "__main__":
    unittest.main()
