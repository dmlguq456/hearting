#!/usr/bin/env python3
"""Periodic steward check: fake-clock schedule, idle rule, retirement and reactivation.

Every test injects ``now``; nothing sleeps, no input or Stop hook runs, and the
real ``session_notice`` carrier writes into a private jobs root.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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
REAL_ENSURE_NORMAL_RUNNER = peer_obligations.ensure_normal_runner
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



class StewardCheckLabTest(CheckBase):
    """Real catalog/board, synthetic roots only; no native model or live check."""

    def setUp(self):
        super().setUp()
        language = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        language.start()
        self.addCleanup(language.stop)
        import experiment_board as board
        import experiment_catalog as catalog
        spec = importlib.util.spec_from_file_location("lab_catalog_fixture", HERE / "experiment_catalog.test.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        self.fixture = fixture.Base()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.board, self.catalog = board, catalog
        board.clear_cache()
        self.addCleanup(board.clear_cache)
        self.root = self.fixture.make_root()
        self.now = fixture.NOW
        self.fixture.register(self.root, {"groups": [fixture.group_spec()], "conditions": [
            {"display_name": name, "group": "AMI 가변 화자 개선", "refs": {"runs": [{"run_id": rid}]}}
            for name, rid in (("작은 모델", "small"), ("큰 모델", "large"))]})
        self.write_run("small", 20.0)
        self.write_run("large", 21.0)
        self.targets = [target("a", state="done"), target("b", state="done")]
        self.rows = [SimpleNamespace(harness=t["harness"], session_id=t["session_id"],
                                     cwd=str(self.root.parent)) for t in self.targets]
        self.collector = SimpleNamespace(
            _registry_sessions=lambda: self.rows, role_targets=lambda *a, **kw: self.targets,
            _peer_message_module=lambda: None)
        for patch in (
            mock.patch.object(SC, "_collector", return_value=self.collector),
            mock.patch.object(SC, "_observe_role", side_effect=REAL_OBSERVE),
            mock.patch.object(SC, "_end_observation", return_value=None),
            mock.patch.object(SC, "_open_work", return_value=([], [])),
            mock.patch("peer_steward_native.native_agent", return_value={"result": {"agent": {"agent_status": "done"}}}),
            mock.patch.object(board.time, "time", return_value=self.now)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_run(self, rid, value):
        self.fixture.write_run(self.root, rid, series={"wer": [(12, value)]},
                               stagnation={"state": "ok", "seconds_since_anchor": 60.0})

    def test_lab_change_notice_repeat_quiet_for_each_harness(self):
        from harness_capabilities import parent_completion
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                sid = "lab-" + harness
                duty = SC.register(harness, sid, now=T0, store=self.store, ensure_runner=False)
                def step(now):
                    SC.advance(self.store.get(duty["id"]), self.store, now=now)
                    return self.store.get(duty["id"])["observation"]
                def ack():
                    claimed, _ = sweep.sweep_deliver(self.tmp, parent_completion(harness)["carrier"], sid)
                    self.assertEqual(sweep.ack_delivered(self.tmp, sid, claimed, acked_by="test"), 1)
                step(T0 + INTERVAL)
                ack()
                step(T0 + INTERVAL + 100)
                before = self.notify.call_count
                with mock.patch.object(self.board, "read", wraps=self.board.read) as read:
                    step(T0 + 2 * INTERVAL + 100)
                self.assertEqual(read.call_count, 1)  # two target cwd values, one root
                self.assertEqual(self.notify.call_count, before)
                self.fixture.write_run(self.root, "small", at=self.now - 1000,
                                       stagnation={"state": "ok", "seconds_since_anchor": 60.0})
                step(T0 + 3 * INTERVAL + 100)
                self.assertEqual(self.notify.call_count, before + 1)
                notice = self.notice()
                self.assertIn("LAB: 미확인 1·이름 없음 0·실패 0·정체 0", notice["text"])
                self.assertIn("지금 볼 것: AMI 가변 화자 개선 — 작은 모델", notice["text"])
                self.assertNotIn("20.4", notice["text"])
                ack()
                step(T0 + 3 * INTERVAL + 200)
                result = step(T0 + 4 * INTERVAL + 200)
                self.assertEqual(result["reason"], "quiet-no-change")
                self.assertEqual(self.notify.call_count, before + 1)
                self.write_run("small", 20.0)

    def test_real_board_stale_fresh_stale_does_not_repeat_notice(self):
        self.register()
        outcomes = []
        for index, age in enumerate((1200, 300, 1200, 300, 1200, 300)):
            self.fixture.write_run(self.root, "small", at=self.now - age,
                                   stagnation={"state": "ok", "seconds_since_anchor": 60.0})
            now = T0 + (index + 1) * (INTERVAL + 200)
            before = self.notify.call_count
            self.step(now)
            sent = self.notify.call_count > before
            outcomes.append(sent)
            if sent:
                self.deliver_and_ack()
                self.step(now + 100)
        self.assertEqual(outcomes, [True, False, False, False, False, False])

    def test_other_run_after_five_recoveries_wakes_once(self):
        self.register()
        outcomes = []
        ages = [(1200, 300)] + [(300, 300)] * 5 + [(300, 1200)] * 2
        for index, pair in enumerate(ages):
            for rid, age in zip(("small", "large"), pair):
                self.fixture.write_run(self.root, rid, at=self.now - age,
                                       stagnation={"state": "ok", "seconds_since_anchor": 60.0})
            self.board.clear_cache()
            now = T0 + (index + 1) * (INTERVAL + 200)
            before = self.notify.call_count
            self.step(now)
            sent = self.notify.call_count > before
            outcomes.append(sent)
            if sent:
                self.deliver_and_ack()
                self.step(now + 100)
        self.assertEqual(outcomes, [True, False, False, False, False, False, True, False])

    def walk_real(self, sid, actions):
        duty = SC.register(HARNESS, sid, now=T0, store=self.store, ensure_runner=False)
        outcomes = []
        for index, action in enumerate(actions):
            action()
            self.board.clear_cache()
            now = T0 + (index + 1) * (INTERVAL + 200)
            before = self.notify.call_count
            SC.advance(self.store.get(duty["id"]), self.store, now=now)
            sent = self.notify.call_count > before
            outcomes.append(sent)
            if sent:
                obs = self.store.get(duty["id"])["observation"]
                receipt = SC._notice_record(sid, obs["notice_delivery_id"])["receipt"]
                session_notice.validate(receipt)
                claimed, _ = sweep.sweep_deliver(self.tmp, CARRIER, sid)
                self.assertEqual(sweep.ack_delivered(self.tmp, sid, claimed, acked_by="test"), 1)
                SC.advance(self.store.get(duty["id"]), self.store, now=now + 100)
        return outcomes

    def test_real_reused_run_id_after_failure_or_disappearance_wakes_once(self):
        for ends in (True, False):
            with self.subTest(ends=ends):
                def write(started, age, status="running"):
                    self.fixture.write_run(self.root, "small", status=status, started=started,
                                           at=self.now - age,
                                           ended=self.now - age if status == "failed" else None,
                                           stagnation={"state": "ok", "seconds_since_anchor": 60.0})
                old = self.now - 7200
                new = self.now - 600
                actions = [lambda: write(old, 300), lambda: write(old, 1200)]
                if ends:
                    actions.append(lambda: write(old, 3000, "failed"))
                actions += [lambda: write(new, 120), lambda: write(new, 1200), lambda: None]
                outcomes = self.walk_real("reuse-" + str(ends), actions)
                self.assertEqual(outcomes, [True, True] + ([False] if ends else []) +
                                 [False, True, False])

    def test_real_vanish_return_same_run_is_quiet(self):
        path = self.root / "resource-runs" / "small.json"
        def stale():
            if path.exists():
                path.chmod(0o600)
            self.fixture.write_run(self.root, "small", started=self.now - 7200,
                                   at=self.now - 1200,
                                   stagnation={"state": "ok", "seconds_since_anchor": 60.0})
        try:
            outcomes = self.walk_real("vanish-one", [stale] + [action for _ in range(8)
                                      for action in (lambda: path.chmod(0), stale)])
        finally:
            path.chmod(0o600)
        self.assertEqual(outcomes, [True] + [False] * 16)

    def test_real_vanish_return_two_linked_runs_wakes_once(self):
        self.catalog.run_write(self.root, lambda ctx: self.catalog.op_link(
            ctx, "작은 모델", "small2"), now=self.now)
        self.fixture.write_run(self.root, "small", status="succeeded", started=self.now - 7200,
                               at=self.now - 4000, ended=self.now - 4000)
        path = self.root / "resource-runs" / "small2.json"
        for age in (120, 1200):
            with self.subTest(age=age):
                def readable():
                    if path.exists():
                        path.chmod(0o600)
                    self.fixture.write_run(self.root, "small2", started=self.now - 600,
                                           at=self.now - age,
                                           stagnation={"state": "ok", "seconds_since_anchor": 60.0})
                actions = [readable] + [a for _ in range(4)
                                       for a in (lambda: path.chmod(0), readable)]
                try:
                    outcomes = self.walk_real("vanish-" + str(age), actions)
                finally:
                    path.chmod(0o600)
                # Initial ordinary notice remembers small2 when stale; the first
                # missing read announces small once. Neither is forgotten later.
                self.assertEqual(outcomes, [True, True] + [False] * 7)
                print("real_vanish_return", age, outcomes)

    def test_real_tidy_80names_long_targets_wakes_once(self):
        root = self.fixture.make_root("tidy80")
        self.fixture.register(root, {"groups": [{"display_name": "가" * 80}], "conditions": [
            {"display_name": "나" * 80, "group": "가" * 80, "refs": {"runs": [{"run_id": "r"}]}}]})
        self.targets = [target("t" * 39 + str(i), state="done") for i in range(5)]
        self.rows[:] = [SimpleNamespace(harness=t["harness"], session_id=t["session_id"],
                                       cwd=str(root.parent)) for t in self.targets]
        def write(age):
            self.fixture.write_run(root, "r", at=self.now - age,
                                   stagnation={"state": "ok", "seconds_since_anchor": 60.0})
        outcomes = self.walk_real("tidy80", [lambda: write(300), lambda: write(1200)] +
                                  [lambda: None] * 5)
        self.assertEqual(outcomes, [True, True] + [False] * 5)
        text = self.notice()["text"]
        self.assertIn("LAB: 미확인 1·이름 없음 0·실패 0·정체 0", text)
        self.assertNotIn("지금 볼 것", text)
        self.assertIn("`peer-steward steward off`", text)
        print("real_tidy_80names_long_targets", outcomes, text)

    def test_six_lab_read_failures_preserve_other_observation(self):
        from fleet.collectors import project_status
        baseline = REAL_OBSERVE(HARNESS, SID)
        def without_lab(seen):
            return {k: v for k, v in seen.items() if k not in ("lab", "lab_roots")}
        patches = [
            mock.patch.object(self.board, "read", side_effect=RuntimeError("broken")),
            mock.patch.object(project_status, "artifact_root", side_effect=OSError("broken")),
            mock.patch.dict(sys.modules, {"experiment_board": None}),
            mock.patch.object(self.board, "read", return_value=({"visible": True}, []))]
        for patch in patches:
            with patch:
                seen = REAL_OBSERVE(HARNESS, SID)
                self.assertEqual(seen["lab"], {})
                self.assertEqual(without_lab(seen), without_lab(baseline))
        path = self.catalog.catalog_path(self.root)
        path.chmod(0)
        try:
            self.board.clear_cache()
            seen = REAL_OBSERVE(HARNESS, SID)
            self.assertEqual(seen["lab"], {})
            self.assertEqual(without_lab(seen), without_lab(baseline))
        finally:
            path.chmod(0o600)
        path.write_text(path.read_text() + " " * (1024 * 1024))
        self.board.clear_cache()
        seen = REAL_OBSERVE(HARNESS, SID)
        self.assertEqual(seen["lab"], {})
        self.assertEqual(without_lab(seen), without_lab(baseline))
        self.register()
        with mock.patch.object(self.board, "read", side_effect=RuntimeError("broken")):
            self.step(T0 + INTERVAL)
        self.assertEqual(self.obs()["phase"], "notified")

    def test_only_new_values_even_new_best_name_do_not_change_signature(self):
        self.assertEqual(self.board.read(self.root)[0]["groups"][0]["headline"]["name"], "작은 모델")
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.write_run("large", 18.0)  # changes board headline from small to large
        self.assertEqual(self.board.read(self.root)[0]["groups"][0]["headline"]["name"], "큰 모델")
        self.assertEqual(REAL_OBSERVE(HARNESS, SID)["lab"][str(self.root)]["groups"][0]["headline_name"], "큰 모델")
        result = self.step(T0 + 2 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(result["observation"]["reason"], "quiet-no-change")

    def test_missing_root_catalog_and_broken_catalog_keep_existing_observation(self):
        for kind in ("missing-root", "missing-catalog", "broken"):
            with self.subTest(kind=kind):
                root = self.fixture.base / kind / ".agent_reports"
                root.parent.mkdir()
                if kind != "missing-root":
                    root.mkdir()
                if kind == "broken":
                    path = self.catalog.catalog_path(root)
                    path.parent.mkdir(parents=True)
                    path.write_text("{broken")
                for row in self.rows:
                    row.cwd = str(root.parent)
                seen = REAL_OBSERVE(HARNESS, SID)
                self.assertFalse(seen.get("lab"))
                self.assertEqual(SC._classify(seen), "tidy-and-release")
                old = {k: v for k, v in seen.items() if k not in ("lab", "lab_roots")}
                self.assertEqual(SC._signature("tidy-and-release", seen), SC._signature("tidy-and-release", old))

    def test_counts_from_board_and_unlinked_changes_are_observed(self):
        self.fixture.write_run(self.root, "small", status="failed")
        self.fixture.write_run(self.root, "large", stagnation={"state": "stagnant", "seconds_since_anchor": 9000.0})
        self.write_run("unlinked", 12.3)
        seen = REAL_OBSERVE(HARNESS, SID)
        lab = seen["lab"][str(self.root)]
        self.assertEqual({key: lab[key] for key in ("failed", "stagnant", "unknown", "unlinked")},
                         {"failed": 1, "stagnant": 1, "unknown": 0, "unlinked": 1})
        self.register()
        self.step(T0 + INTERVAL)
        self.assertIn("이름 없음 1", self.notice()["text"])
        self.assertIn("지금 볼 것: AMI 가변 화자 개선 — 작은 모델", self.notice()["text"])
        self.write_run("unlinked-two", 11.0)
        after = REAL_OBSERVE(HARNESS, SID)
        self.assertEqual(SC._signature("tidy-and-release", seen), SC._signature("tidy-and-release", after))

    def test_multiple_roots_and_notice_budget(self):
        second = self.fixture.make_root("second")
        spec = {"groups": [{"display_name": "둘째 묶음"}], "conditions": [
            {"display_name": "미확인 조건", "group": "둘째 묶음", "refs": {"runs": [{"run_id": "missing"}]}}]}
        self.fixture.register(second, spec)
        self.rows[1].cwd = str(second.parent)
        with mock.patch.object(self.board, "read", wraps=self.board.read) as read:
            seen = REAL_OBSERVE(HARNESS, SID)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(set(seen["lab"]), {str(self.root), str(second)})
        self.register()
        self.step(T0 + INTERVAL)
        self.assertIn("지금 볼 것: 둘째 묶음 — 미확인 조건", self.notice()["text"])
        self.assertLessEqual(len(self.notice()["text"]), session_notice.MAX_TEXT)

    def test_foreign_registry_rows_are_never_read(self):
        self.rows.append(SimpleNamespace(harness="claude", session_id="foreign", cwd="/not-a-target"))
        from fleet.collectors import project_status
        with mock.patch.object(project_status, "artifact_root", wraps=project_status.artifact_root) as resolve:
            REAL_OBSERVE(HARNESS, SID)
        self.assertEqual([call.args[0] for call in resolve.call_args_list], [str(self.root.parent)])


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

    def test_finished_targets_do_not_wake_again_when_only_own_records_change(self):
        # Every target ended; the holder keeps working on its own open records, which
        # change between checks. Nobody is left to watch, so after the one release
        # reminder the check does not open another turn (BC-SUPERVISOR-CONTEXT-COST-1114).
        ended = [target(name, state="idle", liveness="ended") for name in ("a", "b", "c", "d")]
        self.seen = {"targets": ended, "pending_messages": 0, "open_work": ["#1 own record"]}
        self.register()
        self.step(T0 + INTERVAL)
        self.assertEqual(self.notify.call_count, 1)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        for index, records in enumerate((["#1 own record", "#2 new record"], ["#2 new record"],
                                         ["#3 another"]), start=2):
            self.seen = {"targets": ended, "pending_messages": index, "open_work": records,
                         "user_decisions": [f"decision {index}"]}
            self.step(T0 + index * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.obs()["reason"], "quiet-no-change")
        # A target that comes back to life is watched again.
        self.seen = {"targets": [target("a", state="working"), *ended[1:]], "pending_messages": 0}
        self.step(T0 + 5 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 2)

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


class StewardCheckUnresolvedTest(CheckBase):
    """RV-PR1593: retain unresolved targets without paying for unchanged dead work."""

    def observe(self, liveness, sid="target-unresolved"):
        collector = SimpleNamespace(
            _peer_message_module=lambda: None, _registry_sessions=lambda: [],
            role_targets=lambda *a, **k: [],
            unresolved_role_targets=lambda *a, **k: [{
                "harness": "claude", "session_id": sid, "name": "helper",
                "source": "watch", "unresolved": True}],
            session_liveness=lambda *a, **k: liveness)
        with mock.patch.object(SC, "_collector", return_value=collector), \
                mock.patch.object(SC, "_end_observation", return_value=None), \
                mock.patch.object(SC, "_session_liveness", REAL_SESSION_LIVENESS), \
                mock.patch.object(SC, "_open_work", return_value=([], [])), \
                mock.patch("peer_steward_native.native_agent", return_value=None):
            self.seen = REAL_OBSERVE(HARNESS, SID)
        return self.seen["targets"][0]

    def test_ended_unjoined_target_lets_the_role_go_quiet(self):
        row = self.observe("ended")
        self.assertTrue(SC._quiet(self.seen), self.seen)  # reviewer's original probe
        self.assertEqual(row["liveness"], "ended")
        self.assertTrue(row["unresolved"])
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        checked = T0 + INTERVAL + 100
        self.assertIn("target name=helper harness=claude state=unknown liveness=ended",
                      self.check(checked))
        for cycle in (1, 2, 3):
            self.step(checked + cycle * INTERVAL)
        self.assertEqual(self.notify.call_count, 1)

    def test_live_unjoined_target_repeats_each_interval(self):
        row = self.observe("live")
        self.assertEqual(row["liveness"], "live")
        self.assertFalse(SC._quiet(self.seen))
        self.register()
        for cycle in (1, 2, 3):
            now = T0 + cycle * (INTERVAL + 100)
            self.step(now)
            self.assertEqual(self.notify.call_count, cycle)
            self.assertEqual(self.notice()["required_action"], "run-steward-check")
            self.deliver_and_ack()
            self.step(now + 100)

    def test_unknown_unjoined_target_notifies_once_per_changed_state(self):
        self.observe("unknown", sid=None)
        self.assertTrue(SC._quiet(self.seen))
        self.register()
        self.step(T0 + INTERVAL)
        self.deliver_and_ack()
        self.step(T0 + INTERVAL + 100)
        self.step(T0 + 2 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 1)
        self.observe("ended")
        self.step(T0 + 3 * INTERVAL + 100)
        self.assertEqual(self.notify.call_count, 2)
        self.deliver_and_ack()
        self.step(T0 + 3 * INTERVAL + 200)
        self.step(T0 + 4 * INTERVAL + 200)
        self.assertEqual(self.notify.call_count, 2)


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
            role_targets=lambda h, s, **kw: [{"name": "e7", "harness": "codex", "session_id": "sid-e7"}],
            _registry_sessions=lambda: [], _peer_message_module=lambda: module)
        with mock.patch.object(SC, "_collector", return_value=collector), \
                mock.patch.object(SC, "_agents", return_value=[]), \
                mock.patch.object(SC, "_end_observation", return_value=None), \
                mock.patch.object(SC, "_session_liveness", return_value="live"), \
                mock.patch.object(SC, "_open_work", return_value=(["w"], [])):
            seen = SC._observe_role("claude", SID)
        self.assertEqual(seen["targets"][0]["owed"], 1)
        self.assertEqual(seen["pending_messages"], 0)
        self.assertEqual(seen["open_work"], ["w"])


class StewardCheckPeerRequestCountTest(CheckBase):
    """Only a request another session sent and the holder has not received is a
    "peer request queued for you"; runtime notices (route completions, experiment
    finals, this check's own notice) never are. Rows keep the real shape the
    peer-message store gives them, per receiving harness."""

    # The receipt a notice's carrier row keeps differs per adapter: Claude's hook
    # releases its in-flight reservation, Codex and OpenCode leave the native queue's
    # ``native-pending`` row.
    RECEIPTS = {"claude": "delivery-reservation-released",
                "codex": "native-pending", "opencode": "native-pending"}

    def setUp(self):
        super().setUp()
        self.rows = []
        module = SimpleNamespace(pending_messages=lambda who: [
            row for row in self.rows
            if (row["to"]["harness"], row["to"]["session_id"]) == (who["harness"], who["session_id"])])
        self.collector = SimpleNamespace(role_targets=lambda h, s, **kw: [],
                                         _registry_sessions=lambda: [],
                                         _peer_message_module=lambda: module)
        for name, patch in (
                ("_observe_role", mock.patch.object(SC, "_observe_role", REAL_OBSERVE)),
                ("_collector", mock.patch.object(SC, "_collector", return_value=self.collector)),
                ("_end_observation", mock.patch.object(SC, "_end_observation", return_value=None)),
                ("_open_work", mock.patch.object(SC, "_open_work", return_value=([], [])))):
            patch.start()
            self.addCleanup(patch.stop)

    def row(self, harness, sid, *, sender="runtime", state="pending", receipt=None, text="x"):
        binding = ({"schema": "session-notice-runtime-v1"} if sender == "runtime" else None)
        row = {"ref": f"{len(self.rows):032x}", "state": state, "text": text,
               "from": {"harness": sender, "session_id": None if sender == "runtime" else "sid-other"},
               "to": {"harness": harness, "session_id": sid},
               "receipt": receipt or self.RECEIPTS[harness], "producer_binding": binding}
        self.rows.append(row)
        return row

    def test_only_runtime_notices_pending_is_not_a_peer_request(self):
        for harness, receipt in self.RECEIPTS.items():
            with self.subTest(harness=harness):
                self.rows.clear()
                for kind in ("route completion", "experiment final", "steward-check: 12 peer request(s)"):
                    self.row(harness, SID, text=kind)
                self.row(harness, SID, state="queued", receipt="claude-pty-continue-submitted")
                seen = REAL_OBSERVE(harness, SID)
                self.assertEqual(seen["pending_messages"], 0)
                self.assertNotIn("queued for you", " ".join(SC.next_actions(seen, {}, T0)))
                self.assertFalse(SC._remaining(seen))

    def test_running_the_check_repeatedly_never_grows_the_count(self):
        for harness in self.RECEIPTS:
            with self.subTest(harness=harness):
                self.rows.clear()
                for index in range(6):
                    out = self.check_as(harness, T0 + index * 3600)
                    self.assertNotIn("queued for you", out)
                    self.assertNotIn("pending_messages=1", out)
                    # the courier leaves this check's own notice pending, as observed
                    self.row(harness, SID, text="steward-check: notice %d" % index)
                self.assertEqual(len(self.rows), 6)
                self.assertEqual(REAL_OBSERVE(harness, SID)["pending_messages"], 0)

    def check_as(self, harness, now):
        out = io.StringIO()
        with mock.patch.object(SC, "_role_held", return_value={"held": True, "reason": None,
                                                                "successor": None}):
            SC.cmd_check(harness, SID, now=now, out=out)
        return out.getvalue()

    def test_one_undelivered_request_from_another_session_counts_once(self):
        for harness in self.RECEIPTS:
            with self.subTest(harness=harness):
                self.rows.clear()
                self.row(harness, SID, text="route completion")
                self.row(harness, SID, sender="claude", text="please review")
                self.row(harness, SID, sender="codex", state="received", text="old")
                seen = REAL_OBSERVE(harness, SID)
                self.assertEqual(seen["pending_messages"], 1)
                self.assertIn("1 peer request(s) queued for you → handle them as they arrive",
                              SC.next_actions(seen, {}, T0))
                out = self.check_as(harness, T0)
                self.assertIn("pending_messages=1", out)
                self.assertIn("1 peer request(s) queued for you", out)

    def test_runtime_notice_to_a_target_is_not_a_request_owed_by_it(self):
        self.collector.role_targets = lambda h, s, **kw: [
            {"name": "e7", "harness": "codex", "session_id": "sid-e7"}]
        self.row("codex", "sid-e7", text="route completion")
        with mock.patch.object(SC, "_session_liveness", return_value="live"):
            seen = REAL_OBSERVE("claude", SID)
        self.assertEqual(seen["targets"][0]["owed"], 0)
        self.row("codex", "sid-e7", sender="claude", text="do this")
        with mock.patch.object(SC, "_session_liveness", return_value="live"):
            seen = REAL_OBSERVE("claude", SID)
        self.assertEqual(seen["targets"][0]["owed"], 1)


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


HERDR_LOGGER = """#!/bin/sh
echo "$@" >> "$FAKE_HERDR_CALLLOG"
case "$*" in
  *"agent list"*) cat "$FAKE_HERDR_AGENTS"; exit 0 ;;
