#!/usr/bin/env python3
"""Private, temporary-fixture regressions for route-free support settlement."""
from __future__ import annotations

import importlib.util
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
from split_module_fixture import patch_split

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "utilities")]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


JOIN = load("dispatch_completion_join", ROOT / "utilities/dispatch_completion_join.py")
REAP = load("support_test_reap", ROOT / "utilities/dispatch-reap-watch.py")
REGISTRY = load("support_test_registry", ROOT / "utilities/dispatch-registry.py")
CONTRACT = __import__("dispatch_contract")


class SupportSettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hearting-support-settlement-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.log = self.base / "support.claude.jsonl"

    def write_log(self, verdict="PASS", handoff=True):
        result = "artifact: -\nverdict: %s\nblocker: none" % verdict if handoff else "not a handoff"
        self.log.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
            + json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": result}) + "\n",
            encoding="utf-8",
        )

    def row(self, attempt="att-support", verdict="PASS", *, handoff=True, status="open"):
        self.write_log(verdict, handoff)
        meta = {
            "attempt_id": attempt, "attempt_schema_version": "2", "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "support", "unit": "ops/session-tidy-memory",
            "assigned_contract": "session-tidy-memory", "harness": "claude",
            "launch_lifecycle": "detached", "log_file": str(self.log),
            "pid": "4321", "pid_start": "42", "pgid": "4321",
            "pid_ns": "test-ns", "pid_observer_ns": "test-ns",
            "artifact_root": str(self.base / ".agent_reports"),
        }
        raw = "\t".join(("2026-10-04T00:00:00Z", status, str(self.base),
                          str(self.base), f"{attempt}-slug",
                          ",".join(f"{key}={value}" for key, value in meta.items())))
        self.jobs.write_text(raw + "\n", encoding="utf-8")
        return JOIN.exact_attempt_row(self.jobs, attempt)

    @staticmethod
    def quiescent(*_args, **_kwargs):
        return CONTRACT.ProcessQuiescence("quiescent", "fixture-drained")

    def test_join_settles_pass_artifact_dash_without_route_authority(self):
        row = self.row()
        with patch_split(JOIN, "attempt_process_quiescence", self.quiescent), \
             mock.patch.object(JOIN, "run_route_completion") as route_completion:
            result = JOIN.settle_finished_attempt(self.jobs, row)
        current = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
        self.assertTrue(result["closed"], result)
        self.assertEqual(current.status, "done")
        self.assertEqual(current.metadata.get("note"), "completed-supervisor")
        self.assertEqual(current.metadata.get("failure_class"), "pass")
        self.assertFalse(current.metadata.get("route_node"))
        route_completion.assert_not_called()

    def test_failure_and_blocked_keep_supervisor_semantics(self):
        for verdict, note, failure in (("FAIL", "dead-worker-fail", "fail"),
                                       ("BLOCKED", "dead-worker-blocked", "blocked")):
            with self.subTest(verdict=verdict):
                row = self.row(f"att-{verdict.lower()}", verdict)
                with patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
                    result = JOIN.settle_finished_attempt(self.jobs, row)
                current = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
                self.assertTrue(result["closed"], result)
                self.assertEqual(current.metadata.get("note"), note)
                self.assertEqual(current.metadata.get("failure_class"), failure)

    def test_live_foreign_duplicate_and_terminal_history_do_not_close(self):
        row = self.row()
        selected = CONTRACT.launched_attempt_identity(row.raw.split("\t"))
        decision = JOIN.classify_exact_route_free_support_outcome(
            row, jobs=self.jobs, expected_attempt_id=row.attempt_id, expected_pid=4321,
            expected_pid_start="42", expected_pgid=4321,
            quiescence=CONTRACT.ProcessQuiescence("live", "leader-live"),
            expected_log_file=str(self.log), selected_identity=selected,
        )
        self.assertEqual(decision.close_action, "pending")
        wrong = JOIN.classify_exact_route_free_support_outcome(
            row, jobs=self.jobs, expected_attempt_id=row.attempt_id, expected_pid=4322,
            expected_pid_start="42", expected_pgid=4321, quiescence=self.quiescent(),
            expected_log_file=str(self.log), selected_identity=selected,
        )
        self.assertEqual(wrong.close_action, "pending")
        self.jobs.write_text(row.raw + "\n" + row.raw + "\n", encoding="utf-8")
        duplicate = JOIN.classify_exact_route_free_support_outcome(
            row, jobs=self.jobs, expected_attempt_id=row.attempt_id, expected_pid=4321,
            expected_pid_start="42", expected_pgid=4321, quiescence=self.quiescent(),
            expected_log_file=str(self.log), selected_identity=selected,
        )
        self.assertEqual(duplicate.reason, "support-row-not-unique")
        self.jobs.write_text("", encoding="utf-8")
        terminal = self.row(status="killed")
        with patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            result = JOIN.settle_finished_attempt(self.jobs, terminal)
        self.assertTrue(result["closed"])
        self.assertEqual(JOIN.exact_attempt_row(self.jobs, terminal.attempt_id).status, "killed")

    def test_changed_log_or_identity_vetoes_cas_and_preserves_stop(self):
        row = self.row()
        selected = CONTRACT.launched_attempt_identity(row.raw.split("\t"))
        classification = JOIN.classify_exact_route_free_support_outcome(
            row, jobs=self.jobs, expected_attempt_id=row.attempt_id, expected_pid=4321,
            expected_pid_start="42", expected_pgid=4321, quiescence=self.quiescent(),
            expected_log_file=str(self.log), selected_identity=selected,
        )
        self.write_log("FAIL")
        veto = JOIN.apply_exact_route_free_support_classification(
            row, jobs=self.jobs, classification=classification, selected_identity=selected,
            expected_log_file=str(self.log), expected_pid=4321,
            expected_pid_start="42", expected_pgid=4321,
        )
        self.assertEqual(veto, "support-settlement-revalidation-veto")
        self.assertEqual(JOIN.exact_attempt_row(self.jobs, row.attempt_id).status, "open")

        row = self.row("att-stop", status="killed")
        # The writer observes the explicit terminal row in the registry and cannot
        # reclassify or replace its stop history.
        with patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            outcome = JOIN.settle_finished_attempt(self.jobs, row)
        self.assertTrue(outcome["closed"])
        self.assertEqual(JOIN.exact_attempt_row(self.jobs, "att-stop").status, "killed")

    def test_runtime_handoff_error_is_typed_and_missing_result_never_passes(self):
        row = self.row("att-runtime")
        self.log.write_text(json.dumps({"type": "dispatch.supervisor.error",
                                        "reason": "runtime-session-failed"}) + "\n",
                            encoding="utf-8")
        with patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            result = JOIN.settle_finished_attempt(self.jobs, row)
        current = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
        self.assertTrue(result["closed"], result)
        self.assertEqual(current.metadata.get("note"), "dead-runtime-exit")
        self.assertEqual(current.metadata.get("failure_class"), "runtime")
        self.assertEqual(current.metadata.get("process_exit"), "70")

        row = self.row("att-no-result", handoff=False)
        self.log.unlink()
        with patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            result = JOIN.settle_finished_attempt(self.jobs, row)
        current = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
        self.assertTrue(result["closed"], result)
        self.assertEqual(current.metadata.get("failure_class"), "protocol")
        self.assertEqual(current.metadata.get("note"), "dead-missing-result")

    def test_log_aba_commits_the_decision_validated_under_cas(self):
        for first, middle in (("PASS", "FAIL"), ("FAIL", "PASS"), ("BLOCKED", "PASS")):
            with self.subTest(first=first, middle=middle):
                row = self.row(verdict=first)
                identity = CONTRACT.launched_attempt_identity(row.raw.split("\t"))
                selected = JOIN.classify_exact_route_free_support_outcome(
                    row, jobs=self.jobs, expected_attempt_id=row.attempt_id,
                    expected_pid=4321, expected_pid_start="42", expected_pgid=4321,
                    quiescence=self.quiescent(), expected_log_file=str(self.log),
                    selected_identity=identity,
                )
                self.write_log(middle)
                writer = JOIN.close_attempt_row_if

                def restore_before_cas(*args, **kwargs):
                    self.write_log(first)
                    return writer(*args, **kwargs)

                with patch_split(JOIN, "attempt_process_quiescence", self.quiescent), \
                     mock.patch.object(JOIN, "close_attempt_row_if", restore_before_cas):
                    outcome = JOIN.apply_exact_route_free_support_classification(
                        row, jobs=self.jobs, classification=selected, selected_identity=identity,
                        expected_log_file=str(self.log), expected_pid=4321,
                        expected_pid_start="42", expected_pgid=4321,
                    )
                current = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
                self.assertEqual(outcome, "support-settlement-committed")
                self.assertEqual(current.metadata.get("note"), selected.note)
                for key, value in selected.evidence.items():
                    self.assertEqual(current.metadata.get(key), value)

    def test_cas_rechecks_quiescence_binding_duplicates_and_stop(self):
        for change in ("live", "unverifiable", "pid_start", "log_file", "namespace", "duplicate", "cancelled"):
            with self.subTest(change=change):
                row = self.row()
                before = self.jobs.read_text()
                writer = JOIN.close_attempt_row_if
                probe = [self.quiescent()]
                expected = [before]

                def change_before_cas(*args, **kwargs):
                    if change in {"live", "unverifiable"}:
                        probe[0] = CONTRACT.ProcessQuiescence(change, "fixture-new-observation")
                    elif change == "duplicate":
                        expected[0] = before + before
                    elif change == "cancelled":
                        expected[0] = before.replace("\topen\t", "\tcancelled\t", 1)
                    else:
                        old, new = {
                            "pid_start": ("pid_start=42", "pid_start=43"),
                            "log_file": (str(self.log), str(self.base / "foreign.claude.jsonl")),
                            "namespace": ("pid_observer_ns=test-ns", "pid_observer_ns=foreign-ns"),
                        }[change]
                        expected[0] = before.replace(old, new, 1)
                    self.jobs.write_text(expected[0])
                    return writer(*args, **kwargs)

                with patch_split(JOIN, "attempt_process_quiescence", lambda *_: probe[0]), \
                     mock.patch.object(JOIN, "close_attempt_row_if", change_before_cas):
                    result = JOIN.settle_finished_attempt(self.jobs, row)
                self.assertEqual(self.jobs.read_text(), expected[0])
                self.assertEqual(result["closed"], change == "cancelled")

        # A terminal row with a different admitted binding cannot settle the
        # caller's stale attempt; preserving history does not grant identity.
        row = self.row()
        self.jobs.write_text(row.raw.replace("\topen\t", "\tdone\t", 1)
                             .replace("pid_start=42", "pid_start=43", 1) + "\n")
        result = JOIN.settle_finished_attempt(self.jobs, row)
        self.assertFalse(result["closed"])
        self.assertEqual(result["reason"], "support-binding-mismatch")

    def test_duplicate_settlement_sibling_and_terminal_history_are_unchanged(self):
        row = self.row()
        sibling = row.raw.replace("att-support", "att-sibling")
        self.jobs.write_text(row.raw + "\n" + sibling + "\n")
        with patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            self.assertTrue(JOIN.settle_finished_attempt(self.jobs, row)["closed"])
            committed = self.jobs.read_bytes()
            self.assertTrue(JOIN.settle_finished_attempt(self.jobs, row)["closed"])
        self.assertEqual(self.jobs.read_bytes(), committed)
        self.assertEqual(self.jobs.read_text().splitlines()[1], sibling)
        for status in ("done", "killed", "cancelled"):
            with self.subTest(status=status):
                row = self.row(status=status)
                original = row.raw + ",note=dead-runtime-exit,failure_class=runtime\n"
                self.jobs.write_text(original)
                row = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
                self.assertTrue(JOIN.settle_finished_attempt(self.jobs, row)["closed"])
                self.assertEqual(self.jobs.read_text(), original)

    def test_reaper_drained_path_uses_shared_settlement(self):
        row = self.row()
        args = types.SimpleNamespace(
            jobs=self.jobs, attempt_id=row.attempt_id, pid=4321, pid_start="42", pgid=4321,
            interval=0.001, drain_interval_max=0.001, residue_grace=0,
            parent_recheck_interval=0.001,
        )
        empty = types.SimpleNamespace(state="empty", members=())
        with patch_split(REAP, "process_namespace_identity", return_value="test-ns"), \
             patch_split(REAP, "attempt_scan_namespace_authority", return_value=True), \
             patch_split(REAP, "process_start_ticks", return_value="43"), \
             patch_split(REAP, "process_group_observation", return_value=empty), \
             patch_split(REAP, "attempt_tagged_descendants", return_value=empty), \
             patch_split(REAP, "attempt_process_quiescence", self.quiescent), \
             patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            result = REAP.watch(args)
        current = JOIN.exact_attempt_row(self.jobs, row.attempt_id)
        self.assertEqual(CONTRACT.launched_attempt_identity(current.raw.split("\t")),
                         CONTRACT.launched_attempt_identity(row.raw.split("\t")))
        self.assertEqual(current.metadata.get("launch_outcome"), "governed-process-group-drained")
        self.assertEqual(result, 0)
        self.assertEqual(JOIN.exact_attempt_row(self.jobs, row.attempt_id).metadata.get("note"),
                         "completed-supervisor")

    def registry_args(self, apply):
        return types.SimpleNamespace(
            jobs=self.jobs, attempt="att-support", session="", route="", node="", job="",
            apply=apply, agent_home=None, integration_ref="", only_exact_dead=False,
            all=True, now=0, audit=None,
        )

    def test_registry_dry_run_and_apply_use_shared_settlement(self):
        self.row()
        with patch_split(REGISTRY, "attempt_process_quiescence", self.quiescent), \
             patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            dry_out = io.StringIO()
            with contextlib.redirect_stdout(dry_out):
                dry = REGISTRY.reconcile(REGISTRY.read_rows(self.jobs), self.registry_args(False))
        self.assertEqual(JOIN.exact_attempt_row(self.jobs, "att-support").status, "open")
        self.assertEqual(dry, 0)
        self.assertEqual(json.loads(dry_out.getvalue())["closed"], 0)
        with patch_split(REGISTRY, "attempt_process_quiescence", self.quiescent), \
             patch_split(JOIN, "attempt_process_quiescence", self.quiescent):
            applied_out = io.StringIO()
            with contextlib.redirect_stdout(applied_out):
                applied = REGISTRY.reconcile(REGISTRY.read_rows(self.jobs), self.registry_args(True))
        current = JOIN.exact_attempt_row(self.jobs, "att-support")
        self.assertEqual(current.status, "done")
        self.assertEqual(current.metadata.get("note"), "completed-supervisor")
        self.assertEqual(applied, 0)
        self.assertEqual(json.loads(applied_out.getvalue())["closed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
