#!/usr/bin/env python3
"""Durable registered-batch observer ownership and restart projection tests."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import random
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
from split_module_fixture import split_patch  # noqa: E402
from dispatch_terminal_commit import CompletionState  # noqa: E402

PENDING = CompletionState("pending")
COMPLETE = CompletionState("complete")


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
        with split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING), \
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
        with split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING), \
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
        clock = [1000.0]
        context._clock = lambda: clock[0]

        service_script = self.base / "release" / "utilities" / "codex-managed-completion.py"
        with mock.patch("peer_obligations.service_script",
                        return_value=(service_script, {}, None)), \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=True) as lease, \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING), \
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
            # Unchanged inputs defer the next attempt to the owner's retry time.
            clock[0] += 601.0
            fourth = {"signature": ()}
            obligations.ensure_observers(
                self.jobs, observer_snapshot=fourth, settlement_context=context)
            fifth = {"signature": ()}
            obligations.ensure_observers(
                self.jobs, observer_snapshot=fifth, settlement_context=context)

        self.assertEqual(registry_cache.metrics_snapshot()["content_reads"], 1)
        self.assertEqual(settle.call_count, 2)
        self.assertTrue(any(item[:3] == (str(self.jobs), "owner-settlement", "att-owner")
                            for item in third["signature"]))
        self.assertEqual(third["signature"], fifth["signature"],
                         "the deferred settlement stays in the outer signature")

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
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING), \
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
             split_patch("dispatch_contract.supervisor_lease_is_held", side_effect=lease_held), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING), \
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
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING), \
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
             split_patch("dispatch_contract.dispatch_state_roots", return_value=roots):
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

        with split_patch("dispatch_contract.supervisor_lease_is_held", return_value=True) as lease, \
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

    def test_runner_owner_proof_budget_resumes_across_passes(self):
        """A just-started runner proves cold owner history a slice per pass.

        2026-11-11: after each install the runner re-proved every finished
        owner on the shared artifact roots in its first pass (882 s, 816,956
        NAS reads) while accepted notices waited for the pass to end.
        """
        rows = [self._runtime_owner_row(f"att-cold-{index}") for index in range(5)]
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
        dependency = self.base / "terminal-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        registry_cache.clear_cache()
        now = [0.0]
        proven = []

        def prove(_jobs, _status, metadata):
            proven.append(metadata["attempt_id"])
            now[0] += 10.0  # each proof costs more than the pass budget
            return COMPLETE

        unbudgeted = obligations.OwnerSettlementReadContext()
        unbudgeted._dependency_watcher_fallback = True
        budgeted = obligations.OwnerSettlementReadContext()
        budgeted._dependency_watcher_fallback = True
        budgeted._clock = lambda: now[0]
        budgeted.limit_owner_scan(5.0)
        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", side_effect=prove), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            obligations._resume_owner_settlements(self.jobs, context=unbudgeted)
            self.assertEqual(len(proven), 5, "a context without a budget keeps one full pass")
            proven.clear()
            per_pass = []
            for _pass in range(5):
                before = len(proven)
                obligations._resume_owner_settlements(self.jobs, context=budgeted)
                per_pass.append((len(proven) - before,
                                 bool(budgeted.owner_scan_unfinished(self.jobs))))
            obligations._resume_owner_settlements(self.jobs, context=budgeted)
        self.assertEqual(per_pass, [(1, True), (1, True), (1, True), (1, True), (1, False)])
        self.assertEqual(proven, [f"att-cold-{index}" for index in range(5)],
                         "each pass resumes after the last proven owner, never from the start")
        self.assertEqual(budgeted.owner_scan_unfinished(), ())

    def test_settled_owner_cache_survives_unrelated_append_and_invalidates_dependencies(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        dependency = self.base / "terminal-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        registry_cache.clear_cache()
        context = obligations.OwnerSettlementReadContext()
        context._dependency_watcher_fallback = True
        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        side_effect=[COMPLETE, PENDING, PENDING]) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
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
                return PENDING if target_checks > 1 else COMPLETE
            return COMPLETE

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               side_effect=lambda _jobs, _status, metadata:
                                   (dependencies[metadata["attempt_id"]],)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        side_effect=owner_pending), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held",
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
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        side_effect=(COMPLETE, PENDING)) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
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
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        side_effect=(COMPLETE, PENDING)) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
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
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        side_effect=(COMPLETE, PENDING)) as pending, \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
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
        with split_patch("dispatch_contract.supervisor_lease_is_held", return_value=True), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", return_value=PENDING):
            signatures = obligations._resume_owner_settlements(self.jobs)

        self.assertIn((str(self.jobs), "owner-settlement", "att-owner", "done",
                       "runtime-v1", "completed-supervisor\ufffd"), signatures)
        self.assertFalse(any(item[:2] == (str(self.jobs), "owner-settlement-unavailable")
                             for item in signatures))

    def _replace_registry(self, text):
        # Most registry writers replace the whole file, which changes its inode.
        replacement = self.jobs.with_name("jobs.replacement")
        replacement.write_text(text, encoding="utf-8")
        os.replace(replacement, self.jobs)

    def _settlement_fixture(self, state, *, lease_held=False, dependencies=None):
        """Patch the observation seams of an owner settlement pass.

        ``state`` is one CompletionState or a function of the classified owner;
        ``dependencies`` and ``lease_held`` may likewise be functions.
        """
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        clock = [1000.0]
        context = obligations.OwnerSettlementReadContext()
        context._clock = lambda: clock[0]
        self.addCleanup(context.close)
        lease_kwargs = ({"side_effect": lease_held} if callable(lease_held)
                        else {"return_value": lease_held})
        lease = stack.enter_context(mock.patch(
            "dispatch_contract.supervisor_lease_is_held", **lease_kwargs))
        state_kwargs = ({"side_effect": state} if callable(state)
                        else {"return_value": state})
        classify = stack.enter_context(mock.patch(
            "dispatch_terminal_commit.owner_completion_state", **state_kwargs))
        settle = stack.enter_context(mock.patch(
            "dispatch_completion_join.materialize_after_terminal_close"))
        if dependencies is not None:
            dependency_kwargs = ({"side_effect": dependencies} if callable(dependencies)
                                 else {"return_value": dependencies})
            stack.enter_context(mock.patch.object(
                obligations, "_owner_settlement_dependency_paths", **dependency_kwargs))
        return context, clock, classify, settle, lease

    def test_one_registry_generation_serves_every_classification_in_a_pass(self):
        states = {"att-pending": PENDING,
                  "att-na": CompletionState("not-applicable"),
                  "att-complete": COMPLETE}
        self.jobs.write_text("".join(self._runtime_owner_row(attempt) + "\n"
                                     for attempt in states), encoding="utf-8")
        evidence = self.base / "complete-evidence.json"
        evidence.write_text("sealed", encoding="utf-8")
        registry_cache.clear_cache()
        registry_cache.reset_metrics()
        context = obligations.OwnerSettlementReadContext()
        self.addCleanup(context.close)
        observed, passes = [], []
        real_view = registry_cache.registry_view

        def pass_view(jobs, **kwargs):
            passes.append(real_view(jobs, **kwargs))
            return passes[-1]

        def classify(jobs, _status, metadata, **_kwargs):
            # Like the real classification: open a scope, read one snapshot.
            with registry_cache.registry_read_scope():
                snapshot = registry_cache.registry_snapshot(jobs)
                observed.append((metadata["attempt_id"], snapshot.view, snapshot.signature,
                                 snapshot.matches_jobs(jobs),
                                 registry_cache.metrics_snapshot()["content_reads"]))
            return states[metadata["attempt_id"]]

        def write_during_settlement(jobs, _attempt):
            with Path(jobs).open("a", encoding="utf-8") as stream:
                stream.write("t\tdone\t/repo\t/wt\tchild\tattempt_id=att-written\n")

        with mock.patch.object(registry_cache, "registry_view", side_effect=pass_view), \
             mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(evidence,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        side_effect=classify), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close",
                        side_effect=write_during_settlement) as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            obligations._resume_owner_settlements(self.jobs, context=context)

        self.assertEqual([item[0] for item in observed],
                         ["att-pending", "att-na", "att-complete", "att-pending"])
        for _attempt, view, _signature, matches, reads in observed[:3]:
            self.assertIs(view, passes[0])
            self.assertTrue(matches)
            self.assertEqual(reads, 1, "a classification must not read the registry again")
        # The settlement write is outside the pinned section: the classification
        # after it observes the current file.
        _attempt, _view, signature, matches, _reads = observed[3]
        self.assertNotEqual(signature, tuple(passes[0].signature))
        self.assertTrue(matches)
        settle.assert_called_once_with(self.jobs, "att-pending")

    def test_unchanged_registry_keeps_the_owner_cursor(self):
        rows = "".join(self._runtime_owner_row(attempt) + "\n"
                       for attempt in ("att-one", "att-two"))
        self.jobs.write_text(rows, encoding="utf-8")
        registry_cache.clear_cache()
        context = obligations.OwnerSettlementReadContext()
        self.addCleanup(context.close)
        view = registry_cache.registry_view(self.jobs, append_only=True)
        self.assertEqual(context.owner_candidate_floor(self.jobs, view), -1)
        context.remember_owner_cursor(self.jobs, view)

        cached = registry_cache.registry_view(self.jobs, append_only=True)
        self.assertIs(cached, view)
        self.assertEqual(context.owner_candidate_floor(self.jobs, cached),
                         view.last_complete_order)

        # The bytes, not the inode, are what the cursor proved: an identical
        # replacement keeps the floor, a changed one gives it up.
        self._replace_registry(rows)
        replaced = registry_cache.registry_view(self.jobs, append_only=True)
        self.assertIsNot(replaced, view)
        self.assertEqual(context.owner_candidate_floor(self.jobs, replaced),
                         view.last_complete_order)
        self._replace_registry(self._runtime_owner_row("att-one", status="running") + "\n"
                               + self._runtime_owner_row("att-two") + "\n")
        changed = registry_cache.registry_view(self.jobs, append_only=True)
        self.assertEqual(context.owner_candidate_floor(self.jobs, changed), -1)

        context.remember_owner_cursor(self.jobs, changed)
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write(self._runtime_owner_row("att-three") + "\n")
        appended = registry_cache.registry_view(self.jobs, append_only=True)
        self.assertEqual(context.owner_candidate_floor(self.jobs, appended),
                         changed.last_complete_order)

    def test_inode_reused_by_a_replacement_does_not_resume_an_older_cursor(self):
        # gen1 (inode A) holds the cursor; gen2 (inode B) finishes an old owner
        # row and another reader sees it; gen3 is gen2 plus one row, written by
        # rename, and ext4 commonly hands it inode A again. gen3 keeps every
        # cached gen2 segment, yet it is a replacement, not an append of gen1:
        # a floor at gen1's cursor would skip the finished owner's settlement.
        # Without inode reuse the floor holds anyway (see OwnerCandidateFloorProofTest
        # for the same history with the inode brought back on any filesystem).
        def rename_registry(lines):
            fd, staged = tempfile.mkstemp(prefix=".jobs.log.claim-", dir=str(self.base))
            with os.fdopen(fd, "wb") as stream:
                stream.write("".join(line + "\n" for line in lines).encode())
            os.replace(staged, self.jobs)

        for trial in range(5):
            with self.subTest(trial=trial):
                registry_cache.clear_cache()
                context = obligations.OwnerSettlementReadContext()
                self.addCleanup(context.close)
                lines = [self._runtime_owner_row(f"att-{number}") for number in range(20)]
                lines[5] = self._runtime_owner_row("att-5", status="running")
                rename_registry(lines)
                first = registry_cache.registry_view(self.jobs, append_only=True)
                context.remember_owner_cursor(self.jobs, first)
                lines[5] = self._runtime_owner_row("att-5")
                rename_registry(lines)
                registry_cache.registry_view(self.jobs)
                lines.append(self._runtime_owner_row("att-new"))
                rename_registry(lines)
                third = registry_cache.registry_view(self.jobs, append_only=True)
                same_inode = tuple(third.signature[:2]) == tuple(first.signature[:2])
                self.assertEqual(context.owner_candidate_floor(self.jobs, third), -1,
                                 f"inode reused: {same_inode}")

    def test_not_applicable_owner_is_reclassified_only_when_its_rows_change(self):
        owner = self._runtime_owner_row("att-na")
        unrelated = "t\tdone\t/repo\t/wt\tchild\tattempt_id=att-unrelated\n"
        self.jobs.write_text(owner + "\n" + unrelated, encoding="utf-8")
        context, clock, state, _settle, _lease = self._settlement_fixture(
            CompletionState("not-applicable"))

        def run():
            return obligations._resume_owner_settlements(self.jobs, context=context)

        run()
        self.assertEqual(state.call_count, 1)

        # A rewrite that changes only an unrelated row leaves the owner alone.
        self._replace_registry(owner + "\n" + unrelated.replace("att-unrelated", "att-other"))
        run()
        self.assertEqual(state.call_count, 1)
        self.assertEqual(context.metrics()["deferred_owner_checks"], 1)

        # The owner's own row changed.
        self._replace_registry(
            owner.replace("completed-supervisor", "completed-changed") + "\n" + unrelated)
        run()
        self.assertEqual(state.call_count, 2)
        run()
        self.assertEqual(state.call_count, 2)

        # No input changed: only its bounded retry time observes it again.
        clock[0] += 601.0
        run()
        self.assertEqual(state.call_count, 3)

    def test_unchanged_pending_owner_defers_settlement_until_inputs_change(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        evidence = self.base / "terminal.json"
        evidence.write_text("sealed", encoding="utf-8")
        context, clock, state, settle, _lease = self._settlement_fixture(
            PENDING, dependencies=(evidence,))

        def run():
            return obligations._resume_owner_settlements(self.jobs, context=context)

        first = run()
        self.assertEqual((state.call_count, settle.call_count), (2, 1))
        second = run()
        self.assertEqual((state.call_count, settle.call_count), (2, 1))
        self.assertTrue(first, "the unfinished settlement stays in the outer signature")
        self.assertEqual(first, second)
        self.assertEqual(context.metrics()["deferred_owner_checks"], 1)

        evidence.write_text("changed", encoding="utf-8")
        run()
        self.assertEqual((state.call_count, settle.call_count), (4, 2))
        run()
        self.assertEqual((state.call_count, settle.call_count), (4, 2))

        clock[0] += 601.0
        run()
        self.assertEqual((state.call_count, settle.call_count), (6, 3))
        self.assertEqual(context.metrics()["unsettled_retries"], 2)

    def test_unknown_owner_retries_on_the_bounded_owner_backoff(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        context, clock, _state, settle, _lease = self._settlement_fixture(
            CompletionState("unknown", "OSError"))

        def run():
            return obligations._resume_owner_settlements(self.jobs, context=context)

        run()
        self.assertEqual(settle.call_count, 1)
        for interval in (60, 120, 240, 480, 600, 600):
            attempts = settle.call_count
            clock[0] += interval - 1
            run()
            self.assertEqual(settle.call_count, attempts,
                             f"no attempt one second before {interval}s")
            clock[0] += 1
            run()
            self.assertEqual(settle.call_count, attempts + 1, f"an attempt at {interval}s")

    def test_unknown_backoff_grows_when_each_attempt_rewrites_its_dependency(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        evidence = self.base / "attempts.json"
        evidence.write_text("x", encoding="utf-8")
        context, clock, _state, settle, _lease = self._settlement_fixture(
            CompletionState("unknown", "OSError"), dependencies=(evidence,))
        settle.side_effect = lambda *_args: evidence.write_text(
            "x" * (settle.call_count + 1), encoding="utf-8")

        def run():
            return obligations._resume_owner_settlements(self.jobs, context=context)

        run()
        for interval in (60, 120, 240):
            attempts = settle.call_count
            clock[0] += interval - 1
            run()
            self.assertEqual(settle.call_count, attempts)
            clock[0] += 1
            run()
            self.assertEqual(settle.call_count, attempts + 1)

    def test_deferred_owner_keeps_its_signature_and_sees_new_claims_leases_and_parent_close(self):
        deps = self.base / "deps"
        deps.mkdir()
        evidence = deps / "terminal.json"
        evidence.write_text("sealed", encoding="utf-8")
        claim = deps / "terminal-claim.json"  # absent: depends on its directory
        journal = self.base / "workflow" / "rt-fixture" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text('{"seq": 1}\n', encoding="utf-8")
        owner_row = self._runtime_owner_row("att-owner").replace(
            "attempt_id=att-owner", "attempt_id=att-owner,owner_route_id=rt-fixture")
        child_row = ("t\topen\t/repo\t/wt\tchild\tattempt_schema_version=2,"
                     "attempt_id=att-child,parent_attempt_id=att-owner\n")
        self.jobs.write_text(owner_row + "\n" + child_row, encoding="utf-8")
        states = {"att-owner": PENDING, "att-second": PENDING}
        held = {}
        context, _clock, state, settle, _lease = self._settlement_fixture(
            lambda _jobs, _status, metadata, **_kwargs: states[metadata["attempt_id"]],
            lease_held=lambda _jobs, metadata: held.get(metadata["attempt_id"], False),
            dependencies=lambda _jobs, _status, _metadata:
                obligations._observed_dependencies((evidence, claim)))
        service_script = self.base / "release" / "utilities" / "codex-managed-completion.py"

        def observe():
            snapshot = {"signature": ()}
            obligations.ensure_observers(
                self.jobs, observer_snapshot=snapshot, settlement_context=context)
            return snapshot["signature"]

        def classified(attempt):
            return sum(1 for call in state.call_args_list if call.args[2]["attempt_id"] == attempt)

        def settled(attempt):
            return sum(1 for call in settle.call_args_list if call.args[1] == attempt)

        def owner_signature(signature, attempt):
            return [item for item in signature
                    if item[1] == "owner-settlement" and item[2] == attempt]

        with mock.patch("peer_obligations.service_script",
                        return_value=(service_script, {}, None)), \
             mock.patch("workflow_state.ledger_root_for",
                        return_value=(self.base / "workflow", "explicit-jobs")):
            first = observe()
            self.assertTrue(owner_signature(first, "att-owner"))
            self.assertEqual(observe(), first, "a deferred owner keeps its outer signature")

            # (a) A new owner row is classified on the next pass.
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write(self._runtime_owner_row("att-second") + "\n")
            self.assertTrue(owner_signature(observe(), "att-second"))
            self.assertGreater(classified("att-second"), 0)

            # (b) A lease that becomes held stops the attempt; its release resumes it.
            before, attempts = classified("att-owner"), settled("att-owner")
            held["att-owner"] = True
            observe()
            self.assertGreater(classified("att-owner"), before)
            self.assertEqual(settled("att-owner"), attempts)
            before = classified("att-owner")
            held["att-owner"] = False
            observe()
            self.assertGreater(classified("att-owner"), before)
            self.assertEqual(settled("att-owner"), attempts + 1)

            # (c) A terminal claim appearing among the dependencies.
            before = classified("att-owner")
            claim.write_text("claimed", encoding="utf-8")
            observe()
            self.assertGreater(classified("att-owner"), before)

            # (e) A related open child row replaced with a parent close request.
            before = classified("att-owner")
            self._replace_registry(self.jobs.read_text(encoding="utf-8").replace(
                "parent_attempt_id=att-owner",
                "parent_attempt_id=att-owner,parent_close_requested=1"))
            observe()
            self.assertGreater(classified("att-owner"), before)

            # (d) The parent-close journal grows; the owner is no longer applicable.
            before = classified("att-owner")
            states["att-owner"] = CompletionState("not-applicable")
            with journal.open("a", encoding="utf-8") as stream:
                stream.write('{"seq": 2}\n')
            signature = observe()
            self.assertGreater(classified("att-owner"), before)
            self.assertFalse(owner_signature(signature, "att-owner"))
            self.assertTrue(owner_signature(signature, "att-second"))

    def test_settlement_attempt_writes_do_not_retrigger_the_same_owner(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        evidence = self.base / "attempts.json"
        evidence.write_text("x", encoding="utf-8")
        context, clock, _state, settle, _lease = self._settlement_fixture(
            PENDING, dependencies=(evidence,))
        # Like a terminal-commit attempt record, every attempt rewrites a dependency.
        settle.side_effect = lambda *_args: evidence.write_text(
            "x" * (settle.call_count + 1), encoding="utf-8")

        def run():
            return obligations._resume_owner_settlements(self.jobs, context=context)

        run()
        self.assertEqual(settle.call_count, 1)
        run()
        run()
        self.assertEqual(settle.call_count, 1)
        clock[0] += 601.0
        run()
        self.assertEqual(settle.call_count, 2)
        run()
        self.assertEqual(settle.call_count, 2)

    def test_registered_redrive_shares_retry_memory_with_external_settlement(self):
        import dispatch_terminal_commit as terminal

        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        slot = self.base / "slot"
        slot.mkdir()
        state_file = slot / "terminal-commit.json"
        state_file.write_text('{"state":"owner-envelope-sealed"}\n', encoding="utf-8")
        observed_dependencies = getattr(obligations, "_observed_dependencies", None)
        dependencies = (observed_dependencies(
            (state_file, slot / "campaign-goal.json"))
            if observed_dependencies is not None else
            (state_file, slot))
        writes = []

        def exclusive_replay():
            writes.append(True)
            try:
                terminal._atomic_json(state_file, {"state": "claimed"}, exclusive=True)
            except FileExistsError:
                pass
            return state_file

        context_type = getattr(obligations, "OwnerSettlementReadContext", None)
        if context_type is None:
            with mock.patch("dispatch_terminal_commit.owner_completion_state",
                            return_value=PENDING), \
                 mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False), \
                 mock.patch("dispatch_completion_join.materialize_after_terminal_close") as external_settle:
                exclusive_replay()
                obligations._resume_owner_settlements(self.jobs)
            self.assertEqual(len(writes) + external_settle.call_count, 1,
                             "R4_NO_REPLAY_BEFORE_DUE")
            return

        context, clock, state, external_settle, _lease = self._settlement_fixture(
            PENDING, dependencies=dependencies)
        related = ()
        record = registry_cache.registry_view(self.jobs).settlement_owner_candidates()[0]
        metadata, raw = record.completion_metadata(), record.raw

        with mock.patch.object(obligations, "_owner_unsettled_dependency_paths",
                               create=True,
                               return_value=dependencies), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            if not hasattr(context, "registered_owner_redrive"):
                # Portable baseline path: model the existing registered
                # materializer, then run its independent external consumer.
                exclusive_replay()
                obligations._resume_owner_settlements(self.jobs, context=context)
                self.assertEqual(len(writes) + external_settle.call_count, 1,
                                 "R4_NO_REPLAY_BEFORE_DUE")
                return
            deferred = context.registered_owner_redrive(
                self.jobs, "done", raw, metadata, related, lease_held=False,
                materialize=exclusive_replay)
            self.assertFalse(deferred)
            self.assertEqual(writes, [True])
            # The external consumer sees the post-attempt generations and keeps
            # the same owner visible without replaying the materializer.
            signatures = obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertTrue(signatures)
            self.assertEqual(external_settle.call_count, 0)
            self.assertEqual(state.call_count, 1)
            self.assertEqual(writes, [True])
            self.assertTrue(context.registered_owner_redrive(
                self.jobs, "done", raw, metadata, related, lease_held=False,
                materialize=exclusive_replay), "R4_NO_REPLAY_BEFORE_DUE")
            self.assertEqual(writes, [True])
            clock[0] += 599.9
            self.assertTrue(context.registered_owner_redrive(
                self.jobs, "done", raw, metadata, related, lease_held=False,
                materialize=exclusive_replay), "R4_NO_REPLAY_BEFORE_DUE")
            self.assertEqual(writes, [True])
            clock[0] += 0.1
            self.assertFalse(context.registered_owner_redrive(
                self.jobs, "done", raw, metadata, related, lease_held=False,
                materialize=exclusive_replay))
            self.assertEqual(writes, [True, True])

    def test_registered_redrive_observes_same_pass_dependency_change(self):
        rows = {attempt: self._runtime_owner_row(attempt) for attempt in ("att-a", "att-b")}
        self.jobs.write_text("\n".join(rows.values()) + "\n", encoding="utf-8")
        view = registry_cache.registry_view(self.jobs)
        records = {record.completion_metadata()["attempt_id"]: record
                   for record in view.settlement_owner_candidates()}
        dependencies = {}
        for attempt in rows:
            path = self.base / attempt
            path.write_text("before", encoding="utf-8")
            dependencies[attempt] = (path,)
        context = obligations.OwnerSettlementReadContext()
        context._clock = lambda: 1000.0
        self.addCleanup(context.close)
        attempts = []

        def replay(attempt):
            attempts.append(attempt)

        def run(attempt):
            record = records[attempt]
            return context.registered_owner_redrive(
                self.jobs, record.status, record.raw, record.completion_metadata(), (),
                lease_held=False, materialize=lambda: replay(attempt))

        with mock.patch.object(obligations, "_owner_unsettled_dependency_paths",
                               side_effect=lambda _jobs, _status, metadata:
                                   dependencies[metadata["attempt_id"]]), \
             mock.patch("dispatch_terminal_commit.owner_completion_state",
                        return_value=PENDING):
            self.assertFalse(run("att-a"))
            self.assertFalse(run("att-b"))
            obligations._resume_owner_settlements(self.jobs, context=context)
            dependencies["att-a"][0].write_text("changed", encoding="utf-8")
            self.assertFalse(run("att-a"))
            self.assertTrue(run("att-b"))
            obligations._resume_owner_settlements(self.jobs, context=context)

        self.assertEqual(attempts, ["att-a", "att-b", "att-a"])

    def test_materialization_exception_stays_unknown_until_retry_time(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        dependency = self.base / "terminal-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        context, clock, _state, _settle, _lease = self._settlement_fixture(
            PENDING, dependencies=(dependency,))
        record = registry_cache.registry_view(self.jobs).settlement_owner_candidates()[0]
        metadata, raw = record.completion_metadata(), record.raw
        retries = []

        def fail_once():
            raise OSError("temporary materialization failure")

        with mock.patch.object(obligations, "_owner_unsettled_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            with self.assertRaisesRegex(OSError, "temporary materialization failure"):
                context.registered_owner_redrive(
                    self.jobs, "done", raw, metadata, (), lease_held=False,
                    materialize=fail_once)
            self.assertTrue(context.registered_owner_redrive(
                self.jobs, "done", raw, metadata, (), lease_held=False,
                materialize=lambda: retries.append("too-early")))
            self.assertEqual(retries, [])
            clock[0] += 60.0
            self.assertFalse(context.registered_owner_redrive(
                self.jobs, "done", raw, metadata, (), lease_held=False,
                materialize=lambda: retries.append("due")))

        self.assertEqual(retries, ["due"])

    def test_shared_owner_retry_observes_changed_inputs(self):
        import dispatch_terminal_commit as terminal

        if not hasattr(obligations, "_owner_unsettled_dependency_paths"):
            self.skipTest("baseline has no unsettled dependency calculation")

        cases = ("owner-row", "related-child", "lease-held-released",
                 "terminal-claim", "campaign-goal", "parent-close-journal")
        for case in cases:
            for phase in ("same-pass", "between-passes"):
                with self.subTest(input=case, phase=phase):
                    self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
                    context = obligations.OwnerSettlementReadContext()
                    context._clock = lambda: 1000.0
                    self.addCleanup(context.close)
                    row = registry_cache.registry_view(
                        self.jobs).settlement_owner_candidates()[0]
                    metadata, raw = row.completion_metadata(), row.raw
                    related = (type("Related", (), {"order": 1, "status": "open",
                                                      "raw": "child-before"})(),)
                    dep_dir = self.base / ("dependency-" + case + "-" + phase)
                    dep_dir.mkdir()
                    if case == "parent-close-journal":
                        dep = dep_dir / "journal.jsonl"
                        dep.write_text('{"seq":1}\n', encoding="utf-8")
                        paths = (dep,)
                    elif case in {"terminal-claim", "campaign-goal"}:
                        dep = dep_dir / ("terminal-claim.json" if case == "terminal-claim"
                                         else "campaign-goal.json")
                        paths = (dep_dir,)
                    else:
                        dep = dep_dir / "owner-state.json"
                        dep.write_text("before", encoding="utf-8")
                        paths = (dep,)
                    materializations = []

                    def attempt(current_raw=raw, current_related=related):
                        return context.registered_owner_redrive(
                            self.jobs, "done", current_raw, metadata, current_related,
                            lease_held=False,
                            materialize=lambda: materializations.append(case))

                    with mock.patch.object(obligations, "_owner_unsettled_dependency_paths",
                                           return_value=paths), \
                         mock.patch.object(terminal, "owner_completion_state", return_value=PENDING):
                        self.assertFalse(attempt())
                        self.assertTrue(attempt(), "same pass must defer the second consumer")
                        self.assertEqual(len(materializations), 1)
                        if phase == "between-passes":
                            context._end_unsettled_pass(self.jobs)
                        next_raw, next_related = raw, related
                        if case == "owner-row":
                            next_raw = raw + ",updated=1"
                        elif case == "related-child":
                            next_related = (type("Related", (), {"order": 1, "status": "open",
                                                                 "raw": "child-after"})(),)
                        elif case == "lease-held-released":
                            self.assertIsNone(context.unsettled_current(
                                self.jobs, "done", raw, metadata, related,
                                lambda _jobs, _meta: True))
                        elif case in {"terminal-claim", "campaign-goal"}:
                            dep.write_text("created", encoding="utf-8")
                            stamp = dep_dir.stat()
                            os.utime(dep_dir, ns=(stamp.st_atime_ns,
                                                  stamp.st_mtime_ns + 1_000_000))
                        elif case == "parent-close-journal":
                            with dep.open("a", encoding="utf-8") as stream:
                                stream.write('{"seq":2}\n')
                            stamp = dep.stat()
                            os.utime(dep, ns=(stamp.st_atime_ns,
                                              stamp.st_mtime_ns + 1_000_000))
                        else:
                            dep.write_text("after", encoding="utf-8")
                            stamp = dep.stat()
                            os.utime(dep, ns=(stamp.st_atime_ns,
                                              stamp.st_mtime_ns + 1_000_000))
                        self.assertFalse(attempt(next_raw, next_related),
                                         "changed inputs must release the next consumer")
                        self.assertEqual(len(materializations), 2)
                        self.assertTrue(attempt(next_raw, next_related),
                                        "the unchanged owner remains deferred after retry")
        import dispatch_contract
        import workflow_state

        route_id = "rt-dependency-fixture"
        attempt_id = "att-dependency-fixture"
        route_file = self.base / "route.json"
        route_file.write_text(json.dumps({
            "route_id": route_id,
            "nodes": [{"id": "finish", "terminal": True}],
        }), encoding="utf-8")
        request = terminal.TerminalCommitRequest(
            route_file, attempt_id, self.jobs, self.base / "artifacts")
        slot = terminal.terminal_slot(request.artifact_root, route_id, attempt_id)
        slot.mkdir(parents=True)
        state_file = terminal._commit_state_path(request)
        state_file.write_text('{"state":"pending"}\n', encoding="utf-8")
        claim = dispatch_contract.terminal_claim_path(self.jobs, route_id, attempt_id)
        claim.parent.mkdir(parents=True)
        claim.write_text('{"state":"claimed"}\n', encoding="utf-8")
        ledger_root, _source = workflow_state.ledger_root_for(self.jobs)
        journal = ledger_root / route_id / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text('{"seq":1}\n', encoding="utf-8")
        metadata = {"attempt_id": attempt_id, "owner_route_id": route_id,
                    "parent_close_route_id": route_id}

        with mock.patch.object(terminal, "_historical_request", return_value=request):
            paths = obligations._owner_unsettled_dependency_paths(
                self.jobs, "done", metadata)

        self.assertIn(claim, paths, "terminal claim must be observed")
        self.assertIn(journal, paths, "parent-close journal must be observed")
        self.assertIn(slot, paths, "missing goal must observe its nearest existing parent")
        self.assertNotIn(terminal.campaign_goal_path(request), paths)

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


class RestoredOwnerProofTest(unittest.TestCase):
    """A proof restored after a runner restart is reselected when its basis changes."""

    setUp = BatchObligationTest.setUp
    _runtime_owner_row = BatchObligationTest._runtime_owner_row

    def _passes_after_restart(self, *, watched):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        dependency = self.base / "terminal.json"
        dependency.write_text("sealed", encoding="utf-8")
        proofs = self.base / "runner-duties" / "settled-owners.json"
        before = obligations.OwnerSettlementReadContext()
        restarted = obligations.OwnerSettlementReadContext()
        for context in (before, restarted):
            context._dependency_watcher_fallback = not watched
            self.addCleanup(context.close)
        state = lambda *_args: COMPLETE if dependency.read_text() == "sealed" else PENDING
        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", side_effect=state), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            obligations._resume_owner_settlements(self.jobs, context=before)
            self.assertTrue(before.save_settled_proofs(proofs))
            self.assertEqual(restarted.load_settled_proofs(proofs), 1)
            # The normal first pass after the restart: the registry is unchanged.
            obligations._resume_owner_settlements(self.jobs, context=restarted)
            self.assertEqual(settle.call_count, 0)
            dependency.write_text("changed", encoding="utf-8")
            outputs = [obligations._resume_owner_settlements(self.jobs, context=restarted)
                       for _ in range(3)]
            return settle.call_count, outputs

    def test_a_dependency_written_during_the_settlement_read_is_not_its_basis(self):
        self.jobs.write_text(self._runtime_owner_row() + "\n", encoding="utf-8")
        dependency = self.base / "terminal.json"
        dependency.write_text("sealed", encoding="utf-8")
        context = obligations.OwnerSettlementReadContext()
        context._dependency_watcher_fallback = True
        self.addCleanup(context.close)
        reads = []

        def state(*_args):
            reads.append(dependency.read_text())
            if len(reads) == 1:
                # Rewritten after this read observed it, before the proof is stored.
                os.utime(dependency, ns=(1, 1))
            return COMPLETE

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               return_value=(dependency,)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", side_effect=state), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close"), \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(context._settled, {}, "a proof must not adopt a mid-read change")
            first_pass_reads = len(reads)
            obligations._resume_owner_settlements(self.jobs, context=context)
        self.assertGreater(len(reads), first_pass_reads, "the next pass observes the owner again")
        self.assertEqual(len(context._settled), 1)

    def _assert_reselected(self, watched):
        settled, outputs = self._passes_after_restart(watched=watched)
        self.assertGreater(settled, 0, "the restored proof's change must reach a pass")
        self.assertTrue(any(outputs), outputs)

    def test_dependency_change_after_restart_reselects_the_owner_by_stat_rotation(self):
        self._assert_reselected(watched=False)

    def test_dependency_change_after_restart_reselects_the_owner_by_watch(self):
        self._assert_reselected(watched=True)

    def _earlier_proof_invalidated_during_budgeted_resume(self, *, restored):
        """An earlier owner whose proof became invalid is not hidden by the resume order.

        2026-11-12 review [52]: owner 0 proven, the 5 s budget resumed after
        owner 1, then owner 0's dependency turned pending; `order <= resume`
        skipped it on every later pass (re-observed 0, settled 0).
        """
        attempts = [f"att-cold-{index}" for index in range(5)]
        self.jobs.write_text("\n".join(self._runtime_owner_row(attempt) for attempt in attempts)
                             + "\n", encoding="utf-8")
        dependencies = {}
        for attempt in attempts:
            dependencies[attempt] = self.base / f"{attempt}.json"
            dependencies[attempt].write_text("sealed", encoding="utf-8")
        proofs = self.base / "runner-duties" / "settled-owners.json"
        now = [0.0]
        observed = []

        def state(_jobs, _status, metadata):
            observed.append(metadata["attempt_id"])
            now[0] += 10.0  # each proof costs more than the pass budget
            sealed = dependencies[metadata["attempt_id"]].read_text() == "sealed"
            return COMPLETE if sealed else PENDING

        def budgeted():
            context = obligations.OwnerSettlementReadContext()
            context._dependency_watcher_fallback = True
            context._clock = lambda: now[0]
            context.limit_owner_scan(5.0)
            self.addCleanup(context.close)
            return context

        with mock.patch.object(obligations, "_owner_settlement_dependency_paths",
                               side_effect=lambda _j, _s, meta: (dependencies[meta["attempt_id"]],)), \
             mock.patch("dispatch_terminal_commit.owner_completion_state", side_effect=state), \
             mock.patch("dispatch_completion_join.materialize_after_terminal_close") as settle, \
             split_patch("dispatch_contract.supervisor_lease_is_held", return_value=False):
            context = budgeted()
            obligations._resume_owner_settlements(self.jobs, context=context)
            if restored:
                self.assertTrue(context.save_settled_proofs(proofs))
                context = budgeted()
                self.assertEqual(context.load_settled_proofs(proofs), 1)
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(observed, attempts[:2])
            self.assertTrue(context.owner_scan_unfinished(self.jobs), "resume after owner 1")
            dependencies[attempts[0]].write_text("pending", encoding="utf-8")
            observed.clear()
            outputs = [obligations._resume_owner_settlements(self.jobs, context=context)
                       for _ in range(8)]
        settled_zero = [call for call in settle.call_args_list if call.args[1] == attempts[0]]
        # Each 5 s pass proves one owner: the never-proved owners 2-4 keep
        # their turn first ([e7]), then owner 0 is observed again.
        self.assertEqual(observed[:4], [*attempts[2:], attempts[0]],
                         "owner 0 is re-observed right after the unproven suffix")
        self.assertTrue(settled_zero, "owner 0 reaches its settlement path")
        self.assertTrue(any(signature[2] == attempts[0] for signature in outputs[3]), outputs[3])
        for attempt in attempts[2:]:
            self.assertEqual(observed.count(attempt), 1,
                             f"{attempt} is still proved exactly once by the resumed scan")
        self.assertEqual(observed.count(attempts[1]), 0, "owner 1 keeps its valid proof")

    def test_invalidated_restored_proof_is_reobserved_despite_the_resume_order(self):
        self._earlier_proof_invalidated_during_budgeted_resume(restored=True)

    def test_invalidated_fresh_proof_is_reobserved_despite_the_resume_order(self):
        self._earlier_proof_invalidated_during_budgeted_resume(restored=False)



class OwnerProofBudgetFairnessTest(unittest.TestCase):
    """Budgeted owner-proof resumption under any order of proof invalidations.

    2026-11-12 review [e7]: owner 0's dependency moved to a new complete
    generation before every pass; each 5 s pass re-proved owner 0 alone, the
    resume order stayed at owner 1, and owners 2-4 were never observed in
    eight passes (fresh and restored proofs alike). Instead of one more
    counterexample this asserts the four properties of the resumption over
    seeded random invalidations, plus the [e7] six and [52] four probe cells:

    1. progress: while an owner was never proved, every pass proves one;
    2. re-observation: an owner whose proof became invalid is proved again
       within a bounded number of passes, several such owners in turn;
    3. once: no valid proof is re-proved, no owner is proved twice in a
       pass, every due owner is proved and the totals match the invalidations;
    4. budget: a pass proves exactly what its soft budget admits, and a
       caller without a budget proves every due owner in one pass.
    """

    setUp = BatchObligationTest.setUp
    _runtime_owner_row = BatchObligationTest._runtime_owner_row

    def _owner_proof_run(self, count, *, cost=10.0, budget=5.0):
        """A private registry of `count` finished owners with one dependency each."""
        attempts = [f"att-fair-{index}" for index in range(count)]
        self.jobs.write_text("\n".join(self._runtime_owner_row(attempt) for attempt in attempts)
                             + "\n", encoding="utf-8")
        registry_cache.clear_cache()
        dependencies = {attempt: self.base / f"{attempt}.json" for attempt in attempts}
        revision = [0]
        for path in dependencies.values():
            path.write_text("complete:initial", encoding="utf-8")
        run = Namespace(attempts=attempts, now=[0.0], proofs=[], classifying=[False],
                        settled=[], contexts=[], cost=cost, budget=budget)

        def change(attempt, text=None):
            revision[0] += 1
            path = dependencies[attempt]
            path.write_text(text or f"complete:revision-{revision[0]}", encoding="utf-8")
            stamp = 10 ** 9 * (revision[0] + 1000)
            os.utime(path, ns=(stamp, stamp))  # a new generation regardless of fs resolution

        def state(_jobs, _status, metadata):
            if run.classifying[0]:
                run.proofs.append(metadata["attempt_id"])
                run.now[0] += cost
            text = dependencies[metadata["attempt_id"]].read_text()
            return COMPLETE if text.startswith("complete") else PENDING

        classify = obligations._classify_owner_candidates

        def observed_classify(*args, **kwargs):
            run.classifying[0] = True
            try:
                return classify(*args, **kwargs)
            finally:
                run.classifying[0] = False

        def context(limit=budget):
            created = obligations.OwnerSettlementReadContext()
            created._dependency_watcher_fallback = True
            created._clock = lambda: run.now[0]
            created.limit_owner_scan(limit)
            run.contexts.append(created)
            self.addCleanup(created.close)
            return created

        def one_pass(current):
            before = len(run.proofs)
            output = obligations._resume_owner_settlements(self.jobs, context=current)
            return run.proofs[before:], output

        run.change, run.context, run.one_pass = change, context, one_pass
        run.patches = contextlib.ExitStack()
        run.patches.enter_context(mock.patch.object(
            obligations, "_owner_settlement_dependency_paths",
            side_effect=lambda _j, _s, meta: (dependencies[meta["attempt_id"]],)))
        run.patches.enter_context(mock.patch.object(
            obligations, "_classify_owner_candidates", side_effect=observed_classify))
        run.patches.enter_context(mock.patch(
            "dispatch_terminal_commit.owner_completion_state", side_effect=state))
        run.patches.enter_context(mock.patch(
            "dispatch_completion_join.materialize_after_terminal_close",
            side_effect=lambda _jobs, attempt, *a, **k: run.settled.append(attempt)))
        run.patches.enter_context(split_patch(
            "dispatch_contract.supervisor_lease_is_held", return_value=False))
        self.addCleanup(run.patches.close)
        return run

    def _checked_pass(self, run, current, model, since, pass_index, label, *, budgeted=True):
        """One pass checked against properties 1, 3 and 4; returns its proofs."""
        due = [attempt for attempt in run.attempts if model[attempt] != "valid"]
        never = {attempt for attempt in run.attempts if model[attempt] == "never"}
        proved, _output = run.one_pass(current)
        where = f"{label} pass {pass_index}: proved {proved}, due {due}"
        self.assertEqual(len(proved), len(set(proved)), f"once per pass — {where}")
        self.assertFalse([a for a in proved if model[a] == "valid"],
                         f"a valid proof is not proved again — {where}")
        if budgeted:
            admitted = math.ceil(run.budget / run.cost)
            self.assertEqual(len(proved), min(len(due), admitted),
                             f"the soft budget admits exactly this many proofs — {where}")
            if never:
                self.assertTrue(never & set(proved), f"progress: a never-proved owner — {where}")
        else:
            self.assertEqual(sorted(proved), sorted(due), f"no budget: one full pass — {where}")
        for attempt in proved:
            model[attempt] = "valid"
            since.pop(attempt, None)
        return proved

    def _invalidate(self, run, model, since, attempt, pass_index, counts):
        run.change(attempt)
        if model[attempt] == "valid":
            model[attempt] = "invalid"
            since[attempt] = pass_index
            counts[attempt] += 1

    def _seeded_case(self, seed):
        rng = random.Random(seed)
        count = rng.randint(5, 8)
        passes = rng.randint(20, 40)
        cost = rng.choice((10.0, 10.0, 2.0))  # one proof per 5 s pass, or a few
        hot = rng.sample(range(count), rng.choice((1, 1, 2, 3)))  # invalidated every pass
        rate = rng.choice((0.0, 0.3, 0.6))
        restore_at = rng.choice((None, rng.randint(1, passes - 2)))
        label = (f"seed {seed} owners={count} passes={passes} cost={cost} hot={hot} "
                 f"rate={rate} restore={restore_at}")
        run = self._owner_proof_run(count, cost=cost)
        attempts = run.attempts
        model = {attempt: "never" for attempt in attempts}
        since, counts = {}, {attempt: 0 for attempt in attempts}
        proofs_of = {attempt: 0 for attempt in attempts}
        bound = 2 * count + 1
        proofs_file = self.base / f"proofs-{seed}.json"
        current = run.context()
        for pass_index in range(passes):
            if pass_index:
                for index in hot:
                    self._invalidate(run, model, since, attempts[index], pass_index, counts)
                if rng.random() < rate:
                    self._invalidate(run, model, since, rng.choice(attempts), pass_index, counts)
            for attempt in self._checked_pass(run, current, model, since, pass_index, label):
                proofs_of[attempt] += 1
            late = {a: pass_index - s for a, s in since.items() if pass_index - s >= bound}
            self.assertFalse(late, f"re-observation within {bound} passes — {label}: {late}")
            if pass_index == restore_at:
                self.assertTrue(current.save_settled_proofs(proofs_file))
                current = run.context()
                current.load_settled_proofs(proofs_file)
                # Only valid proofs were saved: to the restarted runner an
                # invalidated owner is one it has not proved yet.
                for attempt, state in model.items():
                    if state == "invalid":
                        model[attempt] = "never"
        for index in range(len(attempts)):  # the invalidations stop: everything settles
            if all(state == "valid" for state in model.values()):
                break
            for attempt in self._checked_pass(run, current, model, since, passes + index, label):
                proofs_of[attempt] += 1
        self.assertEqual(set(model.values()), {"valid"}, f"no owner is skipped — {label}")
        self.assertEqual(proofs_of, {a: 1 + counts[a] for a in attempts},
                         f"one proof per invalidation — {label}")
        # A caller without a budget still finishes in one pass.
        for attempt in rng.sample(attempts, rng.randint(1, count)):
            self._invalidate(run, model, since, attempt, passes, counts)
        current.limit_owner_scan(None)
        self._checked_pass(run, current, model, since, passes + count, label, budgeted=False)
        self.assertEqual(run.one_pass(current)[0], [], f"nothing left — {label}")

    def _probe_cell(self, *, restored, budget, churn, pending=False):
        """[e7] six cells (churn) and [52] four cells (pending) on five owners."""
        label = f"restored={restored} budget={budget} churn={churn} pending={pending}"
        run = self._owner_proof_run(5)
        attempts = run.attempts
        model = {attempt: "never" for attempt in attempts}
        since, counts = {}, {attempt: 0 for attempt in attempts}
        current = run.context()
        self.assertEqual(run.one_pass(current)[0], attempts[:1])
        if restored:
            proofs_file = self.base / "proofs.json"
            self.assertTrue(current.save_settled_proofs(proofs_file))
            current = run.context()
            self.assertEqual(current.load_settled_proofs(proofs_file), 1)
        self.assertEqual(run.one_pass(current)[0], attempts[1:2])
        self.assertTrue(current.owner_scan_unfinished(self.jobs), "resume after owner 1")
        model.update({attempts[0]: "valid", attempts[1]: "valid"})
        if not budget:
            current.limit_owner_scan(None)
        if pending:
            run.change(attempts[0], "pending")
            seen = []
            for _ in range(8):
                seen.extend(run.one_pass(current)[0])
            self.assertIn(attempts[0], seen, f"owner 0 is re-observed — {label}")
            self.assertIn(attempts[0], run.settled, f"owner 0 reaches settlement — {label}")
            for attempt in attempts[2:]:
                self.assertEqual(seen.count(attempt), 1, f"{attempt} proved once — {label}")
            self.assertEqual(seen.count(attempts[1]), 0, f"owner 1 keeps its proof — {label}")
            return
        seen = []
        for pass_index in range(8):
            if churn:
                self._invalidate(run, model, since, attempts[0], pass_index, counts)
            seen.extend(self._checked_pass(run, current, model, since, pass_index, label,
                                           budgeted=budget))
        self.assertEqual([a for a in attempts[2:] if a not in seen], [],
                         f"tail owners are observed within eight passes — {label}")

    def test_budgeted_resumption_keeps_its_four_properties_under_random_invalidations(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                self._seeded_case(seed)

    def test_review_probe_cells_hold(self):
        cells = [dict(restored=r, budget=b, churn=c) for r, b, c in (
            (False, True, False), (True, True, False), (False, False, True),
            (True, False, True), (False, True, True), (True, True, True))]
        cells += [dict(restored=r, budget=b, churn=False, pending=True)
                  for r in (False, True) for b in (False, True)]
        for cell in cells:
            with self.subTest(**cell):
                self._probe_cell(**cell)



class OwnerCandidateFloorProofTest(unittest.TestCase):
    """The owner candidate floor skips only rows its cursor generation proved.

    The registry is replaced by rename, and ext4 hands a freed inode number out
    again at once (A -> B -> A); a rewrite in place keeps the number. A later
    append in place then made an older cursor's ``(dev, ino)`` look like the
    file it had read, and the floor skipped rows that changed in between: a
    finished owner's settlement was missed. Every judgment here is made against
    the registry bytes, through each read path, with and without the inode
    coming back (a held hard link brings it back on any filesystem).
    """

    READS = ("window", "gap", "plain-first", "scope", "cold")
    WARM_READS = ("window", "gap", "plain-first", "scope")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self._case("setup")
        registry_cache.clear_cache()
        self.addCleanup(registry_cache.clear_cache)

    def _case(self, name):
        registry_cache.clear_cache()
        self.base = self.root / name
        self.base.mkdir()
        self.jobs = self.base / "jobs.log"

    @staticmethod
    def _owner(attempt, status="done", extra=""):
        return ("2026-10-18T00:00:00Z\t" + status + "\t/repo\t/wt\towner\t"
                "attempt_schema_version=2,worker_type=owner,launch_started=1,"
                "supervisor_lease=flock-v1,attempt_id=" + attempt + extra).encode()

    @staticmethod
    def _data(lines, tail=b""):
        return b"".join(line + b"\n" for line in lines) + tail

    def _rename(self, data, onto=None):
        """Replace the registry by rename; ``onto`` brings back that path's inode."""
        if onto is None:
            fd, staged = tempfile.mkstemp(prefix=".jobs.log.claim-", dir=str(self.base))
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        else:
            staged = onto
            with open(staged, "r+b") as stream:
                stream.truncate(0)
                stream.write(data)
        os.replace(staged, self.jobs)
        if os.path.lexists(staged):
            # rename(2) leaves both names when they already link one inode.
            os.unlink(staged)

    def _hold_inode(self):
        """A hard link to the current registry inode for a later ``_rename(onto=...)``."""
        held = self.base / (".held-" + str(os.stat(self.jobs).st_ino))
        os.link(self.jobs, held)
        return held

    def _append(self, data):
        with self.jobs.open("ab") as stream:
            stream.write(data)

    def _rewrite_in_place(self, data):
        """Same inode, new bytes, and the old mtime restored."""
        info = os.stat(self.jobs)
        with self.jobs.open("r+b") as stream:
            stream.truncate(0)
            stream.write(data)
        os.utime(self.jobs, ns=(info.st_atime_ns, info.st_mtime_ns))

    def _read(self, how):
        if how == "cold":
            registry_cache.clear_cache()
        elif how == "scope":
            with registry_cache.registry_read_scope():
                return registry_cache.current_registry_view(self.jobs)
        elif how == "plain-first":
            registry_cache.registry_view(self.jobs)
        elif how == "gap":
            # A supervisor pass later than the retained-descriptor window.
            with mock.patch.object(registry_cache, "_APPEND_OBSERVATION_GAP_SECONDS", -1.0):
                return registry_cache.registry_view(self.jobs, append_only=True)
        return registry_cache.registry_view(self.jobs, append_only=True)

    @staticmethod
    def _truth(raw):
        """Complete rows and owner candidates of ``raw``, parsed without the cache."""
        view = registry_cache.RegistryView("truth", "truth", (0, 0, len(raw), 0, 0), raw)
        rows = view.lines[:view.line_count - (1 if view.has_partial_tail else 0)]
        return rows, {(row.order, row.raw) for row in view.settlement_owner_candidates()}

    def _remember(self, context, view):
        context.remember_owner_cursor(self.jobs, view)
        info = os.stat(self.jobs)
        return self._truth(self.jobs.read_bytes()) + ((info.st_dev, info.st_ino),)

    def _judge(self, context, view, cursor, label=""):
        """The floor and the first wrong thing it would skip, if any."""
        raw = self.jobs.read_bytes()
        rows, candidates = self._truth(raw)
        self.assertEqual(view.lines, self._truth_lines(raw), f"the view itself is exact {label}")
        floor = context.owner_candidate_floor(self.jobs, view)
        proven_rows, proven, _identity = cursor
        if floor >= len(proven_rows):
            return floor, f"floor {floor} beyond the {len(proven_rows)} proven rows"
        if rows[:floor + 1] != proven_rows[:floor + 1]:
            changed = next(order for order, (old, new) in enumerate(zip(proven_rows, rows))
                           if old != new)
            return floor, f"floor {floor} skips changed row {changed}"
        floored = ({item for item in proven if item[0] <= floor}
                   | {(row.order, row.raw)
                      for row in view.settlement_owner_candidates_since(floor)})
        if floored != candidates:
            return floor, "floored candidates differ from a full scan"
        return floor, None

    @staticmethod
    def _proves_rows(view):
        """Whether ``view`` has complete rows its LF segment digests describe."""
        return view.last_complete_order >= 0 and view._prefix_reuse_eligible

    @staticmethod
    def _truth_lines(raw):
        view = registry_cache.RegistryView("truth", "truth", (0, 0, len(raw), 0, 0), raw)
        return view.lines

    def test_a_row_changed_since_the_cursor_is_never_below_the_floor(self):
        # The reviewer probes inode_reuse.py, inode_reuse2.py (and its read
        # paths), and a same-size rewrite in place, each followed by the read
        # the supervisor makes. Row 5 changes after the cursor generation.
        def replaced_then_appended_by_rename(lines, onto):
            lines[5] = self._owner("att-5")
            self._rename(self._data(lines))
            registry_cache.registry_view(self.jobs)
            lines.append(self._owner("att-new"))
            self._rename(self._data(lines), onto=onto)

        def replaced_twice_then_appended(lines, onto):
            lines[5] = self._owner("att-5", status="open")
            self._rename(self._data(lines))
            registry_cache.registry_view(self.jobs)
            lines[19] = self._owner("att-19", extra=",x=1")
            self._rename(self._data(lines), onto=onto)
            registry_cache.registry_view(self.jobs)
            lines.append(self._owner("att-new"))
            self._append(lines[-1] + b"\n")

        def rewritten_in_place_then_appended(lines, _onto):
            lines[5] = self._owner("att-5", status="dead")     # same length as "done"
            self._rewrite_in_place(self._data(lines))
            registry_cache.registry_view(self.jobs)
            lines.append(self._owner("att-new"))
            self._append(lines[-1] + b"\n")

        shapes = (
            ("replaced, then appended by rename", replaced_then_appended_by_rename, "running",
             (False, True)),
            ("replaced twice, then appended in place", replaced_twice_then_appended, "done",
             (False, True)),
            ("rewritten in place, then appended in place", rewritten_in_place_then_appended,
             "done", (False,)),
        )
        for name, change, first_status, holds in shapes:
            for hold in holds:
                for how in self.READS:
                    with self.subTest(shape=name, inode_back=hold, read=how):
                        self._case(f"{len(holds)}-{hold}-{how}-{name.replace(' ', '_')}")
                        context = obligations.OwnerSettlementReadContext()
                        self.addCleanup(context.close)
                        lines = [self._owner(f"att-{number}") for number in range(20)]
                        lines[5] = self._owner("att-5", status=first_status)
                        self._rename(self._data(lines))
                        first = registry_cache.registry_view(self.jobs, append_only=True)
                        cursor = self._remember(context, first)
                        held = self._hold_inode() if hold else None
                        change(lines, held)
                        view = self._read(how)
                        info = os.stat(self.jobs)
                        if hold or "in place" in name.split(",")[0]:
                            # The judged case really has the cursor's inode number.
                            self.assertEqual((info.st_dev, info.st_ino), cursor[2])
                        floor, wrong = self._judge(context, view, cursor)
                        self.assertIsNone(wrong)
                        self.assertLess(floor, 5)
                        self.assertIn((5, lines[5].decode()),
                                      {(row.order, row.raw)
                                       for row in view.settlement_owner_candidates_since(floor)})

    def test_floor_skips_only_rows_the_cursor_generation_proved(self):
        # Random histories: appends in place, rename replacements (some bringing
        # an older generation's inode back), rewrites in place, lagging cursors
        # and every read path. Floor-skipped plus floor-scanned candidates must
        # be a full scan's candidates at every step. A rewrite in place keeps or
        # shrinks the size: same-inode growth is the append the append_only
        # caller asserts, and no registry writer rewrites in place at all.
        separators = (b"\rX", b"\xe2\x80\xa8Y", b"\xff\xfe")
        totals = {"steps": 0, "floors": 0, "inode_back_changed": 0, "append_only_warm": 0}
        wrong, narrowed = [], []
        for seed in (1113, 2026, 7):
            rng = random.Random(seed)
            self._case(f"fuzz-{seed}")
            context = obligations.OwnerSettlementReadContext()
            self.addCleanup(context.close)
            serial = [0]

            def row():
                serial[0] += 1
                pick = rng.random()
                if pick < 0.03:
                    return b""
                if pick < 0.06:
                    return b"a\tb\tc"
                status = rng.choice(("open", "running", "done", "failed"))
                attempt = f"att-{rng.randrange(40)}" if rng.random() < 0.3 else f"att-u{serial[0]}"
                if pick < 0.6:
                    return self._owner(attempt, status=status)
                return (f"2026-10-18T00:00:00Z\t{status}\t/repo\t/wt\tchild\t"
                        f"attempt_id={attempt},parent_attempt_id=att-{rng.randrange(40)}").encode()

            lines = [row() for _ in range(rng.choice((0, 3, 300, 511, 512, 513, 1100)))]
            tail = b""
            self._rename(self._data(lines))
            view = self._read("window")
            cursor = self._remember(context, view)
            whole, appends_only = view.last_complete_order, self._proves_rows(view)
            held = None
            for step in range(150):
                op = rng.choices(
                    ("append", "append-many", "append-partial", "change", "same-length",
                     "append-by-rename", "identical", "delete-front", "swap", "insert",
                     "shorter", "boundary", "inode-back", "in-place-same-size",
                     "in-place-shrink", "separator", "clean"),
                    (10, 5, 2, 5, 4, 5, 3, 2, 2, 2, 2, 3, 6, 3, 2, 1, 1))[0]
                if op.startswith("append") and op != "append-by-rename":
                    if op == "append-partial":
                        piece = row()[:rng.randrange(1, 20)]
                        tail += piece
                        self._append(piece)
                    else:
                        new = [row() for _ in range(1 if op == "append" else rng.randrange(2, 30))]
                        self._append(self._data(new))
                        new[0] = tail + new[0]
                        tail = b""
                        lines.extend(new)
                else:
                    if op in ("change", "inode-back") and lines:
                        lines[rng.randrange(len(lines))] = row()
                    elif op in ("same-length", "in-place-same-size", "boundary") and lines:
                        index = rng.randrange(len(lines))
                        if op == "boundary" and len(lines) > 520:
                            index = rng.choice((510, 511, 512, 513, len(lines) - 1))
                        text = bytearray(lines[index])
                        if text:
                            spot = rng.randrange(len(text))
                            if text[spot:spot + 1] not in (b"\t",):
                                text[spot] = ord("q") if text[spot] != ord("q") else ord("r")
                        lines[index] = bytes(text)
                    elif op == "append-by-rename":
                        lines.extend(row() for _ in range(rng.randrange(1, 4)))
                    elif op == "delete-front" and lines:
                        del lines[:rng.randrange(1, min(len(lines), 4) + 1)]
                    elif op == "swap" and len(lines) > 1:
                        first, second = rng.randrange(len(lines)), rng.randrange(len(lines))
                        lines[first], lines[second] = lines[second], lines[first]
                    elif op == "insert":
                        lines.insert(rng.randrange(len(lines) + 1), row())
                    elif op in ("shorter", "in-place-shrink") and lines:
                        del lines[max(0, len(lines) - rng.randrange(1, 30)):]
                    elif op == "separator" and lines:
                        index = rng.randrange(len(lines))
                        lines[index] += rng.choice(separators)
                    elif op == "clean":
                        for separator in separators:
                            lines = [line.replace(separator, b"") for line in lines]
                    data = self._data(lines, tail)
                    if op.startswith("in-place"):
                        self._rewrite_in_place(data)
                    elif (op == "inode-back" and held is not None
                          and os.stat(held).st_ino != os.stat(self.jobs).st_ino):
                        # Like a freed number handed out again: the cache has
                        # moved on to another inode, so nothing holds it open.
                        self._rename(data, onto=held)
                        held = None
                    else:
                        self._rename(data)
                how = rng.choice(self.READS)
                view = self._read(how)
                info = os.stat(self.jobs)
                floor, problem = self._judge(context, view, cursor, (seed, step, op, how))
                totals["steps"] += 1
                totals["floors"] += floor >= 0
                current_rows = self._truth(self.jobs.read_bytes())[0]
                if ((info.st_dev, info.st_ino) == cursor[2]
                        and current_rows[:len(cursor[0])] != cursor[0]):
                    totals["inode_back_changed"] += 1
                if problem is not None:
                    wrong.append((seed, step, op, how, problem))
                # A cold read starts the cache over with new segment boundaries;
                # from then until the next cursor the floor may only narrow.
                appends_only = (appends_only and how in self.WARM_READS
                                and op in ("append", "append-many", "append-partial"))
                if appends_only:
                    totals["append_only_warm"] += 1
                    if floor != whole:
                        narrowed.append((seed, step, op, how, floor, whole))
                if rng.random() < 0.4:
                    cursor = self._remember(context, view)
                    whole, appends_only = view.last_complete_order, self._proves_rows(view)
                    if held is not None and rng.random() < 0.5:
                        os.unlink(held)
                        held = None
                    if held is None:
                        held = self._hold_inode()
        self.assertEqual(wrong, [], f"{len(wrong)} wrong floors in {totals}")
        # Only appends since the cursor, every read warm: the whole floor is kept.
        self.assertEqual(narrowed, [], f"{len(narrowed)} narrowed floors in {totals}")
        # The histories reached the hole (the cursor's inode number holding other
        # bytes) and the append-only steps the floor exists for.
        self.assertGreater(totals["inode_back_changed"], 10, totals)
        self.assertGreater(totals["append_only_warm"], 30, totals)

    def test_an_append_only_history_keeps_the_whole_floor_across_compaction(self):
        # A lagging cursor over in-place appends keeps its whole floor through
        # every warm read path, also once old segments are compacted together.
        lines = [self._owner(f"att-{number}") for number in range(600)]
        self._rename(self._data(lines))
        context = obligations.OwnerSettlementReadContext()
        self.addCleanup(context.close)
        view = self._read("window")
        cursor = self._remember(context, view)
        whole = view.last_complete_order
        registry_cache.reset_metrics()
        for step in range(400):
            lines.append(self._owner(f"att-new-{step}"))
            self._append(lines[-1] + b"\n")
            how = self.WARM_READS[step % len(self.WARM_READS)]
            view = self._read(how)
            floor, wrong = self._judge(context, view, cursor)
            self.assertIsNone(wrong, f"step {step} via {how}")
            self.assertEqual(floor, whole, f"step {step} via {how}")
            if step % 3 == 2:
                cursor = self._remember(context, view)
                whole = view.last_complete_order
        metrics = registry_cache.metrics_snapshot()
        self.assertGreaterEqual(metrics["segment_compactions"], 1)
        self.assertEqual(metrics["full_metadata_parses"], 0, "every read stayed incremental")



