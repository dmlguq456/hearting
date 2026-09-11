#!/usr/bin/env python3
"""Deterministic production-seam checks for route-free foreground reviews."""

from __future__ import annotations

from contextlib import redirect_stdout
import base64
import importlib.util
import io
import json
import signal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_completion_join as join  # noqa: E402
import dispatch_contract as contract  # noqa: E402
from dispatch_lifecycle import begin_finite_watchdog
from review_watchdog import launch_review_watchdog


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


watcher = _module("review_lifecycle_reap_watch", "dispatch-reap-watch.py")
registry = _module("review_lifecycle_registry", "dispatch-registry.py")


def _jobs(tmp: Path, **overrides: str) -> Path:
    metadata = {
        "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
        "execution_surface": "registered-headless", "registered_worker": "1",
        "fallback_hop": "same-harness-headless", "worker_type": "review",
        "launch_lifecycle": "foreground-scoped", "attempt_id": "att-lifecycle",
        "pid": "123", "pid_start": "456", "pgid": "123", "pid_ns": "pid:[1]",
        "pid_observer_ns": "pid:[1]", "capability": "autopilot-code",
        "capability_mode": "debug", "intensity": "strong", "qa": "standard",
        "harness": "codex", "parent_sid": "session-lifecycle",
        "parent_attempt_id": "parent-lifecycle",
        "parent_completion_delivery": "codex-managed-gateway", "parent_cwd": str(tmp),
        "artifact_root": str(tmp), "unit": "dev/backend", "assigned_contract": "code-execute",
        "review_cycle_id": "cycle-lifecycle", "review_producer_id": "producer-lifecycle",
        "log_file": str(tmp / "worker.jsonl"),
    }
    metadata.update({key: str(value) for key, value in overrides.items()})
    path = tmp / "jobs.log"
    pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
    path.write_text(f"2026-09-11T00:00:00Z\topen\t/repo\t/wt\treview\t{pipe}\n", encoding="utf-8")
    return path


def _row(jobs: Path):
    return join.exact_attempt_row(jobs, "att-lifecycle")


def _reconcile_args(jobs: Path, agent_home: Path):
    return type("ReconcileArgs", (), {
        "session": "", "route": "", "node": "", "attempt": "att-lifecycle", "job": "",
        "apply": True, "jobs": jobs, "agent_home": agent_home, "integration_ref": "",
        "now": time.time(), "audit": None,
    })()


