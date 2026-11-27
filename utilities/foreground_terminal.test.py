#!/usr/bin/env python3
"""Same worker result, same close: the foreground tail decides once for every harness."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import foreground_terminal as F  # noqa: E402


def native_log(harness: str, text: str) -> list[dict]:
    """The worker's last turn as each harness writes it (translation only)."""
    if harness == "codex":
        return [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                {"type": "turn.completed"}]
    if harness == "claude":
        return [{"type": "result", "subtype": "success", "is_error": False, "result": text}]
    return [{"type": "text", "sessionID": "ses_t", "part": {"type": "text", "text": text}},
            {"type": "step_finish", "sessionID": "ses_t", "part": {"type": "step-finish", "reason": "stop"}}]


class SameResultSameCloseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.worktree = self.base / "repo"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.root = self.base / ".agent_reports"
        self.root.mkdir()

    def settle(self, harness, text, failure=None):
        log = self.base / f"{harness}.jsonl"
        log.write_text("\n".join(json.dumps(row) for row in native_log(harness, text)) + "\n")
        closes, legacy = [], []
        outcome = SimpleNamespace(failure=failure, exit_code=1 if failure else 0)
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.root)}), \
             mock.patch.object(F, "close_attempt_row",
                               side_effect=lambda jobs, aid, note, evidence: closes.append((note, evidence)) or True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close"):
            settled = F.settle_foreground_exit(self.base / "jobs.log", "att-1", log, outcome,
                                               worktree=self.worktree, artifact_root=self.root,
                                               worker_type="stage", legacy_close=legacy.append)
        evidence = closes[0][1] if closes else {}
        return (settled["verdict"], settled["note"], settled["closed"], settled["worker_failure"],
                evidence.get("detected_by"), tuple(legacy))

    def test_every_harness_settles_the_same_result_the_same_way(self):
        cases = {
            "pass": ("artifact: -\nverdict: PASS\nblocker: none", None),
            "fail": ("artifact: -\nverdict: FAIL\nblocker: tests failed", None),
            "blocked": ("artifact: -\nverdict: BLOCKED\nblocker: needs an answer", None),
            "malformed": ("done, all good", None),
            "crashed-after-pass": ("artifact: -\nverdict: PASS\nblocker: none", "nonzero-exit"),
            "quiet-no-progress": ("artifact: -\nverdict: PASS\nblocker: none", "no-progress"),
        }
        for name, (text, failure) in cases.items():
            with self.subTest(case=name):
                results = {harness: self.settle(harness, text, failure)
                           for harness in ("claude", "codex", "opencode")}
                self.assertEqual(len(set(results.values())), 1, results)
                verdict, note, closed, worker_failure, detected_by, legacy = results["claude"]
                if name == "pass":
                    self.assertEqual((verdict, note, closed), ("PASS", "", False))
                if name in ("fail", "blocked"):
                    self.assertTrue(note and closed, results)
                    self.assertEqual(detected_by, "foreground-terminal-handoff")
                if name == "crashed-after-pass":
                    self.assertEqual((note, closed, detected_by), ("dead-nonzero-exit", True, "foreground-process-exit"))
                if name == "quiet-no-progress":
                    self.assertEqual((note, closed, detected_by), ("dead-no-progress", True, "foreground-process-exit"))


class UnitCapacityExitTest(unittest.TestCase):
    def test_opencode_nonzero_exit_keeps_the_native_error_kind(self):
        from dispatch_completion_join import completion_followup_text, exact_attempt_row
        from dispatch_contract_base import _terminal_delivery_receipt

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = "att-opencode-native"
            log = root / f"stage.{attempt}.opencode.jsonl"
            log.write_text(json.dumps({"type": "error", "sessionID": "ses-fixture",
                                       "error": {"type": "provider.content-filter"}}) + "\n")
            jobs = root / "jobs.log"
            jobs.write_text(f"now\topen\t{root}\t{root}\tstage\t"
                            f"attempt_id={attempt},attempt_schema_version=2,harness=opencode,"
                            "worker_type=stage,transport=headless,execution_surface=registered-headless,"
                            "fallback_hop=same-harness-headless,"
                            f"registered_worker=1,dispatch_depth=2,log_file={log}\n")
            legacy = mock.Mock()
            outcome = SimpleNamespace(exit_code=70, failure="exit-70")
            settled = F.settle_foreground_exit(jobs, attempt, log, outcome, worktree=root, artifact_root=root,
                                               worker_type="stage", legacy_close=legacy)
            self.assertTrue(settled["closed"], settled)
            legacy.assert_not_called()
            row = exact_attempt_row(jobs, attempt)
            self.assertEqual((row.status, row.metadata["note"]), ("done", "dead-exit-70"))
            self.assertEqual(row.metadata.get("provider_failure_kind"), "provider.content-filter")
            receipt = _terminal_delivery_receipt(row.metadata)
            self.assertIn("was provider.content-filter.", completion_followup_text(receipt, jobs=str(jobs)))

    def test_irrecoverable_process_exit_does_not_discard_exact_unit_capacity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "unit.att-unit.opencode.jsonl"
            log.write_text(json.dumps({"type": "result", "runtime": "opencode", "is_error": True,
                                       "error": {"status": 429, "message": "usage limit"}}) + "\n")
            row = SimpleNamespace(metadata={
                "attempt_id": "att-unit", "worker_type": "review", "capability": "unit", "harness": "opencode",
                "artifact_root": str(root), "log_file": str(log), "dispatch_depth": "1", "route_node": "unit",
                "workflow_completion": "runtime-v1"})
            with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=row), \
                    mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
                    mock.patch.object(F, "close_attempt_row", return_value=True) as close:
                for failure, note in (("exit-1", "dead-capacity"), ("signal-1", "dead-signal-1")):
                    F.settle_foreground_exit(root / "jobs.log", "att-unit", log,
                                             SimpleNamespace(failure=failure, exit_code=1),
                                             worktree=root, artifact_root=root, worker_type="review",
                                             legacy_close=mock.Mock())
                    self.assertEqual(close.call_args.args[2], note)
                    evidence = close.call_args.kwargs["evidence"]
                    self.assertEqual(evidence["failure_class"], "capacity" if failure == "exit-1" else "runtime")


class EveryWrapperUsesItTest(unittest.TestCase):
    def test_the_three_wrappers_call_the_shared_settlement(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                source = (ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text()
                self.assertEqual(source.count("DISPATCH_MAIN.launch_claimed_attempt("), 1)
                self.assertNotIn("settle_foreground_exit(", source)
                self.assertNotIn("terminal_note = f\"dead-{outcome.failure}\"", source)
        shared = (ROOT / "utilities" / "dispatch_headless_main.py").read_text()
        self.assertEqual(shared.count("settle_foreground_exit("), 1)



class ArtifactHintTest(unittest.TestCase):
    def test_the_close_carries_where_an_invalid_artifact_was_looked_for(self):
        from codex_dispatch_terminal import ARTIFACT_HINT_KEYS
        from dispatch_contract import ATTEMPT_TERMINAL_EVIDENCE_KEYS
        terminal = {"artifact_base_root_b64": "cm9vdA", "artifact_candidates_b64": "YQ"}
        evidence = F.terminal_evidence(terminal, "dead-nonzero-exit", "/tmp/log")
        self.assertEqual({key: evidence[key] for key in ARTIFACT_HINT_KEYS}, terminal)
        # a key the registry would refuse would make the whole close fail
        self.assertLessEqual(set(ARTIFACT_HINT_KEYS), ATTEMPT_TERMINAL_EVIDENCE_KEYS)

if __name__ == "__main__":
    unittest.main()