esac
exit 1
"""


class _NativeStewardWorld(CheckBase):
    """A steward marker and native registry records in one repository, read for real."""

    HOLDER = "aaaaaaaa-1111-4111-8111-000000000001"
    WORKER = "bbbbbbbb-1111-4111-8111-000000000002"
    HELPER = "cccccccc-1111-4111-8111-000000000003"

    def setUp(self):
        super().setUp()
        from fleet.collectors import steward
        import recipient_lifetime
        census = recipient_lifetime.RecipientCensus(lambda harness, sid: "alive")
        for target_module, name, replacement in (
                (SC, "_session_liveness", REAL_SESSION_LIVENESS),
                (SC, "_role_flagged", REAL_ROLE_FLAGGED),
                (SC, "_observe_role", REAL_OBSERVE),
                (steward, "end_observation", lambda: _StubObservation(claude=census))):
            patch = mock.patch.object(target_module, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)
        # PATH without any real `herdr`: "absent" must mean absent on this host too.
        self.path = os.pathsep.join(
            part for part in os.environ.get("PATH", "").split(os.pathsep)
            if part and not os.path.exists(os.path.join(part, "herdr")))

    def world(self, name, *, herdr_answers):
        root = self.tmp / name
        repo = root / "repo"
        (repo / ".git").mkdir(parents=True)
        ledger, claude, bindir = root / "ledger", root / "claude", root / "bin"
        bindir.mkdir()
        (root / "jobs.log").write_text("")
        log = root / "herdr-calls.log"
        if herdr_answers:
            agents = root / "agents.json"
            agents.write_text(json.dumps({"result": {"agents": [{
                "agent": "claude", "agent_session": {"value": self.HELPER},
                "cwd": str(repo), "pane_id": "w1:p2", "name": "helper"}]}}))
            shim = bindir / "herdr"
            shim.write_text(HERDR_LOGGER)
            shim.chmod(0o755)
        ts = "2026-11-13T00:00:00Z"
        marker = ledger / "peer-steward" / "claude" / f"{self.HOLDER}.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({
            "schema_version": 1, "harness": "claude", "session_id": self.HOLDER,
            "generation": "gen-holder", "updated": ts, "targets": {
                "worker": {"harness": "claude", "session_id": self.WORKER, "name": "worker",
                           "kind": "watch", "source": "watch", "ts": ts},
                "helper": {"harness": "claude", "session_id": None, "name": "helper",
                           "kind": "watch", "source": "watch", "ts": ts}}}))
        sessions = claude / "sessions"
        sessions.mkdir(parents=True)
        for pid, sid in ((4194301, self.HOLDER), (4194302, self.WORKER)):
            (sessions / f"{pid}.json").write_text(json.dumps(
                {"pid": pid, "sessionId": sid, "cwd": str(repo)}))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("HERDR_", "AGENT_HERDR", "TMUX", "CODEX_"))}
        env.update(PATH=f"{bindir}{os.pathsep}{self.path}", HOME=str(root / "home"),
                   CLAUDE_CONFIG_DIR=str(claude), CODEX_HOME=str(root / "codex"),
                   FLEET_SESSION_REGISTRY_DIR=str(root / "registry"),
                   XDG_CONFIG_HOME=str(root / "xdg-config"), XDG_DATA_HOME=str(root / "xdg-data"),
                   XDG_STATE_HOME=str(root / "xdg-state"),
                   AGENT_PEER_LEDGER_ROOT=str(ledger), AGENT_DISPATCH_JOBS=str(root / "jobs.log"),
                   FAKE_HERDR_CALLLOG=str(log), FAKE_HERDR_AGENTS=str(root / "agents.json"))
        return env, ledger, log


class StewardCheckHerdrReachTest(_NativeStewardWorld):
    """`steward check` and the runner's steward-check duty never ask Herdr.

    The same native world is built twice: once with a logging `herdr` first on PATH that
    answers `agent list` with an extra same-repository agent, once with no `herdr` at all.
    Each case ends by asserting that the logger saw no call and that both worlds give the
    same check output and duty steps."""

    def run_world(self, name, *, herdr_answers):
        env, ledger, log = self.world(name, herdr_answers=herdr_answers)
        SC._THROTTLE.clear()
        self.notify.reset_mock()
        with mock.patch.dict(os.environ, env, clear=True):
            store = peer_obligations.ObligationStore(ledger)
            out = io.StringIO()
            self.assertEqual(SC.cmd_check(HARNESS, self.HOLDER, now=T0, out=out), 0)
            duty_id = SC.duty_id(HARNESS, self.HOLDER)
            SC.advance(store.get(duty_id), store, now=T0 + INTERVAL)  # due: notice sent
            due = store.get(duty_id)["observation"]
            state = SC._notice_record(self.HOLDER, due["notice_delivery_id"])["state"]
            SC.advance(store.get(duty_id), store, now=T0 + INTERVAL + 100)  # open notice
            after = store.get(duty_id)
        calls = log.read_text().splitlines() if log.exists() else []
        notice = self.notify.call_args.kwargs if self.notify.call_args else {}
        return calls, {"check": out.getvalue(), "due_phase": due["phase"],
                       "notice_state": state, "notice_text": notice.get("text"),
                       "notice_action": notice.get("required_action"),
                       "after": (after["state"], after["observation"]["phase"])}

    def test_check_and_duty_never_ask_herdr_and_match_without_herdr(self):
        answered_calls, answered = self.run_world("answering", herdr_answers=True)
        absent_calls, absent = self.run_world("absent", herdr_answers=False)
        self.assertEqual(answered_calls, [])
        self.assertEqual(absent_calls, [])
        self.assertEqual(answered, absent)
        names = re.findall(r"^target name=(\S+) .*liveness=(\S+)", absent["check"], re.M)
        self.assertEqual(names, [("worker", "live"), ("helper", "unknown")], absent["check"])
        self.assertIn("targets=2 ", absent["check"])
        self.assertEqual((absent["due_phase"], absent["notice_state"]), ("notified", "pending"))
        self.assertEqual(absent["after"], ("pending", "notified"))


class StewardCheckWorkerStartedRunnerTest(_NativeStewardWorld):
    """The shared runner is one service of this user: a registered worker's hook may start
    it, and the steward-check duty it runs still reads the user's own native records.

    A worker masks its own harness home (Claude ``CLAUDE_CONFIG_DIR``, Codex
    ``CODEX_HOME``) with a private home and pins the user's in
    ``HEARTING_MODEL_CONFIG_HOME``. The runner's environment is taken from the real
    launch, then the duty's own seams run in it."""

    CODEX_TARGET = "019a0000-0000-7000-8000-0000000000dd"

    def runner_environment(self, starter, ledger):
        with mock.patch.dict(os.environ, starter, clear=True):
            store = peer_obligations.ObligationStore(ledger)
            SC.register(HARNESS, self.HOLDER, now=T0, store=store, ensure_runner=False)
            with mock.patch.object(peer_obligations.subprocess, "Popen") as launch:
                self.assertTrue(REAL_ENSURE_NORMAL_RUNNER(ledger))
            env = launch.call_args.kwargs["env"]
            return dict(os.environ) if env is None else dict(env)

    def worker_home(self, root, name):
        home = root / "dispatch" / "homes" / "workers" / name
        (home / "sessions").mkdir(parents=True)
        (home / "worker-home.json").write_text("{}", encoding="utf-8")
        return home

    def observed_in(self, env):
        SC._THROTTLE.clear()
        with mock.patch.dict(os.environ, env, clear=True):
            flagged = REAL_ROLE_FLAGGED(HARNESS, self.HOLDER)
            names = sorted(target["name"] for target in SC._observe_role(HARNESS, self.HOLDER)["targets"])
        return flagged, names

    def test_a_runner_a_claude_worker_starts_reads_the_users_claude_home(self):
        env, ledger, log = self.world("claude-worker", herdr_answers=False)
        worker = self.worker_home(self.tmp / "claude-worker", "claude-owner")
        starter = {**env, "CLAUDE_CONFIG_DIR": str(worker), "HEARTING_WORKER_HOME": str(worker),
                   "HEARTING_MODEL_CONFIG_HOME": env["CLAUDE_CONFIG_DIR"]}
        runner = self.runner_environment(starter, ledger)
        self.assertEqual(self.observed_in(runner), (True, ["helper", "worker"]))
        self.assertEqual(runner.get("CLAUDE_CONFIG_DIR"), env["CLAUDE_CONFIG_DIR"])
        for name in ("HEARTING_WORKER_HOME", "HEARTING_MODEL_CONFIG_HOME"):
            self.assertNotIn(name, runner)
        self.assertFalse(log.exists())

    def test_a_default_claude_home_stays_the_native_default(self):
        env, ledger, _log = self.world("claude-default", herdr_answers=False)
        default = Path(env["HOME"]) / ".claude"
        default.parent.mkdir(parents=True)
        Path(env["CLAUDE_CONFIG_DIR"]).rename(default)
        worker = self.worker_home(self.tmp / "claude-default", "claude-owner")
        starter = {**env, "CLAUDE_CONFIG_DIR": str(worker), "HEARTING_WORKER_HOME": str(worker),
                   "HEARTING_MODEL_CONFIG_HOME": str(default)}
        runner = self.runner_environment(starter, ledger)
        self.assertNotIn("CLAUDE_CONFIG_DIR", runner)
        self.assertEqual(self.observed_in(runner), (True, ["helper", "worker"]))

    def test_a_runner_a_codex_worker_starts_reads_the_users_codex_threads(self):
        import sqlite3
        env, ledger, _log = self.world("codex-worker", herdr_answers=False)
        marker = ledger / "peer-steward" / "claude" / f"{self.HOLDER}.json"
        record = json.loads(marker.read_text())
        record["targets"]["codex"] = {"harness": "codex", "session_id": self.CODEX_TARGET,
                                      "name": "codex-worker", "kind": "watch", "source": "watch",
                                      "ts": record["updated"]}
        marker.write_text(json.dumps(record))
        repo = self.tmp / "codex-worker" / "repo"
        user_codex = Path(env["CODEX_HOME"])
        worker = self.worker_home(self.tmp / "codex-worker", "codex-owner")
        for home, rows in ((user_codex, [(self.CODEX_TARGET, str(repo))]), (worker, [])):
            home.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(home / "state_5.sqlite") as con:
                con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT)")
                con.executemany("INSERT INTO threads VALUES (?, ?)", rows)
            con.close()
        starter = {**env, "CODEX_HOME": str(worker), "HEARTING_WORKER_HOME": str(worker),
                   "HEARTING_MODEL_CONFIG_HOME": str(user_codex)}
        runner = self.runner_environment(starter, ledger)
        self.assertEqual(self.observed_in(runner), (True, ["codex-worker", "helper", "worker"]))
        self.assertEqual(runner.get("CODEX_HOME"), str(user_codex))
        self.assertEqual(runner.get("CLAUDE_CONFIG_DIR"), env["CLAUDE_CONFIG_DIR"])

    def test_an_ordinary_session_keeps_its_own_claude_home(self):
        env, ledger, _log = self.world("ordinary", herdr_answers=False)
        other = self.tmp / "ordinary" / "chosen-claude"
        (other / "sessions").mkdir(parents=True)
        runner = self.runner_environment({**env, "CLAUDE_CONFIG_DIR": str(other)}, ledger)
        self.assertEqual(runner.get("CLAUDE_CONFIG_DIR"), str(other))


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

    def _collector_with(self, **functions):
        from types import SimpleNamespace
        return mock.patch.object(SC, "_collector", return_value=SimpleNamespace(**functions))

    def test_an_unflagged_duty_names_why_it_is_unflagged(self):
        # #1576 review follow-up: the observation said only "no-steward-flag".
        def unreadable():
            raise OSError("registry unreadable")
        with self._collector_with(_registry_sessions=unreadable):
            self.assertEqual(SC._role_flag_cause(HARNESS, SID), "native-records-unreadable")
        with self._collector_with(_registry_sessions=lambda: [], role_targets=lambda *a, **k: []):
            self.assertEqual(SC._role_flag_cause(HARNESS, SID), "no-targets")
        with self._collector_with(_registry_sessions=lambda: [],
                                  role_targets=lambda *a, **k: [{"name": "x"}]):
            self.assertEqual(SC._role_flag_cause(HARNESS, SID), "foreign-repository-only")
        self.register()
        self.flagged = False
        with self._collector_with(_registry_sessions=lambda: [], role_targets=lambda *a, **k: []):
            duty = self.step(T0 + INTERVAL)
        self.assertEqual(duty["observation"]["reason"], "no-steward-flag")
        self.assertEqual(duty["observation"]["cause"], "no-targets")

    def test_targets_named_by_a_shared_session_prefix_stay_distinguishable(self):
        targets = [{"name": "abcdefgh", "session_id": "abcdefgh-1111"},
                   {"name": "abcdefgh", "session_id": "abcdefgh-2222"},
                   {"name": "zzzzzzzz", "session_id": "zzzzzzzz-1"},
                   {"name": "worker", "session_id": "abcdefgh-3333"}]
        SC._lengthen_shared_prefixes(targets)
        names = [t["name"] for t in targets]
        self.assertEqual(len(set(names)), 4, names)
        self.assertEqual(names[2:], ["zzzzzzzz", "worker"])

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
        rows = [SimpleNamespace(harness=HARNESS, session_id=SID, cwd="/repo", runtime_name=None)]
        collector = SimpleNamespace(role_flag=mock.Mock(return_value=False),
                                    _registry_sessions=mock.Mock(return_value=rows),
                                    role_targets=mock.Mock(return_value=[]))
        with mock.patch.object(SC, "_collector", return_value=collector):
            self.assertFalse(REAL_ROLE_FLAGGED(HARNESS, SID))
        collector.role_targets.assert_called_once_with(HARNESS, SID, sessions=rows,
                                                       include_native=True)
        collector.role_flag.assert_called_once_with(HARNESS, SID, sessions=rows, _targets=[])
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



