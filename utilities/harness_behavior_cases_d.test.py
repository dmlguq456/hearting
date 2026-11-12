#!/usr/bin/env python3
"""Offline checks for the watch scenarios' shared model script (no TUI is launched)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_cases_d as cd  # noqa: E402
from dispatch_contract_roots import stable_state_root  # noqa: E402

ARM = "SCENARIO-WATCH arm a native watch on the target and follow parent_next"
ARMED = ("watch_id=0123456789abcdef state=armed parent_next=end-turn "
         "parent_next_reason=notify-wakes-you")
RECEIPT = "watch_id=0123456789abcdef state=session-ended exact end"


def record(text, last, kind="user", reply=None):
    return {"text": text, "last_text": last, "last_kind": kind, "reply": reply or {}}


class SharedScriptTest(unittest.TestCase):
    """The steward TUI and the target TUI answer from one script, interleaved."""

    def setUp(self):
        self.scenario = cd.WatchWakeScenario()
        self.scenario.target_address = "%1@/tmp/hbt-x/s"
        self.earlier = []

    def ask(self, text, last, kind="user"):
        rec = record(text, last, kind)
        reply = self.scenario._answer(rec, list(self.earlier))
        rec["reply"] = {"text": reply.text, "shell": reply.shell}
        self.earlier.append(rec)
        return reply

    def test_target_init_then_work_then_done(self):
        init = "SCENARIO-WATCH-TARGET INIT"
        self.assertEqual(self.ask(init, init).text, "target initialized")
        work_text = init + "\ntarget initialized\nSCENARIO-WATCH-TARGET WORK"
        work = self.ask(work_text, "SCENARIO-WATCH-TARGET WORK")
        self.assertEqual(work.shell, "sleep 12; echo SCENARIO-WATCH-TARGET-DONE")
        done = self.ask(work_text + "\nSCENARIO-WATCH-TARGET-DONE",
                        "SCENARIO-WATCH-TARGET-DONE", kind="tool")
        self.assertEqual(done.text, "target turn done")

    def test_steward_arms_once_even_after_target_turns(self):
        init = "SCENARIO-WATCH-TARGET INIT"
        self.ask(init, init)
        self.ask(init + "\nSCENARIO-WATCH-TARGET WORK", "SCENARIO-WATCH-TARGET WORK")
        arm = self.ask(ARM, ARM)
        self.assertIn("peer-steward.py", arm.shell)
        self.assertIn(" watch ", arm.shell)
        self.assertIn("%1@/tmp/hbt-x/s", arm.shell)
        # the same prompt text later in the conversation does not arm a second watch
        again = self.ask(ARM, ARM)
        self.assertEqual(again.text, "Nothing new.")
        self.assertIsNone(again.shell)

    def test_armed_line_ends_the_turn_and_one_receipt_wakes_the_steward(self):
        self.ask(ARM, ARM)
        armed = self.ask(ARM + "\n" + ARMED, ARMED, kind="tool")
        self.assertTrue(armed.text.startswith("Watch armed"))
        wake_text = ARM + "\n" + ARMED + "\nWatch armed\n" + RECEIPT
        wake = self.ask(wake_text, RECEIPT)
        self.assertIn(cd.WatchWakeScenario.NEXT, wake.shell)
        handled = self.ask(wake_text + f"\n{cd.WatchWakeScenario.NEXT}-42",
                           f"{cd.WatchWakeScenario.NEXT}-42", kind="tool")
        self.assertEqual(handled.text, "Watch handled.")

    def test_target_and_steward_interleave_without_crossing(self):
        init = "SCENARIO-WATCH-TARGET INIT"
        self.assertEqual(self.ask(init, init).text, "target initialized")
        self.assertIn("watch", self.ask(ARM, ARM).shell)
        work = self.ask(init + "\nSCENARIO-WATCH-TARGET WORK", "SCENARIO-WATCH-TARGET WORK")
        self.assertIn("SCENARIO-WATCH-TARGET-DONE", work.shell)
        self.assertEqual(self.ask(ARM + "\n" + ARMED, ARMED, kind="tool").text[:11], "Watch armed")


class WatchReceiptRootTest(unittest.TestCase):
    def test_receipt_is_read_from_the_stable_state_root_of_the_fixture_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "XDG_STATE_HOME": str(Path(tmp) / "state")}
            self.assertNotIn("AGENT_PEER_LEDGER_ROOT", env)
            root = stable_state_root(env) / "peer-watches"
            root.mkdir(parents=True)
            (root / "0123456789abcdef.receipt.json").write_text(
                json.dumps({"state": "session-ended"}), encoding="utf-8")
            run = SimpleNamespace(home=SimpleNamespace(env=env))
            receipt = cd.NativeWatchEndScenario._read_watch_receipt(run, "0123456789abcdef")
            self.assertEqual(receipt, {"state": "session-ended"})
            self.assertIsNone(cd.NativeWatchEndScenario._read_watch_receipt(run, "ffffffffffffffff"))
            self.assertIsNone(cd.NativeWatchEndScenario._read_watch_receipt(run, None))

    def test_the_override_still_wins_when_a_run_sets_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "peer-watches").mkdir()
            (Path(tmp) / "peer-watches" / "aaaaaaaaaaaaaaaa.receipt.json").write_text(
                json.dumps({"state": "done"}), encoding="utf-8")
            run = SimpleNamespace(home=SimpleNamespace(env={"AGENT_PEER_LEDGER_ROOT": tmp}))
            self.assertEqual(
                cd.NativeWatchEndScenario._read_watch_receipt(run, "aaaaaaaaaaaaaaaa"), {"state": "done"})


class NextActionResultTest(unittest.TestCase):
    """The woken steward's shell call is not its result: wait for the output to reach the model."""

    def test_a_result_that_arrives_after_the_wake_still_counts(self):
        import threading
        requests = [record("wake", "wake", reply={"shell": f"echo {cd.WatchWakeScenario.NEXT}-$((20+22))"})]
        run = SimpleNamespace(main_requests=lambda: list(requests))
        threading.Timer(0.6, lambda: requests.append(record(
            f"wake\n{cd.WatchWakeScenario.NEXT}-42", f"{cd.WatchWakeScenario.NEXT}-42", "tool"))).start()
        self.assertTrue(cd.WatchWakeScenario()._next_action_ran(run, timeout=10))

    def test_no_result_within_the_bound_is_not_run(self):
        run = SimpleNamespace(main_requests=lambda: [record("wake", "wake")])
        self.assertFalse(cd.WatchWakeScenario()._next_action_ran(run, timeout=1))


if __name__ == "__main__":
    unittest.main()