class OwnerScanResumeOrderProofTest(unittest.TestCase):
    """An unfinished budgeted scan resumes only over rows whose bytes it proved.

    2026-11-13 review of #1580, scenario B (head 0/5, base 0/5): the resume
    order was keyed by the registry path alone. A registration
    (``launch_started=0``) behind it that launched and finished between two
    passes, or a finished owner not yet proved that an archive pulled behind
    it, was skipped as already proved; the pass finishing the scan then moved
    the candidate cursor over it, and its settlement was never made. Every pass
    here is the runner's real entry, ``_resume_owner_settlements``.
    """

    setUp = BatchObligationTest.setUp

    def _case(self, name):
        registry_cache.clear_cache()
        self.base = Path(self.temp.name) / name
        self.base.mkdir()
        self.jobs = self.base / "jobs.log"

    @staticmethod
    def _owner(attempt, *, status="done", launched="1", note="completed-supervisor"):
        metadata = ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            f"launch_started={launched}", "supervisor_lease=flock-v1",
            "supervisor_lease_file=/fixture/owner.lease", "log_file=/fixture/owner.log",
            f"attempt_id={attempt}", "workflow_completion=runtime-v1", f"note={note}"))
        return f"2026-11-13T00:00:00Z\t{status}\t/repo\t/wt\towner\t{metadata}"

    @staticmethod
    def _child(number):
        return (f"2026-11-13T00:00:00Z\tdone\t/repo\t/wt\tchild\t"
                f"attempt_id=att-child-{number},parent_attempt_id=att-elsewhere")

    def _replace(self, lines):
        """Replace the registry by rename, as every registry rewriter does."""
        fd, staged = tempfile.mkstemp(prefix=".jobs.log.claim-", dir=str(self.base))
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("".join(line + "\n" for line in lines))
        os.replace(staged, self.jobs)

    def _append(self, lines):
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("".join(line + "\n" for line in lines))

    def _runner(self, *, cacheable=True):
        """A runner context whose 5 s budget admits one owner proof per pass.

        An owner whose row says ``note=needs-settlement`` owes settlement until
        its settlement write (recorded, not run) is made. ``cacheable=False``
        keeps no settled proof, so only the resume order spares a re-proof.
        """
        dependency = self.base / "terminal-evidence.json"
        dependency.write_text("sealed", encoding="utf-8")
        run = Namespace(now=[0.0], proofs=[], settled=[], resumes=[], classifying=[False])

        def state(_jobs, _status, metadata):
            if run.classifying[0]:
                run.proofs.append(metadata["attempt_id"])
                run.now[0] += 10.0  # each proof costs more than the pass budget
            owes = (metadata.get("note") == "needs-settlement"
                    and metadata["attempt_id"] not in run.settled)
            return PENDING if owes else COMPLETE

        classify = obligations._classify_owner_candidates

        def observed_classify(*args, **kwargs):
            run.resumes.append(kwargs.get("resume_after"))
            run.classifying[0] = True
            try:
                return classify(*args, **kwargs)
            finally:
                run.classifying[0] = False

        context = obligations.OwnerSettlementReadContext()
        context._dependency_watcher_fallback = True
        context._clock = lambda: run.now[0]
        self.addCleanup(context.close)
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(
            obligations, "_owner_settlement_dependency_paths",
            return_value=(dependency,) if cacheable else None))
        patches.enter_context(mock.patch.object(
            obligations, "_classify_owner_candidates", side_effect=observed_classify))
        patches.enter_context(mock.patch(
            "dispatch_terminal_commit.owner_completion_state", side_effect=state))
        patches.enter_context(mock.patch(
            "dispatch_completion_join.materialize_after_terminal_close",
            side_effect=lambda _jobs, attempt, *a, **k: run.settled.append(attempt)))
        patches.enter_context(split_patch(
            "dispatch_contract.supervisor_lease_is_held", return_value=False))

        def one_pass(how="window"):
            """One runner pass; whether its historical scan is still unfinished."""
            if how == "gap":
                # A pass later than the retained-descriptor window.
                with mock.patch.object(registry_cache, "_APPEND_OBSERVATION_GAP_SECONDS", -1.0):
                    obligations._resume_owner_settlements(self.jobs, context=context)
            else:
                if how == "plain-first":
                    registry_cache.registry_view(self.jobs)
                obligations._resume_owner_settlements(self.jobs, context=context)
            return context.owner_scan_unfinished(self.jobs)

        run.context, run.one_pass = context, one_pass
        return run

    def _until_settled(self, run, attempt, passes=30):
        """Passes until ``attempt`` is settled and the scan finished; the count."""
        for count in range(1, passes + 1):
            unfinished = run.one_pass()
            if attempt in run.settled and not unfinished:
                return count
        self.fail(f"{attempt} settled={attempt in run.settled}, scan finished={not unfinished}"
                  f" after {passes} passes; proofs {run.proofs}")

    def test_a_registration_finished_behind_the_resume_order_is_settled(self):
        # The review probe e2e_settlement.py, scenario B, through the runner
        # entry: the registration finishes while several rows remain, or just
        # before the pass that finishes the scan.
        for remaining in ("several rows left", "one row left"):
            with self.subTest(remaining=remaining):
                self._case(remaining.replace(" ", "-"))
                lines = [self._owner(f"att-{number}") for number in range(20)]
                lines[5] = self._owner("att-5", status="open", launched="0")
                self._replace(lines)
                run = self._runner()
                run.one_pass()  # an unbudgeted full pass leaves the cursor here
                self.assertEqual(run.context.owner_scan_unfinished(), ())
                run.context.limit_owner_scan(5.0)
                for number in range(12, 20):  # later owners finished again: re-proved
                    lines[number] = self._owner(f"att-{number}", note="completed-again")
                self._replace(lines)
                for _ in range(1 if remaining == "several rows left" else 7):
                    self.assertTrue(run.one_pass())
                order = dict(run.context.owner_scan_unfinished())[str(self.jobs.resolve())]
                self.assertGreater(order, 5, "the resume order has passed the registration")
                lines[5] = self._owner("att-5", note="needs-settlement")  # launched, finished
                self._replace(lines)
                self._until_settled(run, "att-5")
                self.assertEqual(run.settled.count("att-5"), 1)

    def test_an_unproved_owner_an_archive_pulls_behind_the_resume_order_is_settled(self):
        # A just-started runner proves cold history a row per pass; an archive
        # pass moves the oldest group out by rename, which pulls the finished,
        # never-proved att-14 from row 14 to row 8, behind the resume order 10.
        lines = [self._owner(f"att-{number}") for number in range(20)]
        lines[14] = self._owner("att-14", note="needs-settlement")
        self._replace(lines)
        run = self._runner()
        run.context.limit_owner_scan(5.0)
        for _ in range(11):
            self.assertTrue(run.one_pass())
        self.assertEqual(run.proofs, [f"att-{number}" for number in range(11)])
        self.assertEqual(run.context.owner_scan_unfinished(), ((str(self.jobs.resolve()), 10),))
        del lines[:6]
        self._replace(lines)
        self._until_settled(run, "att-14")
        self.assertEqual(run.proofs, [f"att-{number}" for number in range(20)],
                         "every owner is proved once: the moved rows again, none twice")

    def test_appends_keep_the_resume_order_of_an_unfinished_scan(self):
        # Appends in place change no proved row: each pass resumes after the
        # last proved owner, and the scan takes one pass per owner as before.
        # No proof is cached here, so a pass starting over would re-prove the
        # first owner every time and never finish.
        for how in ("window", "gap", "plain-first"):
            for filler in (0, 600):
                with self.subTest(read=how, rows_before_the_owners=filler):
                    self._case(f"append-{how}-{filler}")
                    lines = [self._child(number) for number in range(filler)]
                    lines += [self._owner(f"att-{number}") for number in range(8)]
                    self._replace(lines)
                    run = self._runner(cacheable=False)
                    run.context.limit_owner_scan(5.0)
                    attempts = [f"att-{number}" for number in range(8)]
                    passes = 0
                    while run.one_pass(how):
                        passes += 1
                        self.assertLess(passes, 30, f"the scan never finished: {run.proofs}")
                        appended = [self._child(f"{passes}-{number}") for number in range(3)]
                        if passes in (2, 5):
                            appended.append(self._owner(f"att-appended-{passes}"))
                            attempts.append(f"att-appended-{passes}")
                        self._append(appended)
                    passes += 1
                    self.assertEqual(run.proofs, attempts, "each owner once, in registry order")
                    self.assertEqual(passes, len(attempts), "one pass per owner")
                    self.assertNotIn(None, run.resumes[1:], "no pass started over")

if __name__ == "__main__":
    unittest.main()