class LabRegressionTest(CheckBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        patch.start()
        self.addCleanup(patch.stop)
        self.seen["targets"] = [target(state="done")]
        self.clock = T0
        self.register()

    def labs(self, unknown=0, failed=0, stagnant=0, unlinked=0, groups=None):
        groups = groups or [dict(name="AMI", headline_name="작은 모델", unknown=unknown,
                                 failed=failed, stagnant=stagnant)]
        groups = [dict(g, rows=g.get("rows", [
            dict(condition_id=g["name"] + str(i), run_id="run-" + str(i),
                 unknown_flag=i < g["unknown"]) for i in range(12)])) for g in groups]
        # Linked healthy rows keep these run identities present during 1→0→1.
        groups[0]["rows"] += [dict(condition_id="linked-" + str(i), run_id="loose-" + str(i),
                                  unknown_flag=False) for i in range(3)]
        return {"/synthetic": dict(groups=groups, unlinked=unlinked,
            unlinked_run_ids=["loose-" + str(i) for i in range(unlinked)],
            **{key: sum(g[key] for g in groups) for key in ("unknown", "failed", "stagnant")})}

    def tick(self, lab):
        self.clock += INTERVAL + 100
        self.seen["lab"] = lab
        before = self.notify.call_count
        self.step(self.clock)
        if self.obs()["phase"] == "notified":
            self.deliver_and_ack()
            self.step(self.clock + 100)
        return self.notify.call_count - before

    def test_korean_receipt_delivers_with_lab(self):
        self.seen["targets"] = [target()]
        self.seen["open_work"] = [
            "실험 보고 가독성 사단계로 카드와 주입에서 정본 이름을 쓰고 모든 세션이 시작할 때 정본을 발견하게 하며 남은 경고 문구도 함께 고치기",
            "셸 패널 실험 구획과 여덟 칸 띠를 설치 전 화면으로 확인하고 사용자에게 보여 줄 시안을 고른 뒤 접기와 펼치기 동작까지 맞추기",
            "자식 실패 알림이 쉬고 있는 감독을 깨우지 않는 문제의 세 번째 단계 정정과 수신처 인계를 마무리하고 재현 시험을 남기기",
            "감독 정정 묶음에서 런타임 종료 처리와 남은 자식 정리 그리고 사용자 결정 대기 항목을 따로 나누어 보고서에 적어 두기"]
        groups = [dict(name=name, headline_name=head, unknown=1, failed=0, stagnant=0)
                  for name, head in (("AMI 가변 화자 개선", "작은 모델"),
                                     ("VoxConverse 일반화 확인", "큰 모델 기준선 재현"))]
        sid = "0199a1b2-c3d4-7e5f-8a9b-123456789012"
        duty = SC.register(HARNESS, sid, now=T0, store=self.store, ensure_runner=False)
        self.seen["lab"] = self.labs(groups=groups)
        SC.advance(self.store.get(duty["id"]), self.store, now=T0 + INTERVAL)
        obs = self.store.get(duty["id"])["observation"]
        self.assertEqual(obs["phase"], "notified", obs)
        receipt = SC._notice_record(sid, obs["notice_delivery_id"])["receipt"]
        self.assertLessEqual(len(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()), 2048)
        self.assertIn("`peer-steward steward check`", receipt["text"])

    def test_totals_and_one_new_unknown_name_pair_with_intact_advice(self):
        self.seen["open_work"] = [f"task {i} " + "x" * 55 for i in range(4)]
        groups = [dict(name=f"healthy-{i}", headline_name="best-model-" + "x" * 20,
                       unknown=0, failed=0, stagnant=0) for i in range(4)]
        groups.append(dict(name="attention-group", headline_name="missing-condition",
                           unknown=12, failed=0, stagnant=0))
        self.tick(self.labs(groups=groups))
        text = self.record()["receipt"]["text"]
        self.assertIn("`peer-steward steward check`", text)
        self.assertIn("지금 볼 것: attention-group — missing-condition", text)
        self.assertIn("미확인 12", text)
        self.assertIn("missing-condition", text)
        self.assertIn("(묶음 5개)", text)
        self.assertNotIn("외 ", text)
        self.assertNotIn("…", text)

    def test_runtime_failure_and_stagnation_display_without_waking(self):
        self.tick(self.labs())
        for field in ("failed", "stagnant"):
            with self.subTest(field=field):
                labs = self.labs(**{field: 1})
                self.assertEqual(self.tick(labs), 0)
                self.assertIn("1", SC._lab_summary({"lab": labs}, [])[0])

    def test_count_only_notice_and_failed_delivery_do_not_poison_retry(self):
        self.seen["lab"] = self.labs(groups=[dict(name="가" * 180, headline_name="나" * 80,
                                                unknown=12, failed=0, stagnant=0)])
        self.seen["open_work"] = ["가" * 60 + str(i) for i in range(4)]
        with mock.patch.object(session_notice, "notify", return_value=None):
            self.step(T0 + INTERVAL)
        self.assertEqual(self.obs()["reason"], "notice-unavailable")
        self.assertNotIn("lab_notified", self.obs())
        self.step(T0 + 2 * INTERVAL)
        self.assertEqual(self.obs()["phase"], "notified")
        receipt = self.record()["receipt"]
        self.assertIn("LAB: 미확인 12·이름 없음 0·실패 0·정체 0", receipt["text"])
        self.assertIn("`peer-steward steward check`", receipt["text"])

    def test_oversized_names_use_counts_and_remember_all_runs(self):
        lab = self.labs(groups=[dict(name="가" * 180, headline_name="나" * 80,
                                   unknown=1, failed=0, stagnant=0)])
        self.assertEqual(self.tick(lab), 1)
        self.assertIn("LAB: 미확인 1·이름 없음 0·실패 0·정체 0", self.notice()["text"])
        self.assertNotIn("지금 볼 것", self.notice()["text"])
        self.assertEqual(len(self.obs()["lab_notified"]), 1)
        self.assertEqual(self.tick(lab), 0)
        lab["/synthetic"]["groups"][0].update(name="짧은 묶음", headline_name="run A")
        self.assertEqual(self.tick(lab), 0)

    def test_korean_actions_without_lab_fit_receipt(self):
        self.seen["open_work"] = ["한" * 69 + str(i) for i in range(4)]
        self.assertEqual(self.tick({}), 1)
        self.assertEqual(self.obs()["reason"], "notice-acked")
        self.assertIn("more", self.notice()["text"])
        session_notice.validate({"schema_version": 1, "kind": "notice",
            **{k: self.notice()[k] for k in ("key", "subject", "text", "required_action")}})

    def test_receipt_size_sweep_with_and_without_lab(self):
        sizes = []
        for ko in range(20, 71, 5):
            self.seen["open_work"] = [chr(ord("가") + i) * ko for i in range(4)]
            for count in (0, 2, 3, 5):
                groups = [dict(name="묶음" + str(i), headline_name="모델" + str(i),
                               unknown=1, failed=0, stagnant=0) for i in range(count)]
                self.seen["lab"] = self.labs(groups=groups) if groups else {}
                sid = f"0199a1b2-c3d4-7e5f-8a9b-{ko:06d}{count:06d}"
                duty = SC.register(HARNESS, sid, now=T0, store=self.store, ensure_runner=False)
                SC.advance(self.store.get(duty["id"]), self.store, now=T0 + INTERVAL)
                obs = self.store.get(duty["id"])["observation"]
                self.assertEqual(obs["phase"], "notified", (ko, count, obs))
                receipt = SC._notice_record(sid, obs["notice_delivery_id"])["receipt"]
                session_notice.validate(receipt)
                text = receipt["text"]
                self.assertIn("`peer-steward steward check`", text)
                self.assertEqual(" | LAB: " in text, count > 0)
                if count:
                    self.assertIn(f"미확인 {count}·이름 없음 0·실패 0·정체 0 (묶음 {count}개)", text)
                omitted = sum(work not in text for work in self.seen["open_work"])
                if omitted:
                    self.assertIn(f"+{omitted} more", text)
                for fragment in text.split("Do these now: ")[1].split(". Then run")[0].split("; "):
                    if not fragment.startswith("+"):
                        self.assertIn(fragment, SC.next_actions(self.seen))
                sizes.append((len(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()),
                              len(receipt["text"])))
        print("receipt-size-sweep", len(sizes), "max-bytes", max(x[0] for x in sizes),
              "max-chars", max(x[1] for x in sizes))

    def test_departed_and_unreadable_roots_keep_announced_identities(self):
        lab = self.labs(unknown=1)
        self.tick(lab)
        before = self.obs()["lab_notified"]
        self.assertEqual(self.tick({}), 0)
        self.assertEqual(self.obs()["lab_notified"], before)
        self.assertEqual(self.tick({}), 0)
        self.assertEqual(self.obs()["lab_notified"], before)
        self.assertEqual(self.tick(lab), 0)

    def test_new_unknown_group_precedes_runtime_failures(self):
        failed = [dict(name="실패 묶음 " + "가" * 30 + str(i), headline_name="나" * 30,
                       unknown=0, failed=1, stagnant=0) for i in range(4)]
        self.tick(self.labs(groups=failed))
        lab = self.labs(groups=failed + [dict(name="새 미확인", headline_name="new run",
                                            unknown=1, failed=0, stagnant=0)])
        self.assertEqual(self.tick(lab), 1)
        self.assertIn("지금 볼 것: 새 미확인 — new run", self.notice()["text"])
        self.assertEqual(self.tick(lab), 0)

    def test_six_groups_are_announced_together_and_remembered_once(self):
        groups = [dict(name="묶음" + str(i) + "가" * 77, headline_name="나" * 80,
                       unknown=1, failed=0, stagnant=0) for i in range(6)]
        lab = self.labs(groups=groups)
        self.assertEqual(self.tick(lab), 1)
        self.assertIn("LAB: 미확인 6·이름 없음 0·실패 0·정체 0 (묶음 6개)", self.notice()["text"])
        self.assertEqual(len(self.obs()["lab_notified"]), 6)
        for _ in range(7):
            self.assertEqual(self.tick(lab), 0)

    def test_oversized_group_does_not_hide_any_new_problem(self):
        lab = self.labs(groups=[
            dict(name="가" * 180, headline_name="나" * 80, unknown=1, failed=0, stagnant=0),
            dict(name="small-group", headline_name="run B", unknown=1, failed=0, stagnant=0)])
        self.assertEqual(self.tick(lab), 1)
        self.assertIn("LAB: 미확인 2·이름 없음 0·실패 0·정체 0 (묶음 2개)", self.notice()["text"])
        self.assertEqual(len(self.obs()["lab_notified"]), 2)
        for _ in range(3):
            self.assertEqual(self.tick(lab), 0)

    def test_healthy_name_does_not_displace_actions_but_counts_remain(self):
        self.seen["open_work"] = [str(i) + "x" * 69 for i in range(4)]
        self.tick(self.labs(groups=[dict(name="healthy" + "가" * 30, headline_name="best" + "나" * 20,
                                        unknown=0, failed=0, stagnant=0)]))
        text = self.notice()["text"]
        self.assertNotIn("more", text)
        self.assertIn("LAB: 미확인 0·이름 없음 0·실패 0·정체 0", text)
        for work in self.seen["open_work"]:
            self.assertIn(work, text)

    def test_new_run_same_condition_and_new_unlinked_run(self):
        lab = self.labs(unknown=1, unlinked=1)
        self.tick(lab)
        lab["/synthetic"]["groups"][0]["rows"][0]["run_id"] = "replacement"
        self.assertEqual(self.tick(lab), 1)
        self.assertEqual(self.tick(lab), 0)
        lab["/synthetic"]["unlinked_run_ids"] = ["different-loose"]
        self.assertEqual(self.tick(lab), 1)
        self.assertEqual(self.tick(lab), 0)

    def test_missing_summary_retains_memory_and_role_reset_clears_it(self):
        lab = self.labs(unknown=1)
        self.tick(lab)
        before = self.obs()["lab_notified"]
        self.assertEqual(self.tick({}), 0)  # failed read retains identity
        self.assertEqual(self.obs()["lab_notified"], before)
        empty = {"/synthetic": dict(groups=[], unlinked=0, unlinked_run_ids=[],
                                   unknown=0, failed=0, stagnant=0)}
        self.assertEqual(self.tick(empty), 0)
        self.assertEqual(self.obs()["lab_notified"], before)
        self.assertEqual(self.tick(lab), 0)  # absent then returned is the same run
        for i in range(20):
            lab["/synthetic"]["groups"][0]["rows"][0]["run_id"] = str(i)
            self.assertEqual(self.tick(lab), 1)
            self.assertEqual(len(self.obs()["lab_notified"]), i + 2)
        SC.settle_session(HARNESS, SID, reason="role-released", store=self.store)
        self.register(now=self.clock)
        self.assertEqual(self.tick(lab), 1)
        self.assertEqual(self.tick(lab), 0)
        self.assertNotIn("lab_seen", self.obs())

    def test_lab_language_and_existing_signature_scope(self):
        seen = {**self.seen, "lab": self.labs(unknown=12, failed=1, stagnant=2, unlinked=3)}
        for lang, words in (("en", ("failed 1", "stuck 2", "unknown 12", "look now:", "unnamed 3")),
                            ("ko", ("실패 1", "정체 2", "미확인 12", "지금 볼 것:", "이름 없음 3"))):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                counts, names, _ = SC._lab_summary(seen, [])
                line = counts + names
                for word in words:
                    self.assertIn(word, line)
        self.assertEqual(SC._signature("tidy-and-release", seen), SC._signature(
            "tidy-and-release", {**seen, "lab": self.labs(unknown=12, failed=9, unlinked=3)}))
        changed = self.labs(unknown=12, unlinked=3)
        changed["/synthetic"]["groups"][0]["headline_name"] = "다른 조건"
        self.assertEqual(SC._signature("tidy-and-release", seen), SC._signature(
            "tidy-and-release", {**seen, "lab": changed}))

    def test_separate_groups_roots_and_ordinary_work_still_wake(self):
        self.tick(self.labs(unknown=1))
        self.assertEqual(self.tick(self.labs(unlinked=1)), 1)
        self.assertEqual(self.tick(self.labs(unlinked=0)), 0)
        labs = self.labs(unlinked=1, groups=[dict(name="new-group", headline_name="new",
                                                unknown=1, failed=0, stagnant=0)])
        self.assertEqual(self.tick(labs), 1)
        labs["/second"] = labs.pop("/synthetic")
        self.assertEqual(self.tick(labs), 1)
        self.seen["open_work"] = ["new work"]
        self.assertEqual(self.tick({}), 1)
        self.seen["targets"] = [target()]
        self.assertEqual(self.tick({}), 1)
        self.assertEqual(self.tick({}), 1)

    def test_unlinked_idless_tail_only_wakes_above_announced_high(self):
        self.tick(self.labs())
        for count, expected in ((65, 1), (65, 0), (64, 0), (65, 0), (66, 1), (0, 0), (66, 0)):
            lab = self.labs(unlinked=count)
            lab["/synthetic"]["unlinked_run_ids"] = lab["/synthetic"]["unlinked_run_ids"][:64]
            self.assertEqual(self.tick(lab), expected, count)
        self.assertEqual(sorted(dict(self.obs()["lab_notified"]).values()), [1] * 64 + [66])

    def test_2000_replacement_runs_keep_only_1024_oldest_first(self):
        lab = self.labs(unknown=1)
        data = lab.pop("/synthetic")
        lab["/synthetic/" + "long-root-" * 30] = data
        data["groups"][0]["rows"][0]["condition_id"] = "condition-" * 30
        announced = []
        # Exercise the actual duty store and schedule; receipt delivery is covered
        # above. An immediate fake ACK avoids accumulating 2000 delivery fixtures.
        self.notify.side_effect = lambda *a, **kw: {"state": "pending"}
        with mock.patch.object(SC, "_notice_record", return_value={"state": "acked"}), \
                mock.patch.object(self, "deliver_and_ack", return_value=[]):
            for index in range(2000):
                data["groups"][0]["rows"][0]["run_id"] = "long-run-" * 30 + f"r{index:04d}"
                self.assertEqual(self.tick(lab), 1)
                memory = self.obs()["lab_notified"]
                self.assertEqual(len(memory), min(index + 1, 1024))
                announced.append(memory[-1])
            remembered = self.obs()["lab_notified"]
            self.assertEqual(remembered, announced[976:])
            size = len(json.dumps(self.duty()).encode())
            self.assertLess(size, 128 * 1024)
            self.assertEqual(self.tick(lab), 0)
        print("memory-2000", len(remembered), "record-bytes", size)

    def many(self, count):
        """One group of `count` unknown runs with a readable start: two nameplates each."""
        rows = [dict(condition_id=f"c{i}", run_id=f"r{i}", started_at=1.0 + i, unknown_flag=True)
                for i in range(count)]
        return {"/synthetic": dict(groups=[dict(name="AMI", headline_name="작은 모델", unknown=count,
                                                failed=0, stagnant=0, rows=rows)],
                                   unlinked=0, unlinked_run_ids=[], unknown=count, failed=0, stagnant=0)}

    def remember(self, entries):
        observation = self.obs()
        observation["lab_notified"] = entries
        self.store.update(self.duty()["id"], observation=observation)

    def test_a_full_memory_keeps_the_nameplates_that_are_still_visible(self):
        # 300 runs = 600 visible nameplates, announced long ago; 424 other (old) nameplates
        # fill the memory to its 1024 limit. New runs keep arriving: the cut must drop old ones.
        lab = self.many(300)
        self.assertEqual(self.tick(lab), 1)
        visible = [[rid, 1] for rid in SC._lab_summary({"lab": lab}, [])[2]]
        self.assertEqual(len(visible), 600)
        self.remember(visible + [[SC._lab_id("/gone", "run", i), 1] for i in range(424)])
        for added in range(1, 6):
            lab = self.many(300 + added)
            self.assertEqual(self.tick(lab), 1, added)
            self.assertEqual(self.tick(lab), 0, added)      # the next check is quiet
            memory = dict(self.obs()["lab_notified"])
            self.assertEqual(len(memory), 1024)
            self.assertTrue(set(SC._lab_summary({"lab": lab}, [])[2]) <= set(memory))
            self.assertLess(len(json.dumps(self.duty()).encode()), 128 * 1024)

    def test_more_visible_nameplates_than_the_memory_holds_announce_once_by_count(self):
        lab = self.many(1100)                                # 2200 nameplates, 1024 fit
        self.assertEqual(self.tick(lab), 1)
        self.assertIn("미확인 1100", self.notice()["text"])
        for _ in range(3):
            self.assertEqual(self.tick(lab), 0)
        self.assertLessEqual(len(self.obs()["lab_notified"]), 1024)
        self.assertLess(len(json.dumps(self.duty()).encode()), 128 * 1024)
        self.assertEqual(self.tick(self.many(1101)), 1)      # more than before: said again, once
        self.assertEqual(self.tick(self.many(1101)), 0)
        self.assertEqual(self.tick(self.many(1090)), 0)      # fewer: still quiet

    def test_a_renamed_count_word_does_not_wake_a_steward_already_told(self):
        lab = self.labs(unknown=1, unlinked=2)
        self.assertEqual(self.tick(lab), 1)
        before = self.obs()["lab_notified"]
        for lang in ("en", "ko"):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                self.assertEqual(self.tick(lab), 0, lang)
        self.assertEqual(self.obs()["lab_notified"], before)

    def test_the_count_word_for_runs_without_a_catalog_name(self):
        seen = {**self.seen, "lab": self.labs(unlinked=3)}
        for lang, word, old in (("ko", "이름 없음 3", "미연결"), ("en", "unnamed 3", "unlinked")):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                line = SC._lab_summary(seen, [])[0]
            self.assertIn(word, line)
            self.assertNotIn(old, line)

    def test_check_output_carries_the_lab_line_the_notice_carries(self):
        self.seen["targets"] = [target()]
        lab = self.labs(unknown=1, unlinked=2)
        self.seen["lab"] = lab
        line, names, _ = SC._lab_summary({"lab": lab}, [])
        self.assertIn("이름 없음 2", line)
        self.assertIn(line + names, self.check(self.clock + 10).splitlines())
        self.seen["lab"] = {}
        self.assertNotIn("LAB:", self.check(self.clock + 20))

    def test_installed_main_without_lab_memory(self):
        self.tick({})  # same ordinary signature and duty shape as installed main
        observation = self.obs()
        observation.pop("lab_notified", None)
        self.store.update(self.duty()["id"], observation=observation)
        self.assertEqual(self.tick({}), 0)
        self.assertEqual(self.tick(self.labs()), 0)
        self.assertEqual(self.tick(self.labs(unknown=1)), 1)
        self.assertEqual(self.tick(self.labs(unknown=1)), 0)

    def test_legacy_nameplate_only_wakes_for_current_unknown(self):
        self.tick(self.labs())
        lab = self.labs(unknown=1)
        row = lab["/synthetic"]["groups"][0]["rows"][0]
        row["started_at"] = 123.0
        old = SC._lab_id("/synthetic", "condition", row["condition_id"], row["run_id"])
        observation = self.obs()
        observation["lab_notified"] = [[old, 1]]
        self.store.update(self.duty()["id"], observation=observation)
        self.assertEqual(self.tick(self.labs()), 0)
        self.assertEqual(self.tick({}), 0)
        self.assertEqual(self.tick(lab), 1)
        self.assertEqual(self.tick(lab), 0)

    def test_missing_started_at_is_stable_and_unlinked_keeps_old_identity(self):
        lab = self.labs(unknown=1, unlinked=1)
        row = lab["/synthetic"]["groups"][0]["rows"][0]
        self.assertEqual(self.tick(lab), 1)
        row["started_at"] = None
        self.assertEqual(self.tick(lab), 0)
        row["started_at"] = 123.0
        self.assertEqual(self.tick(lab), 1)
        self.assertEqual(self.tick(lab), 0)
        self.assertIn(SC._lab_id("/synthetic", "unlinked", "loose-0"),
                      dict(self.obs()["lab_notified"]))

    def test_restarted_run_waits_for_readable_start(self):
        lab = self.labs(unknown=1)
        row = lab["/synthetic"]["groups"][0]["rows"][0]
        row["started_at"] = 123.0
        self.assertEqual(self.tick(lab), 1)
        row["started_at"] = None  # New execution, but its start is unreadable.
        self.assertEqual(self.tick(lab), 0)
        row["started_at"] = 456.0
        self.assertEqual(self.tick(lab), 1)
        self.assertEqual(self.tick(lab), 0)

    def test_long_tidy_names_are_removed_whole_before_lab_counts(self):
        self.seen["targets"] = [target("대" * 150 + str(i), state="done") for i in range(5)]
        self.tick(self.labs())
        self.assertEqual(self.tick(self.labs(unknown=1)), 1)
        text = self.notice()["text"]
        self.assertIn("LAB: 미확인 1·이름 없음 0·실패 0·정체 0", text)
        self.assertIn("`peer-steward steward off`", text)
        shown = sum(t["name"] in text for t in self.seen["targets"])
        self.assertIn(f"+{5 - shown} more", text)
        self.assertEqual(text.count("대"), shown * 150)
        self.assertEqual(self.tick(self.labs(unknown=1)), 0)

    def test_unknown_and_unlinked_only_wake_above_notified_level(self):
        for index, value in enumerate((1, 0, 1, None, 1, 2, 1, 2)):
            labs = {} if value is None else self.labs(unknown=value, unlinked=value)
            with self.subTest(index=index):
                self.assertEqual(self.tick(labs), int(index in (0, 5)))


if __name__ == "__main__":
    unittest.main()
