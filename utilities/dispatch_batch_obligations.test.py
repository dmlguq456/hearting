#!/usr/bin/env python3
"""Durable registered-batch observer ownership and restart projection tests."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_batch_obligations as obligations  # noqa: E402
import dispatch_registry_cache as registry_cache  # noqa: E402


class BatchObligationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("fixture\n", encoding="utf-8")
        self.args = Namespace(
            jobs=self.jobs, sealed_batch_id="batch-fixture",
            parent_attempt_id="att-parent", parent_session_id=None,
            attempt_id=["att-child"], delivery_parent_id="att-parent",
            control_socket=self.base / "control.sock", queue_socket=None,
            thread_id=None, interval=0.1, timeout=1.0,
        )

    def _runtime_owner_row(self, attempt="att-owner", *, status="done"):
        metadata = ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            "launch_started=1", "supervisor_lease=flock-v1",
            "supervisor_lease_file=/fixture/owner.lease", "log_file=/fixture/owner.log",
            f"attempt_id={attempt}", "workflow_completion=runtime-v1",
            "note=completed-supervisor",
        ))
        return f"2026-10-18T00:00:00Z\t{status}\t/repo\t/wt\towner\t{metadata}"

    def test_done_runtime_owner_without_sidecar_remains_discoverable_and_retries(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        with mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle:
            self.assertTrue(obligations.has_unowned_work(self.jobs))
            self.assertIn((str(self.jobs), "owner-settlement", "att-owner", "done",
                           "runtime-v1", "completed-supervisor"),
                          obligations.pending_observer_signature(self.jobs))
            obligations._resume_owner_settlements(self.jobs)
        settle.assert_called_once_with(self.jobs, "att-owner")

    def test_owner_settlement_keeps_partial_registry_retryable_until_complete(self):
        self.jobs.write_text(self._runtime_owner_row(), encoding="utf-8")
        before = self.jobs.read_bytes()
        context = obligations.OwnerSettlementReadContext()
        with mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle:
            self.assertTrue(obligations.has_unowned_work(self.jobs))
            self.assertIn((str(self.jobs), "unavailable"),
                          obligations.pending_observer_signature(self.jobs))
            obligations._resume_owner_settlements(self.jobs, context=context)
            settle.assert_not_called()
            self.assertEqual(self.jobs.read_bytes(), before)

            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            obligations._resume_owner_settlements(self.jobs, context=context)
            settle.assert_called_once_with(self.jobs, "att-owner")

    def test_pending_done_owner_stays_visible_across_cached_passes(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        registry_cache.clear_cache()
        registry_cache.reset_metrics()
        context = obligations.OwnerSettlementReadContext()

        service_script = self.base / "release" / "utilities" / "codex-managed-completion.py"
        with mock.patch("peer_obligations.service_script",
                        return_value=(service_script, {}, None)), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=True) as lease, \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle:
            first = {"signature": ()}
            self.assertEqual(obligations.ensure_observers(
                self.jobs, observer_snapshot=first, settlement_context=context), 0)
            second = {"signature": ()}
            self.assertEqual(obligations.ensure_observers(
                self.jobs, observer_snapshot=second, settlement_context=context), 0)
            self.assertEqual(registry_cache.metrics_snapshot()["content_reads"], 1)
            self.assertTrue(any(item[:3] == (str(self.jobs), "owner-settlement", "att-owner")
                                for item in first["signature"]))
            self.assertEqual(first["signature"], second["signature"])

            lease.return_value = False
            third = {"signature": ()}
            obligations.ensure_observers(
                self.jobs, observer_snapshot=third, settlement_context=context)
            fourth = {"signature": ()}
            obligations.ensure_observers(
                self.jobs, observer_snapshot=fourth, settlement_context=context)

        self.assertEqual(registry_cache.metrics_snapshot()["content_reads"], 1)
        self.assertEqual(settle.call_count, 2)
        self.assertTrue(any(item[:3] == (str(self.jobs), "owner-settlement", "att-owner")
                            for item in third["signature"]))

    def test_reconnection_writes_only_the_selected_registry_root(self):
        other_jobs = self.base / "other-jobs.log"
        other_jobs.write_text(self._runtime_owner_row("att-second") + "\n", encoding="utf-8")
        other_before = other_jobs.read_bytes()
        self.jobs.write_text(self._runtime_owner_row("att-first") + "\n", encoding="utf-8")
        service_script = self.base / "release" / "utilities" / "codex-managed-completion.py"
        def selected_or_discovered(selected, *, strict=True):
            if selected:
                path = Path(selected).expanduser()
                return [path.resolve(strict=strict) if strict else path.resolve(strict=False)]
            return [self.jobs, other_jobs]

        with mock.patch.object(obligations, "_jobs_paths", side_effect=selected_or_discovered), \
             mock.patch("peer_obligations.service_script", return_value=(service_script, {}, None)), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle:
            self.assertEqual(obligations.ensure_observers(), 0)
            settle.assert_called_once_with(self.jobs, "att-first")
            self.assertEqual(other_jobs.read_bytes(), other_before)

            # An explicitly supplied registry is authoritative for this call;
            # discovery of that same compatibility path above is read-only.
            settle.reset_mock()
            self.assertEqual(obligations.ensure_observers(other_jobs), 0)
            settle.assert_called_once_with(other_jobs, "att-second")

    def test_invalid_registry_does_not_starve_later_sidecars_or_mutate_legacy(self):
        other_root = self.base / "other-registry"
        other_root.mkdir()
        other_jobs = other_root / "jobs.log"
        other_jobs.write_text(
            self._runtime_owner_row("att-second") + "\n"
            + self._runtime_owner_row("att-live") + "\n", encoding="utf-8")
        other_before = other_jobs.read_bytes()
        other_args = Namespace(**vars(self.args))
        other_args.jobs = other_jobs
        other_args.sealed_batch_id = "batch-other-root"
        other_args.attempt_id = ["att-other-child"]
        other_args.control_socket = other_root / "control.sock"
        other_duty_id, _other_record = obligations.create(other_args)
        duty_id, _record = obligations.create(self.args)
        unknown_args = Namespace(**vars(self.args))
        unknown_args.sealed_batch_id = "batch-unknown"
        unknown_args.attempt_id = ["att-unknown"]
        unknown_id, _unknown = obligations.create(unknown_args)
        obligations.update(self.jobs, unknown_id, state="unknown",
                           reason="parent-session-ended")
        unknown_path = obligations._record_path(self.jobs, unknown_id)
        unknown_before = unknown_path.read_bytes()
        unreadable = b"\xffunreadable-registry\n"
        self.jobs.write_bytes(unreadable)

        class Launched:
            pid = 902341

        def lease_held(_jobs, metadata):
            return metadata.get("attempt_id") == "att-live"

        service_script = self.base / "release" / "utilities" / "codex-managed-completion.py"
        with mock.patch.object(obligations, "_jobs_paths", return_value=[self.jobs, other_jobs]), \
             mock.patch("peer_obligations.service_script", return_value=(service_script, {}, None)), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", side_effect=lease_held), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch.object(obligations, "_parent_duty_has_recipient", return_value=False), \
             mock.patch.object(obligations.subprocess, "Popen", return_value=Launched()) as launch:
            self.assertEqual(obligations.ensure_observers(), 2)
            signature = obligations.pending_observer_signature()
            self.assertTrue(obligations.has_unowned_work())

        settle.assert_not_called()
        self.assertEqual(launch.call_count, 2)
        launches = [call.args[0] for call in launch.call_args_list]
        self.assertEqual(
            {argv[argv.index("--jobs") + 1] for argv in launches},
            {str(self.jobs), str(other_jobs)},
        )
        self.assertTrue(any(duty_id in argv for argv in launches))
        self.assertTrue(any(other_duty_id in argv for argv in launches))
        self.assertEqual(self.jobs.read_bytes(), unreadable)
        self.assertEqual(other_jobs.read_bytes(), other_before)
        self.assertEqual(unknown_path.read_bytes(), unknown_before)
        self.assertIn((str(self.jobs), "unavailable"), signature)
        self.assertTrue(any(item[:3] == (str(self.jobs), duty_id, "observing")
                            for item in signature))
        self.assertTrue(any(item[:3] == (str(self.jobs), unknown_id, "unknown")
                            for item in signature))
        self.assertTrue(any(item[:3] == (str(other_jobs), "owner-settlement", "att-second")
                            for item in signature), "read-only census still reports discovered work")
        self.assertFalse(any(item[:3] == (str(other_jobs), "owner-settlement", "att-live")
                             for item in signature))
        self.assertTrue(any(item[:3] == (str(other_jobs), other_duty_id, "observing")
                            for item in signature))

    def test_registry_read_oserror_keeps_liveness_signature_and_later_work(self):
        other_root = self.base / "other-registry"
        other_root.mkdir()
        other_jobs = other_root / "jobs.log"
        other_jobs.write_text(self._runtime_owner_row("att-readable") + "\n", encoding="utf-8")
        other_before = other_jobs.read_bytes()
        other_args = Namespace(**vars(self.args))
        other_args.jobs = other_jobs
        other_args.sealed_batch_id = "batch-readable-root"
        other_args.attempt_id = ["att-readable-child"]
        other_args.control_socket = other_root / "control.sock"
        other_duty_id, _other_record = obligations.create(other_args)
        duty_id, _record = obligations.create(self.args)
        first_registry_before = self.jobs.read_bytes()
        read_owner_rows = obligations._pending_owner_settlements

        def fail_first_registry(path, *, context=None):
            if Path(path) == self.jobs:
                raise PermissionError("fixture registry read denied")
            return read_owner_rows(path, context=context)

        class Launched:
            pid = 902342

        with mock.patch.object(obligations, "_jobs_paths", return_value=[self.jobs, other_jobs]), \
             mock.patch("peer_obligations.service_script", return_value=(Path("/unused"), {}, None)), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch.object(obligations, "_pending_owner_settlements",
                               side_effect=fail_first_registry), \
             mock.patch.object(obligations.subprocess, "Popen", return_value=Launched()) as launch:
            self.assertEqual(obligations.ensure_observers(), 2)
            signature = obligations.pending_observer_signature()
            self.assertTrue(obligations.has_unowned_work())

        settle.assert_not_called()
        self.assertEqual(launch.call_count, 2)
        self.assertIn((str(self.jobs), "unavailable"), signature)
        self.assertTrue(any(item[:3] == (str(self.jobs), duty_id, "observing")
                            for item in signature))
        self.assertTrue(any(item[:3] == (str(other_jobs), "owner-settlement", "att-readable")
                            for item in signature))
        self.assertTrue(any(item[:3] == (str(other_jobs), other_duty_id, "observing")
                            for item in signature))
        self.assertEqual(self.jobs.read_bytes(), first_registry_before)
        self.assertEqual(other_jobs.read_bytes(), other_before)

    def test_exact_identity_and_settled_result_are_immutable(self):
        duty_id, record = obligations.create(self.args)
        self.assertEqual(record["identity"]["attempt_ids"], ["att-child"])
        settled = {"state": "ready", "children": [{"attempt_id": "att-child", "status": "done"}]}
        obligations.update(self.jobs, duty_id, state="delivery-pending", outcome=settled)
        obligations.update(self.jobs, duty_id, state="delivery-pending", reason="receiver-unavailable")
        self.assertEqual(obligations.read(self.jobs, duty_id)["outcome"], settled)
        with self.assertRaises(obligations.BatchObligationError):
            obligations.update(self.jobs, duty_id, outcome={"state": "failed"})

    def test_concurrent_resume_claim_has_one_observer(self):
        duty_id, _record = obligations.create(self.args)
        owner = obligations.acquire(self.jobs, duty_id)
        self.assertIsNotNone(owner)
        self.assertIsNone(obligations.acquire(self.jobs, duty_id))
        obligations.release(owner)
        retry = obligations.acquire(self.jobs, duty_id)
        self.assertIsNotNone(retry)
        obligations.release(retry)

    def test_startup_resumes_only_unfinished_records_with_same_identity(self):
        duty_id, _record = obligations.create(self.args)
        calls = []

        class Launched:
            pid = 901234

        with mock.patch.object(obligations.subprocess, "Popen", side_effect=lambda *a, **k: calls.append((a, k)) or Launched()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)
        command = calls[0][0][0]
        self.assertIn("--resume-obligation", command)
        self.assertIn(duty_id, command)
        self.assertEqual(command[command.index("--jobs") + 1], str(self.jobs))
        self.assertEqual(obligations.read(self.jobs, duty_id)["identity"]["attempt_ids"], ["att-child"])

    def test_observer_does_not_inherit_the_callers_attempt_tag(self):
        from dispatch_contract import ATTEMPT_DESCENDANT_ENV
        obligations.create(self.args)
        calls = []

        class Launched:
            pid = 901234

        with mock.patch.dict(os.environ, {ATTEMPT_DESCENDANT_ENV: "att-worker", "HEARTING_FIXTURE_KEEP": "1"}), \
                mock.patch.object(obligations.subprocess, "Popen",
                                  side_effect=lambda *a, **k: calls.append((a, k)) or Launched()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)
            env = dict(calls[0][1].get("env", os.environ))
        self.assertNotIn(ATTEMPT_DESCENDANT_ENV, env.keys())
        self.assertEqual(env.get("HEARTING_FIXTURE_KEEP"), "1")
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))

    def test_service_environment_also_loses_the_attempt_tag(self):
        from dispatch_contract import ATTEMPT_DESCENDANT_ENV
        obligations.create(self.args)
        calls = []
        environment = {"AGENT_HOME": "/fixture/releases/B", ATTEMPT_DESCENDANT_ENV: "att-worker"}

        class Launched:
            pid = 901235

        with mock.patch("peer_obligations.service_script",
                        return_value=(Path("/fixture/releases/B/utilities/codex-managed-completion.py"),
                                      environment, None)), \
                mock.patch.object(obligations.subprocess, "Popen",
                                  side_effect=lambda *a, **k: calls.append((a, k)) or Launched()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)
        self.assertEqual(calls[0][1]["env"], {"AGENT_HOME": "/fixture/releases/B"})

    def test_handoff_observer_does_not_inherit_attempt_tag(self):
        from dispatch_contract import ATTEMPT_DESCENDANT_ENV

        duty_id, _record = obligations.create(self.args)
        obligations.update(
            self.jobs, duty_id, state="delivery-pending",
            observer={"pid": os.getpid(), "pid_start": obligations._pid_start(os.getpid())},
        )
        fd = obligations.acquire(self.jobs, duty_id)
        self.assertIsNotNone(fd)
        environment = {
            "AGENT_HOME": "/fixture/releases/B",
            ATTEMPT_DESCENDANT_ENV: "att-worker",
            "HEARTING_FIXTURE_KEEP": "service-value",
        }
        calls = []

        class Launched:
            pid = 901236

        try:
            with mock.patch(
                "peer_obligations.service_script",
                return_value=(Path("/fixture/releases/B/utilities/codex-managed-completion.py"),
                              environment, None),
            ), mock.patch.object(
                obligations.subprocess, "Popen",
                side_effect=lambda *a, **kw: calls.append((a, kw)) or Launched(),
            ):
                self.assertTrue(obligations.handoff_observer(self.jobs, duty_id, fd))
        finally:
            os.close(fd)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["env"], {
            "AGENT_HOME": "/fixture/releases/B",
            "HEARTING_FIXTURE_KEEP": "service-value",
        })
        self.assertIn(ATTEMPT_DESCENDANT_ENV, environment)
        self.assertEqual(environment[ATTEMPT_DESCENDANT_ENV], "att-worker")

    def test_verified_service_replaces_exact_live_observer_and_passes_control_binding(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending",
                           observer={"pid": 321, "pid_start": "654"})
        root = obligations._root(self.jobs)
        lock_fd = os.open(root / (duty_id + ".lease"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = "/fixture/managed/current"
        target = Path("/fixture/releases/B/utilities/codex-managed-completion.py")
        environment = {"AGENT_HOME": "/fixture/releases/B"}
        calls = []

        class Launched:
            pid = 9002

        try:
            with mock.patch("peer_obligations.service_script",
                            return_value=(target, environment, binding)) as service_script, \
                 mock.patch("peer_obligations.replace_managed_observer",
                            return_value="replaced") as replace, \
                 mock.patch.object(obligations, "acquire", side_effect=[None, lock_fd]), \
                 mock.patch.object(obligations.subprocess, "Popen",
                                   side_effect=lambda *a, **kw: calls.append((a, kw)) or Launched()):
                self.assertEqual(obligations.ensure_observers(self.jobs, service_current=binding), 1)
            service_script.assert_called_once_with(
                "utilities/codex-managed-completion.py", binding)
            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[0:2], (321, "654"))
            argv = calls[0][0][0]
            self.assertEqual(Path(argv[1]), target)
            self.assertEqual(argv[-2:], ["--service-current", binding])
            self.assertEqual(calls[0][1]["env"], environment)
            self.assertNotIn("service_current", obligations.read(self.jobs, duty_id)["arguments"])
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def test_malformed_present_observer_start_is_not_treated_as_missing(self):
        duty_id, _record = obligations.create(self.args)
        outcome = {"state": "ready", "children": [
            {"attempt_id": "att-child", "status": "done"},
        ]}
        obligations.update(self.jobs, duty_id, state="delivery-pending", outcome=outcome,
                           observer={"pid": 321, "pid_start": ""})
        binding = "/fixture/managed/current"
        with mock.patch("peer_obligations.service_script",
                        return_value=(Path("/fixture/current/codex-managed-completion.py"),
                                      {}, binding)), \
             mock.patch.object(obligations, "acquire", return_value=None), \
             mock.patch("peer_obligations.replace_managed_observer") as replace, \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(
                self.jobs, service_current=binding), 0)
        replace.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(obligations.read(self.jobs, duty_id)["observer"],
                         {"pid": 321, "pid_start": ""})

    def test_complete_duty_is_not_replayed(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="complete", outcome={"state": "ready"},
                           delivery="accepted")
        with mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
        launch.assert_not_called()

    def test_absent_compatibility_slots_are_known_empty(self):
        roots = (self.base / "release" / ".dispatch",
                 self.base / "xdg-state" / "hearting" / "dispatch")
        with mock.patch("dispatch_contract.resolve_agent_home", return_value=self.base / "release"), \
             mock.patch("dispatch_contract.dispatch_state_roots", return_value=roots):
            self.assertEqual(obligations.pending_observer_signature(), ())
            self.assertFalse(obligations.has_unowned_work())

        unused_jobs = self.base / "unused" / "jobs.log"
        unused_jobs.parent.mkdir()
        self.assertEqual(obligations.pending_observer_signature(unused_jobs), ())
        self.assertFalse(obligations.has_unowned_work(unused_jobs))

    def test_empty_existing_registry_and_sidecar_state_are_known_empty(self):
        empty_jobs = self.base / "empty" / "jobs.log"
        empty_jobs.parent.mkdir(parents=True)
        empty_jobs.touch()
        (empty_jobs.parent / "supervisor-state" / "obligations").mkdir(parents=True)
        self.assertEqual(obligations.pending_observer_signature(empty_jobs), ())
        self.assertFalse(obligations.has_unowned_work(empty_jobs))

    def test_missing_registry_keeps_durable_pending_records_visible(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending",
                           reason="awaiting-recipient")
        self.jobs.unlink()

        signature = obligations.pending_observer_signature(self.jobs)
        self.assertEqual(len(signature), 1)
        self.assertEqual(signature[0][:3], (str(self.jobs), duty_id, "delivery-pending"))
        self.assertTrue(obligations.has_unowned_work(self.jobs))
        with self.assertRaises(obligations.BatchObligationError):
            obligations._root(self.jobs)

    def test_unreadable_malformed_and_symlink_state_remains_unknown(self):
        root = obligations._root(self.jobs)
        root.mkdir(parents=True)
        with mock.patch.object(obligations.os, "scandir", side_effect=PermissionError("protected")):
            self.assertEqual(obligations.pending_observer_signature(self.jobs),
                             ((str(self.jobs), "unavailable"),))
            self.assertTrue(obligations.has_unowned_work(self.jobs))
            snapshot = obligations.pending_observer_snapshot(self.jobs)
            self.assertFalse(snapshot["cacheable"])
            self.assertEqual(snapshot["paths_by_jobs"][str(self.jobs)], None)

        malformed = root / "batch-malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")
        self.assertEqual(obligations.pending_observer_signature(self.jobs),
                         ((str(self.jobs), malformed.name, "unreadable"),))
        self.assertTrue(obligations.has_unowned_work(self.jobs))
        self.assertFalse(obligations.pending_observer_snapshot(self.jobs)["cacheable"])

        protected_jobs = self.base / "protected" / "jobs.log"
        protected_jobs.parent.mkdir(parents=True)
        protected_jobs.touch()
        protected_root = protected_jobs.parent / "supervisor-state" / "obligations"
        target = self.base / "protected-target"
        target.mkdir()
        protected_root.parent.mkdir()
        protected_root.symlink_to(target, target_is_directory=True)
        self.assertEqual(obligations.pending_observer_signature(protected_jobs),
                         ((str(protected_jobs), "unavailable"),))
        self.assertFalse(obligations.pending_observer_snapshot(protected_jobs)["cacheable"])

    def test_pending_snapshot_reuses_only_stable_readable_directory_pair(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending",
                           reason="awaiting-recipient")
        original = obligations._read_only_records
        key = str(self.jobs.resolve(strict=False))

        with mock.patch.object(obligations, "_read_only_records", wraps=original) as scan:
            snapshot = obligations.pending_observer_snapshot(self.jobs)
            self.assertTrue(snapshot["cacheable"])
            self.assertIn(duty_id, [item[1] for item in snapshot["signature"]])
            self.assertEqual(snapshot["root_stamps"][key], obligations._directory_stamp(
                snapshot["roots_by_jobs"][key]))
            scan.assert_called_once_with(self.jobs)

    def test_sidecar_created_between_scan_and_after_stamp_forces_refresh(self):
        original = obligations._read_only_records
        created = []

        def scan_then_publish(jobs):
            records = original(jobs)
            if not created:
                duty_id, _record = obligations.create(self.args)
                obligations.update(jobs, duty_id, state="delivery-pending",
                                   reason="accepted-during-snapshot")
                created.append(duty_id)
            return records

        with mock.patch.object(obligations, "_read_only_records", side_effect=scan_then_publish):
            stale = obligations.pending_observer_snapshot(self.jobs)
        self.assertFalse(stale["cacheable"])
        self.assertNotIn(created[0], [item[1] for item in stale["signature"]])

        refreshed = obligations.pending_observer_snapshot(self.jobs)
        self.assertTrue(refreshed["cacheable"])
        self.assertIn(created[0], [item[1] for item in refreshed["signature"]])

    def test_unowned_work_probe_is_read_only_and_uses_exact_observer_identity(self):
        duty_id, _record = obligations.create(self.args)
        path = obligations._record_path(self.jobs, duty_id)
        before = path.read_bytes()
        self.assertTrue(obligations.has_unowned_work(self.jobs))
        self.assertEqual(path.read_bytes(), before)

        obligations.update(self.jobs, duty_id, observer={
            "pid": os.getpid(), "pid_start": obligations._pid_start(os.getpid())})
        self.assertFalse(obligations.has_unowned_work(self.jobs))
        obligations.update(self.jobs, duty_id, state="complete")
        self.assertFalse(obligations.has_unowned_work(self.jobs))

    def test_runner_signature_ignores_observer_relaunches_but_not_duty_progress(self):
        # The runner resets its bounded backoff on a signature change. Its own
        # relaunch of a still-pending duty (a new observer PID, live or gone)
        # is not new work; a duty state or delivery change is.
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending", delivery="retryable",
                           observer={"pid": os.getpid(),
                                     "pid_start": obligations._pid_start(os.getpid())})
        first = obligations.pending_observer_signature(self.jobs)
        obligations.update(self.jobs, duty_id, reason="observing",
                           observer={"pid": 2 ** 22 + 7, "pid_start": "1"})
        self.assertEqual(obligations.pending_observer_signature(self.jobs), first)
        obligations.update(self.jobs, duty_id, delivery="sent-ambiguous")
        self.assertNotEqual(obligations.pending_observer_signature(self.jobs), first)

    def test_request_runner_uses_the_common_carrier_recovery_entry(self):
        with mock.patch("peer_obligations.request_dispatch_reconciliation",
                        return_value=True) as request:
            self.assertTrue(obligations.request_runner(self.jobs))
        request.assert_called_once_with(self.jobs.resolve().parent)
        with mock.patch("peer_obligations.request_dispatch_reconciliation",
                        side_effect=OSError("unavailable")):
            self.assertFalse(obligations.request_runner(self.jobs))

    def test_owner_settlement_scan_reuses_unchanged_registry_but_rechecks_leases(self):
        from dispatch_contract import SUPERVISOR_LEASE_KIND

        row = (
            "t\topen\t/repo\t/wt\towner\t"
            f"worker_type=owner,launch_started=1,supervisor_lease={SUPERVISOR_LEASE_KIND},"
            "supervisor_lease_file=attempt.lock,log_file=attempt.log,attempt_id=att-owner\n"
        )
        self.jobs.write_text(row, encoding="utf-8")
        registry_cache.clear_cache()
        registry_cache.reset_metrics()
        context = obligations.OwnerSettlementReadContext()

        with mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=True) as lease, \
             mock.patch("dispatch_completion_join.settle_finished_attempt") as settle:
            obligations._resume_owner_settlements(self.jobs, context=context)
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(registry_cache.metrics_snapshot()["content_reads"], 1)
            self.assertEqual(lease.call_count, 2)
            settle.assert_not_called()

            # A registry change forces a fresh full read on the next pass.
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write("t\tdone\t/repo\t/wt\tchild\tattempt_id=att-child\n")
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(registry_cache.metrics_snapshot()["content_reads"], 2)

            # Lease release is checked even while the registry stamp is stable.
            lease.return_value = False
            obligations._resume_owner_settlements(self.jobs, context=context)
            settle.assert_called_once()
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(settle.call_count, 2, "an unresolved unowned row stays retryable")
            self.assertEqual(registry_cache.metrics_snapshot()["content_reads"], 2,
                             "retryable settlement reuses unchanged registry content")

    def test_settled_owner_cache_survives_unrelated_append_and_invalidates_dependencies(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        dependency = self.base / "terminal-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        registry_cache.clear_cache()
        context = obligations.OwnerSettlementReadContext()
        context._dependency_watcher_fallback = True
        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending",
                        side_effect=[False, True, True]) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            self.assertEqual(obligations._resume_owner_settlements(
                self.jobs, context=context), ())
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write("2026-10-19T00:00:00Z\tdone\t/repo\t/wt\tchild\t"
                             "attempt_id=att-unrelated\n")
            self.assertEqual(obligations._resume_owner_settlements(
                self.jobs, context=context), ())
            pending.assert_called_once()

            dependency.write_text("changed", encoding="utf-8")
            resumed = obligations._resume_owner_settlements(self.jobs, context=context)
        self.assertTrue(any(signature[2] == "att-owner" for signature in resumed))
        self.assertEqual(pending.call_count, 3)

    def test_dependency_sweep_progresses_during_appends_and_an_active_owner(self):
        settled_count = obligations._SETTLED_DEPENDENCY_CHECKS_PER_PASS + 1
        target = f"att-settled-{settled_count - 1:03d}"
        rows = [self._runtime_owner_row(f"att-settled-{index:03d}")
                for index in range(settled_count)]
        rows.append(self._runtime_owner_row("att-active", status="open"))
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
        dependencies = {}
        for index in range(settled_count):
            path = self.base / f"terminal-evidence-{index:03d}.json"
            path.write_text("sealed", encoding="utf-8")
            dependencies[f"att-settled-{index:03d}"] = path
        context = obligations.OwnerSettlementReadContext()
        historical = []
        target_checks = 0

        def owner_pending(_jobs, _status, metadata):
            nonlocal target_checks
            attempt_id = metadata["attempt_id"]
            historical.append(attempt_id)
            if attempt_id == target:
                target_checks += 1
                return target_checks > 1
            return False

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               side_effect=lambda _jobs, _status, metadata:
                                   (dependencies[metadata["attempt_id"]],)), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending",
                        side_effect=owner_pending), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch("dispatch_contract.supervisor_lease_is_held",
                        side_effect=lambda _jobs, metadata:
                            metadata.get("attempt_id") == "att-active"):
            obligations._resume_owner_settlements(self.jobs, context=context)
            dependencies[target].write_text("changed", encoding="utf-8")

            passes = (settled_count + obligations._SETTLED_DEPENDENCY_CHECKS_PER_PASS - 1) // \
                     obligations._SETTLED_DEPENDENCY_CHECKS_PER_PASS
            completed_passes = None
            for index in range(passes):
                with self.jobs.open("a", encoding="utf-8") as stream:
                    stream.write(f"t\tdone\t/repo\t/wt\tchild\t"
                                 f"attempt_id=att-unrelated-{index}\n")
                obligations._resume_owner_settlements(self.jobs, context=context)
                if target_checks > 1:
                    completed_passes = index + 1
                    break

        self.assertEqual(passes, 2)
        self.assertIsNotNone(completed_passes, "the invalidated owner reaches settlement")
        self.assertLessEqual(completed_passes, passes)
        self.assertEqual(historical.count(target), 3,
                         "the changed owner is rechecked and sent to settlement")
        self.assertEqual(context.metrics()["dependency_invalidations"], 1)
        settle.assert_called_once_with(self.jobs, target)

    def _watch_context(self):
        context = obligations.OwnerSettlementReadContext()
        if not context._ensure_dependency_watcher():
            context.close()
            self.skipTest("inotify is unavailable")
        self.addCleanup(context.close)
        return context

    def _remember_dependency(self, context, attempt, dependency):
        return context.remember_settled_owner(
            self.jobs, "done", f"row-{attempt}", {"attempt_id": attempt}, (),
            (dependency,))

    def test_dependency_watch_churn_releases_directory_and_filesystem_entries(self):
        context = self._watch_context()
        watcher_fd = context._dependency_watch_fd

        for index in range(32):
            directory = self.base / f"watch-churn-{index}"
            directory.mkdir()
            dependency = directory / "terminal.json"
            dependency.write_text("sealed", encoding="utf-8")
            attempt = f"att-churn-{index}"
            self.assertTrue(self._remember_dependency(context, attempt, dependency))
            self.assertEqual(len(context._dependency_watch_dirs), 1)
            self.assertTrue(context._dependency_watch_fs_support)

            context.forget_owner(self.jobs, attempt)
            self.assertFalse(context._dependency_watch_dirs)
            self.assertFalse(context._dependency_watch_paths)
            self.assertFalse(context._dependency_watch_keys)
            self.assertFalse(context._dependency_watch_fs_support)
            self.assertEqual(os.fstat(watcher_fd).st_mode != 0, True)

        context.close()
        self.assertEqual(context._dependency_watch_fd, -1)
        with self.assertRaises(OSError):
            os.fstat(watcher_fd)

    def test_dependency_watch_shares_directory_and_alias_ownership_until_last_owner(self):
        context = self._watch_context()
        shared = self.base / "watch-shared"
        shared.mkdir()
        alias = self.base / "watch-shared-alias"
        alias.symlink_to(shared, target_is_directory=True)
        first_path = shared / "first.json"
        second_path = alias / "second.json"
        first_path.write_text("one", encoding="utf-8")
        second_path.write_text("two", encoding="utf-8")

        self.assertTrue(self._remember_dependency(context, "att-shared-first", first_path))
        self.assertTrue(self._remember_dependency(context, "att-shared-same", first_path))
        self.assertTrue(self._remember_dependency(context, "att-shared-alias", second_path))
        first_key = (str(self.jobs.resolve()), "att-shared-first")
        same_key = (str(self.jobs.resolve()), "att-shared-same")
        alias_key = (str(self.jobs.resolve()), "att-shared-alias")
        first_pairs = context._dependency_watch_keys[first_key]
        alias_pairs = context._dependency_watch_keys[alias_key]
        shared_wd = first_pairs[0][0]
        self.assertEqual({pair[0] for pair in alias_pairs}, {shared_wd})
        self.assertEqual(context._dependency_watch_paths[(shared_wd, "first.json")],
                         {first_key, same_key})
        self.assertEqual(set(context._dependency_watch_dirs.values()), {shared_wd})
        self.assertEqual(set(context._dependency_watch_dirs), {str(shared), str(alias)})

        watcher_fd = context._dependency_watch_fd
        remove = context._dependency_watch_remove
        with mock.patch.object(context, "_dependency_watch_remove", wraps=remove) as rm_watch:
            context.forget_owner(self.jobs, "att-shared-first")
            self.assertEqual(context._dependency_watch_paths[(shared_wd, "first.json")],
                             {same_key})
            context.forget_owner(self.jobs, "att-shared-same")
            self.assertIn((shared_wd, "second.json"), context._dependency_watch_paths)
            rm_watch.assert_not_called()

            context.forget_owner(self.jobs, "att-shared-alias")
            rm_watch.assert_called_once_with(watcher_fd, shared_wd)

        self.assertFalse(context._dependency_watch_dirs)
        self.assertFalse(context._dependency_watch_paths)
        self.assertFalse(context._dependency_watch_fs_support)

    def test_dependency_watcher_closes_on_retirement_and_stat_fallback(self):
        closing = self._watch_context()
        closing_fd = closing._dependency_watch_fd
        closing.close()
        self.assertEqual(closing._dependency_watch_fd, -1)
        with self.assertRaises(OSError):
            os.fstat(closing_fd)

        fallback = self._watch_context()
        fallback_fd = fallback._dependency_watch_fd
        dependency = self.base / "fallback-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        key = (str(self.jobs.resolve()), "att-fallback")
        with mock.patch.object(fallback, "_dependency_watch_supported", return_value=False):
            fallback._register_dependency_watch(key, (dependency,))

        self.assertTrue(fallback._dependency_watcher_fallback)
        self.assertEqual(fallback._dependency_watch_fd, -1)
        self.assertFalse(fallback._dependency_watch_dirs)
        self.assertFalse(fallback._dependency_watch_fs_support)
        with self.assertRaises(OSError):
            os.fstat(fallback_fd)

    def test_snapshot_install_race_invalidates_proof_and_reaches_settlement(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        registry_cache.clear_cache()
        dependency = self.base / "race-evidence.json"
        dependency.write_text("before", encoding="utf-8")
        context = self._watch_context()
        register = context._register_dependency_watch

        def replace_between_snapshot_and_install(key, paths):
            replacement = dependency.with_suffix(".replacement")
            replacement.write_text("after-atomic-replace", encoding="utf-8")
            os.replace(replacement, dependency)
            register(key, paths)

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch.object(context, "_register_dependency_watch",
                               side_effect=replace_between_snapshot_and_install), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending",
                        side_effect=(False, True)) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            signatures = obligations._resume_owner_settlements(self.jobs, context=context)

        self.assertEqual(pending.call_count, 2)
        settle.assert_called_once_with(self.jobs, "att-owner")
        self.assertEqual(context.metrics()["dependency_invalidations"], 1)
        self.assertNotIn((str(self.jobs.resolve()), "att-owner"), context._settled)
        self.assertTrue(any(item[:3] == (str(self.jobs), "owner-settlement", "att-owner")
                            for item in signatures))

    def test_post_registration_snapshot_disappearance_reaches_settlement(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        registry_cache.clear_cache()
        dependency = self.base / "disappearing-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        context = obligations.OwnerSettlementReadContext()
        current_generations = context._current_generations
        snapshots = 0

        def disappear_after_initial_snapshot(paths, cache=None):
            nonlocal snapshots
            snapshots += 1
            if snapshots == 1:
                observed = current_generations(paths, cache)
                dependency.unlink()
                return observed
            return current_generations(paths)

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch.object(context, "_current_generations",
                               side_effect=disappear_after_initial_snapshot), \
             mock.patch.object(context, "_register_dependency_watch") as register, \
             mock.patch("dispatch_terminal_commit.owner_completion_pending",
                        side_effect=(False, True)) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            signatures = obligations._resume_owner_settlements(self.jobs, context=context)

        self.assertEqual(snapshots, 2)
        register.assert_called_once()
        self.assertEqual(pending.call_count, 2)
        settle.assert_called_once_with(self.jobs, "att-owner")
        self.assertNotIn((str(self.jobs.resolve()), "att-owner"), context._settled)
        self.assertTrue(any(item[:3] == (str(self.jobs), "owner-settlement", "att-owner")
                            for item in signatures))

    def test_initial_registration_snapshot_error_reaches_settlement(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        registry_cache.clear_cache()
        dependency = self.base / "unreadable-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        context = obligations.OwnerSettlementReadContext()

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch.object(context, "_current_generations",
                               side_effect=PermissionError("injected stat failure")), \
             mock.patch.object(context, "_register_dependency_watch") as register, \
             mock.patch("dispatch_terminal_commit.owner_completion_pending",
                        side_effect=(False, True)) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            signatures = obligations._resume_owner_settlements(self.jobs, context=context)

        register.assert_not_called()
        self.assertEqual(pending.call_count, 2)
        settle.assert_called_once_with(self.jobs, "att-owner")
        self.assertNotIn((str(self.jobs.resolve()), "att-owner"), context._settled)
        self.assertTrue(any(item[:3] == (str(self.jobs), "owner-settlement", "att-owner")
                            for item in signatures))

    def test_owner_settlement_accepts_valid_literal_replacement_character(self):
        row = self._runtime_owner_row().replace(
            "completed-supervisor", "completed-supervisor\ufffd")
        self.jobs.write_text(row + "\n", encoding="utf-8")
        registry_cache.clear_cache()
        registry_cache.reset_metrics()
        with mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=True), \
             mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True):
            signatures = obligations._resume_owner_settlements(self.jobs)

        self.assertIn((str(self.jobs), "owner-settlement", "att-owner", "done",
                       "runtime-v1", "completed-supervisor\ufffd"), signatures)
        self.assertFalse(any(item[:2] == (str(self.jobs), "owner-settlement-unavailable")
                             for item in signatures))

    def test_parent_ended_duty_waits_dormant_until_recipient_returns(self):
        args = Namespace(**vars(self.args))
        args.parent_session_id = "parent-thread"
        args.parent_attempt_id = None
        args.thread_id = "parent-thread"
        args.delivery_parent_id = "parent-thread"
        args.parent_lifetime = {"pid": 123, "pid_start": "456", "pane": "w1:p2",
                                "session_id": "parent-thread", "pid_namespace": "ns"}
        duty_id, _record = obligations.create(args)
        obligations.update(self.jobs, duty_id, state="unknown", reason="parent-session-ended",
                           parent_lifetime=args.parent_lifetime)
        with mock.patch.object(obligations, "_parent_duty_has_recipient", return_value=False), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
        launch.assert_not_called()
        with mock.patch.object(obligations, "_parent_duty_has_recipient", return_value=True), \
             mock.patch.object(obligations.subprocess, "Popen", return_value=type("P", (), {"pid": 9001})()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)


class _Handover:
    """Stand-ins for the registry's handover rows and the seat-handover lineage."""

    def __init__(self, rows):
        self.rows = rows

    def handover_attempt(self, attempt):
        return self.rows.get(attempt)