class ForegroundSealLifecycleTest(unittest.TestCase):
    def test_seal_replay_is_byte_stable_and_conflict_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = _jobs(Path(directory))
            sealed = contract.seal_foreground_result(jobs, "att-lifecycle", 123, "456", 123, exit_code=0, failure="", group_empty=True)
            before = jobs.read_bytes()
            replay = contract.seal_foreground_result(jobs, "att-lifecycle", 123, "456", 123, exit_code=0, failure="", group_empty=True)
            self.assertEqual(before, jobs.read_bytes())
            self.assertEqual(sealed, replay)
            with self.assertRaises(contract.DispatchContractError) as conflict:
                contract.seal_foreground_result(jobs, "att-lifecycle", 123, "456", 123, exit_code=7, failure="exit-7", group_empty=True)
            self.assertEqual(conflict.exception.reason, "foreground-outcome-conflict")
            self.assertEqual(before, jobs.read_bytes())

    def test_route_identity_and_partial_outcome_never_authorize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(contract.DispatchContractError) as partial:
                contract.seal_foreground_result(_jobs(root, foreground_outcome_schema="1"), "att-lifecycle", 123, "456", 123, exit_code=0, failure="", group_empty=True)
            self.assertEqual(partial.exception.reason, "foreground-outcome-partial")
            with self.assertRaises(contract.DispatchContractError) as bound:
                contract.seal_foreground_result(_jobs(root, route_id="rt-bound"), "att-lifecycle", 123, "456", 123, exit_code=0, failure="", group_empty=True)
            self.assertEqual(bound.exception.reason, "foreground-outcome-ineligible")

    def test_conflicting_seal_race_has_one_commit_and_one_nonmutating_loser(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = _jobs(Path(directory))
            barrier = threading.Barrier(2)
            results = []

            def seal(exit_code, failure):
                barrier.wait()
                try:
                    results.append(contract.seal_foreground_result(jobs, "att-lifecycle", 123, "456", 123, exit_code=exit_code, failure=failure, group_empty=True))
                except Exception as exc:
                    results.append(exc)

            threads = [threading.Thread(target=seal, args=(0, "")), threading.Thread(target=seal, args=(7, "exit-7"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(isinstance(item, contract.SealedForegroundOutcome) for item in results), 1)
            self.assertEqual(sum(isinstance(item, contract.DispatchContractError) and item.reason == "foreground-outcome-conflict" for item in results), 1)


class ForegroundClassificationLifecycleTest(unittest.TestCase):
    def test_production_watcher_and_registry_reconcile_share_one_terminal_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = subprocess.Popen(["sleep", "0.05"], start_new_session=True)
            pid, start, pgid = worker.pid, contract.process_start_ticks(worker.pid), os.getpgid(worker.pid)
            namespace = watcher.process_namespace_identity()
            jobs = _jobs(root, pid=pid, pid_start=start, pgid=pgid, pid_ns=namespace, pid_observer_ns=namespace)
            report = root / "review.md"
            report.write_text("review evidence\n", encoding="utf-8")
            worker.wait(timeout=2)
            contract.seal_foreground_result(jobs, "att-lifecycle", pid, start, pgid, exit_code=0, failure="", group_empty=True)
            watcher_args = type("WatcherArgs", (), {"jobs": jobs, "attempt_id": "att-lifecycle", "pid": pid, "pid_start": start, "pgid": pgid, "interval": 0.001, "drain_interval_max": 0.001, "residue_grace": 0.0, "parent_recheck_interval": 0.001})()
            reconcile_started = threading.Event()
            allow_watcher_apply = threading.Event()
            real_apply = watcher.apply_exact_route_free_review_classification

            def gated_apply(row, *, jobs, classification):
                reconcile_started.set()
                self.assertTrue(allow_watcher_apply.wait(5))
                return real_apply(row, jobs=jobs, classification=classification)

            def terminal(_log, **_kwargs):
                encoded = base64.urlsafe_b64encode(str(report).encode()).decode().rstrip("=")
                return {"state": "valid", "verdict": "PASS", "artifact_state": "readable", "artifact_path_b64": encoded}

            watcher_result = {}
            counts = {
                "cas": 0,
                "terminal_revision_note": 0,
                "delivery_identity": 0,
                "materialized_record": 0,
                "delivery_ids": set(),
            }
            real_join_close = join.close_attempt_row
            real_registry_close_if = registry.close_attempt_row_if
            real_updated_metadata = contract._updated_attempt_metadata
            real_delivery_intent = contract._delivery_intent_values
            real_delivery_create = join.pending_delivery.create

            def counted_join_close(*args, **kwargs):
                result = real_join_close(*args, **kwargs)
                if result:
                    counts["cas"] += 1
                return result

            def counted_registry_close_if(*args, **kwargs):
                result = real_registry_close_if(*args, **kwargs)
                if result:
                    counts["cas"] += 1
                return result

            def counted_updated_metadata(*args, **kwargs):
                result = real_updated_metadata(*args, **kwargs)
                if kwargs.get("terminal"):
                    counts["terminal_revision_note"] += 1
                return result

            def counted_delivery_intent(*args, **kwargs):
                result = real_delivery_intent(*args, **kwargs)
                if result.get("delivery_intent") == "1":
                    counts["delivery_identity"] += 1
                return result

            def counted_delivery_create(root_path, **kwargs):
                path = join.pending_delivery.record_path(
                    root_path, kwargs["recipient_key"], kwargs["delivery_id"]
                )
                existed = path.exists()
                result = real_delivery_create(root_path, **kwargs)
                if not existed:
                    counts["materialized_record"] += 1
                    counts["delivery_ids"].add(result["delivery_id"])
                return result

            with mock.patch.object(join, "inspect_terminal_attempt", side_effect=terminal), \
                    mock.patch.object(join, "validate_review_output_binding", return_value=None), \
                    mock.patch.object(join, "close_attempt_row", side_effect=counted_join_close), \
                    mock.patch.object(registry, "close_attempt_row_if", side_effect=counted_registry_close_if), \
                    mock.patch.object(contract, "_updated_attempt_metadata", side_effect=counted_updated_metadata), \
                    mock.patch.object(contract, "_delivery_intent_values", side_effect=counted_delivery_intent), \
                    mock.patch.object(join.pending_delivery, "create", side_effect=counted_delivery_create), \
                    mock.patch.object(watcher, "apply_exact_route_free_review_classification", side_effect=gated_apply):
                thread = threading.Thread(target=lambda: watcher_result.setdefault("rc", watcher.watch(watcher_args)))
                thread.start()
                self.assertTrue(reconcile_started.wait(5))
                with redirect_stdout(io.StringIO()):
                    registry_rc = registry.reconcile(registry.read_rows(jobs), _reconcile_args(jobs, root))
                allow_watcher_apply.set()
                thread.join(timeout=5)
                self.assertEqual((watcher_result.get("rc"), registry_rc), (0, 0))
                final = _row(jobs)
                self.assertEqual(final.raw.split("\t", 2)[1], "done")
                metadata = final.metadata
                identity = tuple(metadata.get(key, "") for key in ("note", "delivery_id", "delivery_row_revision", "delivery_receipt_digest", "classifier_source", "reconcile_reason"))
                self.assertEqual(identity[0], "completed-review")
                self.assertTrue(all(identity[1:]))
                self.assertEqual(len(list(root.rglob(f"{identity[1]}.json"))), 1)
                self.assertEqual(counts["cas"], 1)
                self.assertEqual(counts["terminal_revision_note"], 1)
                self.assertEqual(counts["materialized_record"], 1)
                self.assertEqual(counts["delivery_identity"], 1)
                self.assertEqual(counts["delivery_ids"], {identity[1]})
                terminal_bytes = jobs.read_bytes()
                delivery_path = next(root.rglob(f"{identity[1]}.json"))
                delivery_bytes = delivery_path.read_bytes()
                count_snapshot = dict(counts)
                count_snapshot["delivery_ids"] = set(counts["delivery_ids"])

                # Keep every counting delegate installed for the whole
                # lifecycle. These are the duplicate operations that must be
                # proven non-mutating, not merely sampled after the wrappers
                # have been removed.
                for _ in range(2):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(registry.reconcile(registry.read_rows(jobs), _reconcile_args(jobs, root)), 0)
                    self.assertEqual(terminal_bytes, jobs.read_bytes())
                duplicate_result = watcher.watch(watcher_args)
                self.assertIn(duplicate_result, (0, 65))
                after_duplicate = _row(jobs).metadata
                self.assertEqual(tuple(after_duplicate.get(key, "") for key in ("note", "delivery_id", "delivery_row_revision", "delivery_receipt_digest")), identity[:4])
                self.assertEqual(len(list(root.rglob(f"{identity[1]}.json"))), 1)
                self.assertEqual(counts, count_snapshot)
                self.assertEqual(delivery_bytes, delivery_path.read_bytes())

    def test_preseal_real_reconcile_is_pending_and_keeps_row_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = _jobs(root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(registry.reconcile(registry.read_rows(jobs), _reconcile_args(jobs, root)), 0)
            self.assertEqual(jobs.read_text(encoding="utf-8").split("\t", 2)[1], "open")
            self.assertNotIn("delivery_intent=1", jobs.read_text(encoding="utf-8"))

    def test_live_leader_group_tagged_and_setsid_observations_veto_seal_consumption(self):
        for reason in ("leader-live", "group-member-live", "attempt-descendant-live", "setsid-descendant-live"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                jobs = _jobs(Path(directory))
                contract.seal_foreground_result(jobs, "att-lifecycle", 123, "456", 123, exit_code=0, failure="", group_empty=True)
                decision = join.classify_exact_route_free_review_outcome(_row(jobs), jobs=jobs, expected_attempt_id="att-lifecycle", expected_pid=123, expected_pid_start="456", expected_pgid=123, quiescence=contract.ProcessQuiescence("live", reason))
                self.assertEqual(decision.close_action, "pending")
                self.assertEqual(decision.reason, f"foreground-process-{reason}")

    def test_each_route_identity_key_is_non_authoritative(self):
        for key in contract.ROUTE_IDENTITY_METADATA_KEYS:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                jobs = _jobs(Path(directory), **{key: "bound"})
                decision = join.classify_exact_route_free_review_outcome(_row(jobs), jobs=jobs, expected_attempt_id="att-lifecycle", expected_pid=123, expected_pid_start="456", expected_pgid=123, quiescence=contract.ProcessQuiescence("quiescent", "group-empty"))
                self.assertEqual(decision.reason, "foreground-outcome-ineligible")



class DetachedReviewLifecycleTest(unittest.TestCase):
    def _launch(self, root, ending="raise SystemExit(0)", timeout=4.0, fence_mutation=None):
        jobs = root / "jobs.log"
        gate_read, gate_write = os.pipe()
        artifact_root = root / ".agent_reports"
        artifact_root.mkdir()
        report = artifact_root / "review.md"
        log = root / "worker.jsonl"
        rows = [
            {"type": "item.completed", "item": {"type": "agent_message",
             "text": f"artifact: {report}\nverdict: PASS\nblocker: none"}},
            {"type": "turn.completed"},
        ]
        code = ("import os,pathlib,signal,time; "
                f"pathlib.Path({str(report)!r}).write_text('review evidence\\n'); "
                f"pathlib.Path({str(log)!r}).write_text({(chr(10).join(json.dumps(row) for row in rows)+chr(10))!r}); "
                + ending)
        handle = launch_review_watchdog(
            [sys.executable, str(Path(__file__).with_name("launch-fence.py")),
             "--parent-pid", str(os.getpid()), "--gate-fd", str(gate_read),
             "--jobs", str(jobs), "--attempt-id", "att-lifecycle", "--",
             sys.executable, "-c", code], gate_fd=gate_read,
            budget=begin_finite_watchdog(timeout), attempt_id="att-lifecycle",
            nonce="a" * 64, jobs=jobs,
        )
        os.close(gate_read)
        try:
            receipt = handle.read_ready(2)
            values = dict(receipt["watchdog"])
            child = receipt["child"]
            values.update({
                "artifact_root": str(artifact_root),
                "unit": "qa/code-review",
                "review_output_locator_b64": contract.encode_review_output_locator("review.md"),
                "launch_lifecycle": "detached", "launch_claimed": "1", "launch_fence": "registry-v1",
                "review_admission": "prepared", "parent_attempt_id": "",
                "review_watchdog_budget_digest": receipt["budget_digest"],
                "review_readiness_digest": receipt["receipt_digest"],
                "review_governed_lease_nonce": receipt["nonce"],
                "review_governed_lease": "summary-flock-v1",
                **{f"review_fence_{key}": child[key] for key in
                   ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")},
            })
            values["review_output_digest"] = contract.review_output_binding_digest({
                "schema_version": contract.REVIEW_OUTPUT_BINDING_SCHEMA_VERSION,
                "attempt_id": "att-lifecycle", "cycle_id": "cycle-lifecycle",
                "producer_id": "producer-lifecycle", "worktree": str(root),
                "artifact_root": str(artifact_root), "capability": "autopilot-code",
                "unit": "qa/code-review", "output_path": str(report),
            })
            # An absent parent attempt is represented by absence, not an
            # invalid empty registry field.
            _jobs(root, **values)
            jobs.write_text(jobs.read_text().replace(",parent_attempt_id=", "", 1)
                            .replace("\t/repo\t/wt\t", f"\t{root}\t{root}\t"))
            if fence_mutation:
                pipe = jobs.read_text()
                key, value = fence_mutation
                import re
                pipe = re.sub(r"(?<=,)" + key + r"=[^,\n]+", key + "=" + value, pipe)
                jobs.write_text(pipe)
            handle.commit()
            os.write(gate_write, b"1")
        finally:
            os.close(gate_write)
        return handle, jobs

    @staticmethod
    def _args(jobs, row):
        meta = row.metadata
        return type("WatcherArgs", (), {
            "jobs": jobs, "attempt_id": "att-lifecycle", "pid": int(meta["pid"]),
            "pid_start": meta["pid_start"], "pgid": int(meta["pgid"]),
            "interval": .005, "drain_interval_max": .01,
            "residue_grace": .1, "parent_recheck_interval": .01,
        })()

    def test_real_launch_fence_refuses_a_stale_child_identity_before_payload(self):
        for mutation in (("review_fence_pid_start", "1"), ("pid_observer_ns", "pid:[foreign]")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                handle, jobs = self._launch(root, fence_mutation=mutation)
                self.assertNotEqual(handle.process.wait(timeout=5), 0)
                self.assertFalse((root / ".agent_reports" / "review.md").exists())
                self.assertNotIn("launch_started", _row(jobs).metadata)

    def test_actual_watchdog_exit_closes_and_materializes_without_owner_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            handle, jobs = self._launch(root)
            self.assertEqual(handle.process.wait(timeout=5), 0)
            before = _row(jobs)
            self.assertEqual(before.metadata.get("launch_started"), "1")
            self.assertIn("review_process_outcome_b64", before.metadata)
            self.assertEqual(watcher.watch(self._args(jobs, before)), 0)
            final = _row(jobs)
            self.assertEqual(final.status, "done")
            self.assertEqual(final.metadata["note"], "completed-review", final.metadata.get("reconcile_reason"))
            records = list(root.rglob(final.metadata["delivery_id"] + ".json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text())
            self.assertEqual(record["route_id"], "review:att-lifecycle")
            self.assertEqual(record["route_node"], "_review")
            self.assertEqual(record["parent_attempt_id"], "-")
            self.assertEqual(record["attempt_ids"], ["att-lifecycle"])
            original = records[0].read_bytes()
            join.materialize_after_terminal_close(jobs, "att-lifecycle")
            self.assertEqual(records[0].read_bytes(), original)

    def test_detached_watchdog_survives_its_launcher_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "after-launcher-exit"
            ending = f"time.sleep(.3); pathlib.Path({str(marker)!r}).write_text('alive')"
            code = (
                "import importlib.util; from pathlib import Path; "
                f"s=importlib.util.spec_from_file_location('launcher_fixture', {str(Path(__file__).resolve())!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                f"m.DetachedReviewLifecycleTest()._launch(Path({td!r}), {ending!r})"
            )
            launcher = subprocess.Popen([sys.executable, "-c", code],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(launcher.wait(timeout=5), 0)
            jobs = root / "jobs.log"
            self.assertEqual(watcher.watch(self._args(jobs, _row(jobs))), 0)
            self.assertEqual(marker.read_text(), "alive")
            self.assertEqual(_row(jobs).metadata["note"], "completed-review")

    def test_actual_failure_outranks_written_pass(self):
        cases = (
            ("raise SystemExit(7)", 4, 7, "dead-exit-7"),
            ("os.kill(os.getpid(), signal.SIGTERM)", 4, -15, "dead-signal-15"),
            ("time.sleep(5)", .5, 124, "dead-timeout"),
        )
        for ending, timeout, expected_exit, expected_note in cases:
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as td:
                handle, jobs = self._launch(Path(td), ending, timeout)
                self.assertEqual(handle.process.wait(timeout=5), expected_exit)
                row = _row(jobs)
                self.assertEqual(watcher.watch(self._args(jobs, row)), 0)
                self.assertEqual(_row(jobs).metadata["note"], expected_note)
                self.assertIn("delivery_id", _row(jobs).metadata)

    def test_missing_or_corrupt_result_never_becomes_pass(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as td:
                handle, jobs = self._launch(Path(td))
                self.assertEqual(handle.process.wait(timeout=5), 0)
                fields = jobs.read_text().strip().split("\t")
                fields[5] = ",".join(part for part in fields[5].split(",")
                                    if not part.startswith("review_process_outcome_b64="))
                if corrupt:
                    fields[5] += ",review_process_outcome_b64=broken"
                jobs.write_text("\t".join(fields) + "\n")
                self.assertEqual(watcher.watch(self._args(jobs, _row(jobs))), 0)
                self.assertTrue(_row(jobs).metadata["note"].startswith("dead-"))

    def test_live_detached_watchdog_cannot_be_closed_before_result(self):
        with tempfile.TemporaryDirectory() as td:
            handle, jobs = self._launch(Path(td), "time.sleep(.3)")
            row = _row(jobs)
            args = self._args(jobs, row)
            decision = join.classify_exact_route_free_review_outcome(
                row, jobs=jobs, expected_attempt_id=args.attempt_id,
                expected_pid=args.pid, expected_pid_start=args.pid_start,
                expected_pgid=args.pgid,
                quiescence=contract.attempt_process_quiescence(row.metadata),
            )
            self.assertEqual(decision.close_action, "pending")
            self.assertEqual(handle.process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