class DeliveryDutyLifetimeTest(unittest.TestCase):
    PARENT = "thread-parent-fixture"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("fixture\n", encoding="utf-8")
        self.processes = []
        self.addCleanup(self._stop_processes)

    def _stop_processes(self):
        for process in self.processes:
            process.kill()
            process.wait(timeout=5)
            process.stdout.close()

    def _args(self, batch, attempt, **overrides):
        values = dict(
            jobs=self.jobs, sealed_batch_id=batch, parent_attempt_id=None,
            parent_session_id=self.PARENT, thread_id=self.PARENT, attempt_id=[attempt],
            delivery_parent_id=self.PARENT, control_socket=None,
            queue_socket=self.base / "queue.sock", interval=0.1, timeout=3600.0)
        values.update(overrides)
        return Namespace(**values)

    def _duty(self, batch, attempt, **state):
        duty_id, record = obligations.create(self._args(batch, attempt))
        if state:
            obligations.update(self.jobs, duty_id, **state)
        return duty_id, obligations.read(self.jobs, duty_id)

    def _service(self):
        script = self.base / "release" / "utilities" / "codex-managed-completion.py"
        return mock.patch("peer_obligations.service_script",
                          return_value=(script, {}, "/fixture/managed/current"))

    # -- D2: only a successor that can receive the completion keeps the duty --

    def _successor(self, rows, *, harness="codex", successor="successor-thread",
                   tui=(), current_session=None):
        import dispatch_seat_handover as handover
        view = _Handover(rows)
        probe = mock.Mock(return_value=list(tui))
        with mock.patch("dispatch_registry_cache.registry_view", return_value=view), \
             mock.patch.object(handover, "resolve_recipient", return_value=handover.RecipientResolution(
                 successor, harness, "confirmed", self.PARENT, "codex")), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", probe):
            result = obligations.parent_successor(
                self.jobs, self.PARENT, ["att-a"], current_session=current_session)
        return result, probe

    def test_successor_ownership_is_independent_of_carrier_and_live_tui(self):
        rows = {"att-a": ("done", {"parent_sid": self.PARENT})}
        self.assertEqual(self._successor(rows, tui=[{"pid": 1}])[0], "successor-thread")
        self.assertEqual(self._successor(rows, harness="claude", tui=[{"pid": 1}])[0], "successor-thread")
        self.assertEqual(self._successor(rows, tui=[])[0], "successor-thread")
        self.assertEqual(self._successor({})[0], "")
        self.assertEqual(self._successor(rows, successor=self.PARENT, tui=[{"pid": 1}])[0], "")

    def test_known_current_session_is_matched_without_a_tui_lookup(self):
        rows = {"att-a": ("done", {"parent_sid": self.PARENT})}
        result, probe = self._successor(rows, current_session="successor-thread")
        self.assertEqual(result, "successor-thread")
        probe.assert_not_called()
        self.assertEqual(self._successor(rows, current_session="someone-else")[0], "")

    def test_successor_reads_each_attempt_by_index_not_every_registry_row(self):
        view = mock.Mock(spec=["handover_attempt"])
        view.handover_attempt.return_value = None
        with mock.patch("dispatch_registry_cache.registry_view", return_value=view):
            self.assertEqual(obligations.parent_successor(
                self.jobs, self.PARENT, ["att-a", "att-b"]), "")
        self.assertEqual([call.args for call in view.handover_attempt.call_args_list],
                         [("att-a",), ("att-b",)])

    def test_unreadable_handover_evidence_keeps_the_runner_duty(self):
        _duty_id, record = self._duty("batch-a", "att-a", state="unknown",
                                      reason="parent-session-ended")
        with mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(obligations, "parent_successor", side_effect=OSError("registry")):
            self.assertTrue(obligations._parent_duty_has_recipient(record, self.jobs))

    # -- D1: a duty without a recorded TUI binding is judged by the session TUI --

    def test_unbound_duty_follows_the_session_tui_and_deliverable_successor(self):
        _duty_id, record = self._duty("batch-a", "att-a", state="unknown",
                                      reason="parent-session-ended")
        self.assertNotIn("parent_lifetime", record)
        tui = [{"pid": 4242, "pid_start": "1", "session_id": self.PARENT}]
        with mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(obligations, "parent_successor", return_value=""):
            self.assertFalse(obligations._parent_duty_has_recipient(record, self.jobs))
        with mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=tui):
            self.assertTrue(obligations._parent_duty_has_recipient(record, self.jobs))
        with mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]), \
             mock.patch.object(obligations, "parent_successor", return_value="successor"):
            self.assertTrue(obligations._parent_duty_has_recipient(record, self.jobs))
        with mock.patch("pane_ownership.codex_tui_processes_for_session",
                        side_effect=OSError("proc")):
            self.assertTrue(obligations._parent_duty_has_recipient(record, self.jobs))

    def test_runner_leaves_an_unbound_ended_duty_asleep_until_its_tui_returns(self):
        duty_id, _record = self._duty("batch-a", "att-a", state="unknown",
                                      reason="parent-session-ended")
        tui = [{"pid": 4242, "pid_start": "1", "session_id": self.PARENT}]

        class Launched:
            pid = 903001

        with self._service(), \
             mock.patch.object(obligations.subprocess, "Popen", return_value=Launched()) as launch:
            with mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=[]):
                self.assertEqual(obligations.ensure_observers(self.jobs), 0)
            launch.assert_not_called()
            self.assertEqual(obligations.read(self.jobs, duty_id)["state"], "unknown")
            with mock.patch("pane_ownership.codex_tui_processes_for_session", return_value=tui):
                self.assertEqual(obligations.ensure_observers(self.jobs), 1)
            launch.assert_called_once()

    def test_runner_wakes_an_unbound_ended_duty_for_a_deliverable_successor(self):
        self._duty("batch-a", "att-a", state="unknown", reason="parent-session-ended")

        class Launched:
            pid = 903002

        def probe(session):
            return [{"pid": 7, "pid_start": "1", "session_id": session}] \
                if session == "successor-thread" else []

        with self._service(), \
             mock.patch.object(obligations, "_resume_owner_settlements", return_value=[]), \
             mock.patch("pane_ownership.codex_tui_processes_for_session", side_effect=probe), \
             mock.patch("dispatch_registry_cache.registry_view",
                        return_value=_Handover({"att-a": ("done", {})})), \
             mock.patch("dispatch_seat_handover.resolve_recipient", return_value=Namespace(
                 session_id="successor-thread", harness="codex", state="confirmed")), \
             mock.patch.object(obligations.subprocess, "Popen", return_value=Launched()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)

    # -- D3: first-generation observers join the existing replacement path --

    def _first_generation(self, batch, attempt, *, extra=()):
        script = self.base / "bin" / "codex-managed-completion.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n",
                          encoding="utf-8")
        argv = ["--queue-socket", str(self.base / "queue.sock"), "--jobs", str(self.jobs),
                "--parent-session-id", self.PARENT, "--thread-id", self.PARENT,
                "--sealed-batch-id", batch, "--launch-ready-timeout", "60",
                "--timeout", "3600.0", "--attempt-id", attempt, *extra]
        process = subprocess.Popen([sys.executable, str(script), *argv],
                                   stdout=subprocess.PIPE, text=True)
        self.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process, argv

    def test_first_generation_argv_is_a_variant_only_for_its_own_batch(self):
        _id_a, record_a = self._duty("batch-a", "att-a")
        _id_b, record_b = self._duty("batch-b", "att-b")
        process_a, argv_a = self._first_generation("batch-a", "att-a")
        process_b, argv_b = self._first_generation("batch-b", "att-b")
        self.assertEqual(obligations._observer_argument_variants(process_a.pid, record_a),
                         (tuple(argv_a),))
        self.assertEqual(obligations._observer_argument_variants(process_b.pid, record_b),
                         (tuple(argv_b),))
        self.assertEqual(obligations._observer_argument_variants(process_a.pid, record_b), ())
        self.assertEqual(obligations._observer_argument_variants(process_b.pid, record_a), ())

    def test_variant_ignores_control_options_and_refuses_resumed_or_unreadable_argv(self):
        _id_a, record_a = self._duty("batch-a", "att-a")
        process, argv = self._first_generation(
            "batch-a", "att-a", extra=("--service-current", "/fixture/managed/current"))
        self.assertEqual(obligations._observer_argument_variants(process.pid, record_a),
                         (tuple(argv[:-2]),))
        resumed, _argv = self._first_generation(
            "batch-a", "att-a", extra=("--resume-obligation", "batch-a"))
        self.assertEqual(obligations._observer_argument_variants(resumed.pid, record_a), ())
        self.assertEqual(obligations._observer_argument_variants(2 ** 22 + 7, record_a), ())
        altered = dict(record_a, identity=dict(record_a["identity"], attempt_ids=["att-z"]))
        self.assertEqual(obligations._observer_argument_variants(process.pid, altered), ())

    def test_runner_offers_each_observer_only_its_own_first_generation_argv(self):
        id_a, record_a = self._duty("batch-a", "att-a", state="observing")
        id_b, record_b = self._duty("batch-b", "att-b", state="observing")
        process_a, argv_a = self._first_generation("batch-a", "att-a")
        process_b, argv_b = self._first_generation("batch-b", "att-b")
        for duty_id, process in ((id_a, process_a), (id_b, process_b)):
            obligations.update(self.jobs, duty_id, observer={
                "pid": process.pid, "pid_start": obligations._pid_start(process.pid)})
        before_b = obligations.read(self.jobs, id_b)
        offered = {}

        def replace(pid, _start, lock, _script, _required, _binding, **kwargs):
            offered[Path(lock).stem] = (pid, kwargs)
            return False

        with self._service(), \
             mock.patch.object(obligations, "acquire", return_value=None), \
             mock.patch("peer_obligations.replace_managed_observer", side_effect=replace), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
        launch.assert_not_called()
        self.assertEqual(set(offered), {id_a, id_b})
        for duty_id, process, argv in ((id_a, process_a, argv_a), (id_b, process_b, argv_b)):
            pid, kwargs = offered[duty_id]
            self.assertEqual(pid, process.pid)
            self.assertTrue(kwargs["exact_arguments"])
            self.assertEqual(kwargs["argument_variants"], (tuple(argv),))
        self.assertEqual(obligations.read(self.jobs, id_b)["observer"], before_b["observer"])

    def test_old_first_generation_observer_is_replaced_by_the_resumed_form(self):
        duty_id, _record = self._duty("batch-a", "att-a", state="observing")
        process, argv = self._first_generation("batch-a", "att-a")
        obligations.update(self.jobs, duty_id, observer={
            "pid": process.pid, "pid_start": obligations._pid_start(process.pid)})
        lock_fd = os.open(obligations._root(self.jobs) / (duty_id + ".lease"),
                          os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        class Launched:
            pid = 903003

        try:
            with self._service(), \
                 mock.patch.object(obligations, "acquire", side_effect=[None, lock_fd]), \
                 mock.patch("peer_obligations.replace_managed_observer",
                            return_value="replaced") as replace, \
                 mock.patch.object(obligations.subprocess, "Popen",
                                   return_value=Launched()) as launch:
                self.assertEqual(obligations.ensure_observers(self.jobs), 1)
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        self.assertEqual(replace.call_args.kwargs["argument_variants"], (tuple(argv),))
        self.assertIn("--resume-obligation", launch.call_args.args[0])
        self.assertIn(duty_id, launch.call_args.args[0])

    # -- C-B: one observer per undelivered duty, delivered duties are not woken --

    def test_two_unbound_batches_of_one_parent_each_keep_one_observer(self):
        id_a, _a = self._duty("batch-a", "att-a", state="observing")
        id_b, _b = self._duty("batch-b", "att-b", state="observing")
        seen = []

        def replace(pid, *_args, **_kwargs):
            seen.append(pid)
            return "current"

        for duty_id, pid in ((id_a, 811), (id_b, 812)):
            obligations.update(self.jobs, duty_id, observer={"pid": pid, "pid_start": "1"})
        with self._service(), \
             mock.patch.object(obligations, "acquire", return_value=None), \
             mock.patch("peer_obligations.replace_managed_observer", side_effect=replace), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
            launch.assert_not_called()
            self.assertEqual(sorted(seen), [811, 812])
            obligations.update(self.jobs, id_a, state="complete", delivery="accepted",
                               outcome={"state": "ready"})
            seen.clear()
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
        self.assertEqual(seen, [812])


if __name__ == "__main__":
    unittest.main()
