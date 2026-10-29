#!/usr/bin/env python3
"""Isolated real-runner regression for managed message and retire workloads."""

from __future__ import annotations

import hashlib
import fcntl
import contextlib
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_registry_cache  # noqa: E402
import dispatch_batch_obligations  # noqa: E402
import dispatch_completion_join  # noqa: E402
import dispatch_pending_delivery  # noqa: E402
import peer_obligations  # noqa: E402
from dispatch_contract import process_namespace_identity  # noqa: E402
from peer_read_context import PeerReadContext  # noqa: E402

_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "runner_workload_correction_peer_steward", ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)
peer_message = peer_steward.peer_message


def _assert_fixture_path(path, label, fixture_root, *, dir_fd=None, cache=None):
    """Resolve the path the syscall sees, including a relative dir_fd target."""
    fixture_root = Path(fixture_root).resolve(strict=False)
    is_fd_path = isinstance(path, int)
    if is_fd_path:
        try:
            resolved = Path(f"/proc/self/fd/{path}").resolve(strict=True)
        except OSError as exc:
            raise AssertionError(f"{label} refers to an unavailable file descriptor") from exc
    else:
        raw = Path(os.fsdecode(os.fspath(path))).expanduser()
        if raw.is_absolute():
            resolved = raw.resolve(strict=False)
        elif dir_fd is not None:
            try:
                directory = Path(f"/proc/self/fd/{int(dir_fd)}").resolve(strict=True)
            except OSError as exc:
                raise AssertionError(f"{label} refers to an unavailable directory descriptor") from exc
            if not directory.is_dir():
                raise AssertionError(f"{label} dir_fd does not refer to a directory: {directory}")
            resolved = (directory / raw).resolve(strict=False)
        else:
            resolved = raw.resolve(strict=False)

    if not resolved.is_relative_to(fixture_root):
        raise AssertionError(f"{label} escaped the temporary fixture: {resolved}")
    # An FD number can be closed and reused for an unrelated directory. Never
    # memoize an FD-relative resolution under only its textual basename.
    if cache is not None and not is_fd_path and dir_fd is None:
        cache[str(resolved)] = resolved
    return resolved


class RunnerWorkloadCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-runner-workload-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.dispatch = self.base / "dispatch-state"
        self.dispatch.mkdir()
        self.home = self.base / "home"
        for path in (self.home, self.home / ".config", self.home / ".local/share",
                     self.home / ".local/state", self.home / ".cache", self.base / "release",
                     self.base / "codex", self.base / "claude", self.base / "opencode"):
            path.mkdir(parents=True, exist_ok=True)
        self.jobs = self.dispatch / "jobs.log"
        self.jobs.touch()

        self.saved_env = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                               "AGENT_THREAD_", "AGENT_HERDR_", "HERDR_", "HARNESS_",
                               "CLAUDE_", "CODEX_", "OPENCODE_")):
                os.environ.pop(key, None)
        os.environ.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "AGENT_HOME": str(self.base / "release"),
            "AGENT_PEER_LEDGER_ROOT": str(self.dispatch),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "CODEX_HOME": str(self.base / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "OPENCODE_CONFIG_DIR": str(self.base / "opencode"),
        })
        self.addCleanup(self._restore_environment)
        # This workload books every requester on the private "default" server.
        # The imported module otherwise retains the driver's earlier endpoint,
        # which makes its real successor mark contradict those same bookings.
        endpoint = mock.patch.object(peer_steward, "_HERDR_SESSION", "default")
        endpoint.start()
        self.addCleanup(endpoint.stop)
        dispatch_registry_cache.clear_cache()
        dispatch_registry_cache.reset_metrics()
        self.addCleanup(dispatch_registry_cache.clear_cache)
        self.store = peer_obligations.ObligationStore(self.dispatch)

    def _restore_environment(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    @staticmethod
    def _session_id(label, index):
        digest = hashlib.sha256(f"synthetic-{label}-{index}".encode()).hexdigest()
        return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

    def test_fixture_path_guard_binds_dir_fd_targets_and_fd_reuse(self):
        fixture = self.base / "fd-guard-fixture"
        first_dir = fixture / "first"
        second_dir = fixture / "second"
        first_dir.mkdir(parents=True)
        second_dir.mkdir()
        (first_dir / "same-name").write_text("first", encoding="utf-8")
        (second_dir / "same-name").write_text("second", encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="hearting-fd-guard-outside-") as outside_name:
            outside = Path(outside_name)
            (outside / "same-name").write_text("outside", encoding="utf-8")
            first_fd = os.open(first_dir, os.O_RDONLY | os.O_DIRECTORY)
            second_fd = os.open(second_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                first_target = _assert_fixture_path(
                    "same-name", "first same basename", fixture,
                    dir_fd=first_fd, cache={})
                second_target = _assert_fixture_path(
                    "same-name", "second same basename", fixture,
                    dir_fd=second_fd, cache={})
                self.assertNotEqual(first_target, second_target)
                self.assertEqual(first_target, first_dir / "same-name")
                self.assertEqual(second_target, second_dir / "same-name")
                outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(AssertionError):
                        _assert_fixture_path("same-name", "outside directory fd", fixture,
                                             dir_fd=outside_fd)
                finally:
                    os.close(outside_fd)
                with self.assertRaises(AssertionError):
                    _assert_fixture_path(outside / "same-name", "absolute outside path",
                                         fixture, dir_fd=first_fd)
                with self.assertRaises(AssertionError):
                    _assert_fixture_path(os.path.relpath(outside / "same-name", first_dir),
                                         "parent escape",
                                         fixture, dir_fd=first_fd)

                file_fd = os.open(first_dir / "same-name", os.O_RDONLY)
                try:
                    self.assertEqual(_assert_fixture_path(
                        file_fd, "opened file descriptor", fixture),
                        first_dir / "same-name")
                finally:
                    os.close(file_fd)

                temporary_name, final_name = "publish.tmp", "published.json"
                temporary_fd = os.open(temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=first_fd)
                try:
                    os.write(temporary_fd, b"fixture-publish")
                finally:
                    os.close(temporary_fd)
                self.assertEqual(_assert_fixture_path(
                    temporary_name, "FD temporary", fixture, dir_fd=first_fd),
                    first_dir / temporary_name)
                os.replace(temporary_name, final_name,
                           src_dir_fd=first_fd, dst_dir_fd=first_fd)
                self.assertEqual(_assert_fixture_path(
                    final_name, "FD replacement", fixture, dir_fd=first_fd),
                    first_dir / final_name)
                os.unlink(final_name, dir_fd=first_fd)
                self.assertFalse((first_dir / final_name).exists())
            finally:
                os.close(first_fd)
                os.close(second_fd)

            cache = {}
            reused_fd = os.open(first_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                _assert_fixture_path("same-name", "cached first fd", fixture,
                                     dir_fd=reused_fd, cache=cache)
                os.close(reused_fd)
                outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
                if outside_fd != reused_fd:
                    os.dup2(outside_fd, reused_fd)
                    os.close(outside_fd)
                self.assertEqual(os.fstat(reused_fd).st_ino, outside.stat().st_ino)
                with self.assertRaises(AssertionError):
                    _assert_fixture_path("same-name", "reused fd", fixture,
                                         dir_fd=reused_fd, cache=cache)
            finally:
                with contextlib.suppress(OSError):
                    os.close(reused_fd)

    def _message(self, index, *, state="pending", receipt="target-form-open",
                 dispatch_binding=None, initial_observation=None):
        sender = {"harness": "claude", "session_id": self._session_id("sender", index),
                  "name": f"source-{index:03d}"}
        session_id = (self.active_session_id if dispatch_binding else
                      self._session_id("recipient", index))
        recipient = {"harness": "codex", "session_id": session_id,
                     "name": f"recipient-{index:03d}", "pane_id": f"synthetic-pane-{index:03d}"}
        duty_id = "message-" + hashlib.sha256(f"synthetic-duty-{index}".encode()).hexdigest()[:32]
        refs = [dispatch_binding["duty_id"]] if dispatch_binding else []
        body = "synthetic operational-shape message " + ("context evidence " * (index % 9 + 1))
        # Fixture production uses no existing-message lookup. Every resulting
        # pending/transfer row is still written and validated by production code.
        with mock.patch.object(peer_message, "_pending_rows", return_value=()):
            _text, ref = peer_message.prepare_peer_message(
                body, sender, recipient, defer=True, refs=refs, with_trailer=False)
        pending_path = peer_message._pending_path(ref)
        if state != "pending" or dispatch_binding:
            row = json.loads(pending_path.read_text(encoding="utf-8"))
            row["state"] = state
            row["receipt"] = receipt
            if dispatch_binding:
                row["dispatch_notice"] = dict(dispatch_binding)
            peer_message._save_pending(row)
        duty = self.store.create(
            duty_id, "message",
            {"server": "default", "harness": recipient["harness"],
             "session_id": recipient["session_id"]},
            {"ref": ref, "target": recipient["name"], "to": recipient, "from": sender},
        )
        if initial_observation is not None:
            self.store.update(duty_id, state="unknown", observation=initial_observation)
        return duty_id, ref, recipient["pane_id"]

    def _seed_workload(self, *, recurring_negative=False):
        from harness_capabilities import parent_completion

        carrier = parent_completion("codex")["carrier"]
        discovered_roots, _source = peer_message.steward_marker_roots()
        self.assertTrue(discovered_roots)
        for discovered in discovered_roots:
            self.assertTrue(Path(discovered).resolve(strict=False).is_relative_to(self.base.resolve()),
                            f"synthetic root escaped its temp fixture: {discovered}")
        namespace = process_namespace_identity()
        pid_start = peer_obligations._proc_start_identity(os.getpid())
        self.assertIsNotNone(pid_start)
        active_attempt = "att-" + "a" * 32
        self.active_session_id = self._session_id("registered-parent", 0)
        registered_ids = []
        for index in range(6):
            attempt = active_attempt if index == 5 else "att-" + hashlib.sha256(
                f"missing-owner-row-{index}".encode()).hexdigest()[:32]
            duty_id = "registered-batch-" + hashlib.sha256(
                f"synthetic-registered-batch-{index}".encode()).hexdigest()[:32]
            self.store.create(duty_id, "registered-batch", {
                "jobs": str(self.jobs.resolve()), "attempt_id": attempt,
                "harness": "codex", "server": "default",
            }, {"carrier": carrier})
            if index < 5:
                self.store.update(duty_id, state="unknown",
                                  observation={"reason": "pending-row-missing"})
            else:
                self.store.update(duty_id, observation={
                    "holder": [str(os.getpid()), str(pid_start), namespace]})
            registered_ids.append(duty_id)

        metadata = ",".join((
            f"attempt_id={active_attempt}", "attempt_schema_version=2",
            f"parent_sid={self.active_session_id}", "parent_harness=codex",
            f"parent_completion_delivery={carrier}",
            f"parent_runtime_pid={os.getpid()}",
            f"parent_runtime_pid_start={pid_start}", f"parent_runtime_ns={namespace}",
            "delivery_intent=1", "route_id=synthetic-route",
        ))
        active_row = (f"job\topen\t/repo\t2026-10-18T00:00:00Z\towner\t{metadata}\n")
        retire_rows = []
        self.completed_retire_ids = []
        self.retire_handover_meta = []
        for index in range(2):
            predecessor_sid = self._session_id("retired-parent", index)
            successor_sid = self._session_id("successor", index)
            predecessor_pane = f"w1:pOld{index}"
            successor_pane = f"w1:pNew{index}"
            successor_harness = "codex"
            peer_steward._mark_seat_successor(
                successor_pane, predecessor_pane, successor_harness, successor_sid)
            identity = {
                "server": "default", "pane": predecessor_pane, "harness": "claude",
                "session_id": predecessor_sid, "name": f"retired-{index}",
                "foreground": {"pid": 990000 + index, "start": str(8000 + index),
                               "argv": ["claude"], "shell_pid": 980000 + index,
                               "shell_start": str(7000 + index)},
            }
            requester = {"server": "default", "pane": successor_pane,
                         "harness": successor_harness, "session_id": successor_sid}
            duty_id = f"completed-retire-managed-{index}"
            duty = self.store.create(duty_id, "retire", identity,
                                     {"target": predecessor_pane, "requester": requester})
            self.store.update(duty_id, state="complete", result="target-already-gone",
                              delivery="completed", cleanup="complete",
                              observation={"phase": "complete", "reason": "target-already-gone"})
            self.completed_retire_ids.append(duty_id)
            attempt = f"att-retire-{index}"
            route_id = f"synthetic-retire-route-{index}"
            route_hash = "sha256:" + hashlib.sha256(route_id.encode()).hexdigest()
            route_meta = {
                "attempt_id": attempt, "attempt_schema_version": "2",
                "dispatch_depth": "1", "worker_type": "owner",
                "parent_sid": predecessor_sid, "parent_harness": "claude",
                "route_id": route_id, "route_hash": route_hash, "route_node": "execute",
            }
            self.retire_handover_meta.append((route_meta, successor_sid))
            status = "done" if recurring_negative else "open"
            retire_rows.append(f"job\t{status}\t/repo\t2026-10-18T00:00:00Z\towner\t"
                               + ",".join(f"{key}={value}" for key, value in route_meta.items()) + "\n")

        header = active_row + "".join(retire_rows)
        target_bytes = 23_499_871
        prefix = "job\topen\t/repo\t2026-10-18T00:00:00Z\tworker\t"
        full_padding = prefix + "x" * 4700 + "\n"
        remaining = target_bytes - len(header.encode("utf-8"))
        rows, tail = divmod(remaining, len(full_padding.encode("utf-8")))
        if tail and tail < len(prefix.encode("utf-8")) + 1:
            rows -= 1
            tail += len(full_padding.encode("utf-8"))
        padding = full_padding * rows
        if tail:
            padding += prefix + "x" * (tail - len(prefix.encode("utf-8")) - 1) + "\n"
        content = (header + padding).encode("utf-8")
        self.assertEqual(len(content), target_bytes)
        self.jobs.write_bytes(content)

        # Operational message census shape: 114 queued (112 hook-context and
        # two context-API), 73 pending, one unverified in herdr delivery, and
        # five absent rows. The sole dispatch_notice is on that unverified row.
        dispatch_index = registered_ids[5]
        binding = {
            "duty_id": dispatch_index, "jobs": str(self.jobs.resolve()),
            "attempt_id": active_attempt, "carrier": carrier,
            "recipient_sid": self.active_session_id,
            "storage_recipient": self.active_session_id,
            "delivery_id": "delivery-" + hashlib.sha256(b"synthetic-delivery").hexdigest()[:32],
            "claim_owner": "retained-rewake:" + dispatch_index,
        }
        self.message_duties = []
        for index in range(188):
            if index < 112:
                state, receipt = "queued", "hook-context-emitted"
            elif index < 114:
                state, receipt = "queued", "context-api-accepted"
            elif index == 187:
                state, receipt = "unverified", "herdr-delivery-inflight"
            else:
                state, receipt = "pending", "target-form-open"
            linked = binding if index == 187 else None
            duty_id, ref, target = self._message(
                index, state=state, receipt=receipt, dispatch_binding=linked)
            if state in {"queued", "unverified"}:
                observation = {"ref": ref, "transport_state": state, "reason": receipt}
            elif state == "pending":
                observation = {"ref": ref, "reason": "target-identity-changed",
                               "observed_pane": f"synthetic-pane-{index:03d}"}
            else:
                observation = None
            if observation is not None:
                self.store.update(duty_id, state="unknown", observation=observation)
            self.message_duties.append((duty_id, ref, target))

        # Five accepted message obligations have no readable own pending row.
        for index in range(188, 193):
            ref = hashlib.sha256(f"missing-own-pending-{index}".encode()).hexdigest()[:32]
            duty_id = "message-" + ref
            self.store.create(duty_id, "message", {"server": "default"},
                              {"ref": ref, "target": f"missing-{index}"})
            self.store.update(duty_id, state="unknown",
                              observation={"ref": ref, "reason": "pending-row-missing"})
            self.message_duties.append((duty_id, ref, f"missing-{index}"))

        # Existing completed rows are included because the real store scan reads
        # them even though only unfinished duties are returned to the runner.
        self.completed_terminal_ids = []
        terminal_fixture_rows = (("message", 362, "completed-message"),
                                 ("registered-batch", 253, "completed-batch"),
                                 ("retire", 85, "completed-retire"))
        for kind, count, prefix in terminal_fixture_rows:
            for index in range(count):
                duty_id = prefix + "-" + hashlib.sha256(
                    f"{prefix}-{index}".encode()).hexdigest()[:32]
                duty = self.store.create(duty_id, kind, {}, {})
                self.store.update(duty["id"], state="complete")
                self.completed_terminal_ids.append(duty_id)
        cancelled = self.store.create("cancelled-retire-synthetic", "retire", {}, {})
        self.store.update(cancelled["id"], state="cancelled")
        self.cancelled_fixture_id = cancelled["id"]
        self.negative_lineage_paths = []
        if recurring_negative:
            import session_tidy
            # Empty terminal work is now a valid zero-job handoff. Keep the
            # recurring negative workload using genuinely unreadable lineage.
            directory = session_tidy.state_root() / "sessions"
            directory.mkdir(parents=True, exist_ok=True)
            for index, (meta, _successor) in enumerate(self.retire_handover_meta):
                path = directory / f"negative-lineage-{index}.jsonl"
                path.write_text("".join(json.dumps({"event": "succession", "source": "tidy-clear",
                    "harness": "claude", "from": meta["parent_sid"],
                    "sid": f"negative-{index}-{branch}", "ts": 1}) + "\n" for branch in ("B", "C")))
                self.negative_lineage_paths.append(path)
        return registered_ids, carrier, active_attempt

    def _create_open_retire_delivery(self, index):
        # The same input event restores one source's observation; the other
        # source stays ambiguous and the existing runner must keep waiting.
        self.negative_lineage_paths[index].write_text("")
        meta, _successor_sid = self.retire_handover_meta[index]
        attempt_id = meta["attempt_id"]
        receipt = {
            "schema_version": 2,
            "state": "delivered",
            "parent_attempt_id": attempt_id,
            "job_registry": str(self.jobs),
            "children": [{
                "attempt_id": attempt_id,
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }],
            "delivery_classification": "attention",
        }
        delivery_id = "delivery-" + hashlib.sha256(
            f"retire-authority-{attempt_id}".encode()).hexdigest()[:32]
        record = dispatch_pending_delivery.create(
            self.dispatch,
            recipient_kind="claude-parent-runtime",
            recipient_key=meta["parent_sid"],
            delivery_id=delivery_id,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=[attempt_id],
            parent_attempt_id=attempt_id,
            route_id=meta["route_id"],
            route_node=meta["route_node"],
            receipt=receipt,
            receipt_digest=dispatch_pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={attempt_id: meta["route_hash"]},
        )
        path = dispatch_pending_delivery.record_path(
            self.dispatch, meta["parent_sid"], delivery_id)
        return {"record": record, "path": path, "delivery_id": delivery_id,
                "attempt_id": attempt_id, "recipient_key": meta["parent_sid"],
                "route_meta": meta}

    def test_real_runner_20_second_successful_handover_shape(self):
        self._run_real_runner_workload(recurring_negative=False)

    def test_real_runner_20_second_recurring_negative_retire_shape(self):
        self._run_real_runner_workload(recurring_negative=True)

    def _run_real_runner_workload(self, *, recurring_negative):
        registered_ids, _carrier, _attempt = self._seed_workload(
            recurring_negative=recurring_negative)
        fixture_record_count = (len(registered_ids) + len(self.message_duties)
                                + len(self.completed_retire_ids)
                                + len(self.completed_terminal_ids) + 1)
        expected_active_ids = (set(registered_ids)
                               | {duty_id for duty_id, _ref, _target in self.message_duties}
                               | set(self.completed_retire_ids))
        expected_cold_active_ids = (expected_active_ids if recurring_negative else
                                    expected_active_ids - set(self.completed_retire_ids))
        # The active row carries this host's pid, start tick and namespace, so
        # only the ~23 MB padding is fixed; the exact byte count varies by host.
        self.assertEqual(self.jobs.stat().st_size, 23_499_871)
        self.assertEqual(len(list(peer_message._pending_dir().glob("*.json"))), 188)
        self.assertEqual(len(list(peer_message._transfer_path("0" * 32).parent.glob("*.json"))), 188)
        self.assertEqual(len(list(self.store.root.glob("*.json"))), fixture_record_count)
        self.assertEqual(
            {row["id"] for row in self.store.list(fast_active_only=True)},
            expected_active_ids,
            "the fixture's active index target is its registered, message, and unresolved retire set",
        )
        self.assertEqual(len(self.store.list(states={"complete"})), 702)
        self.assertEqual(len(self.store.list(states={"cancelled"})), 1)
        self.assertEqual(len(self.completed_retire_ids), 2)
        self.assertTrue(all(self.store.get(duty_id)["state"] == "complete"
                            for duty_id in self.completed_retire_ids))
        self.assertEqual(sum(self.store.get(duty_id)["state"] in {"pending", "unknown"}
                             for duty_id, _ref, _target in self.message_duties), 193)
        registered_states = [self.store.get(duty_id)["state"] for duty_id in registered_ids]
        self.assertEqual(registered_states.count("unknown"), 5)
        self.assertEqual(registered_states.count("pending"), 1)
        initial_rows = [peer_message._read_pending(ref)
                        for _duty_id, ref, _target in self.message_duties[:188]]
        self.assertEqual(sum(row["state"] == "queued" for row in initial_rows), 114)
        self.assertEqual(sum(row["state"] == "pending" for row in initial_rows), 73)
        self.assertEqual(sum(row["state"] == "unverified" for row in initial_rows), 1)
        notice_indices = [index for index, row in enumerate(initial_rows)
                          if row.get("dispatch_notice")]
        self.assertEqual(notice_indices, [187])
        self.assertEqual(initial_rows[notice_indices[0]]["state"], "unverified")
        observations = [self.store.get(duty_id).get("observation") or {}
                        for duty_id, _ref, _target in self.message_duties]
        expected_reasons = {
            "hook-context-emitted": 112,
            "target-identity-changed": 73,
            "context-api-accepted": 2,
            "herdr-delivery-inflight": 1,
            "pending-row-missing": 5,
        }
        for reason, expected_count in expected_reasons.items():
            self.assertEqual(sum(observation.get("reason") == reason
                                 for observation in observations), expected_count)
        self.assertEqual(len(self.active_session_id), 36)

        fixture_root = self.base.resolve()
        discovered_roots = set()
        write_targets = set()
        observed_roots = set()
        validated_fixture_paths = {}
        fd_readiness_events = []
        completed_followup_write_ids = set()
        monitoring = [False]
        phase = ["setup"]

        def assert_fixture_path(path, label, *, dir_fd=None):
            guard_cpu_started = time.process_time()
            resolved = _assert_fixture_path(
                path, label, fixture_root, dir_fd=dir_fd,
                cache=validated_fixture_paths)
            if monitoring[0] and phase[0] == "runner":
                counts["fixture_path_guard_cpu_seconds"] += time.process_time() - guard_cpu_started
            observed_roots.add(str(resolved))
            return resolved

        def record_readiness_fd_path(operation, target, directory_fd):
            if directory_fd is None or "readiness-v1" not in target.parts:
                return
            directory = os.fstat(directory_fd)
            fd_readiness_events.append({
                "operation": operation, "path": str(target),
                "dir_fd": directory_fd, "directory_dev": directory.st_dev,
                "directory_ino": directory.st_ino,
            })

        initial_roots, _source = peer_message.steward_marker_roots()
        for root in initial_roots:
            discovered_roots.add(str(assert_fixture_path(root, "initial peer-state root")))

        counts = {
            "passes": 0, "passes_before_input": 0, "passes_after_input": 0,
            "message_resumes": 0, "registered_resumes": 0,
            "store_list_calls": 0, "store_record_reads": 0,
            "root_discoveries": 0, "root_candidates": 0,
            "pending_reads": 0, "transfer_record_reads": 0,
            "native_lookups": 0, "attempt_row_lookups": 0,
            "context_attempt_row_lookups": 0, "registry_line_calls": 0,
            "jobs_file_reads": 0, "obligation_writes": 0,
            "store_update_calls": 0,
            "retire_followup_completion_writes": 0,
            "input_obligation_writes": 0, "input_pending_writes": 0,
            "input_transfer_writes": 0, "write_open_calls": 0,
            "write_atomic_with_dir_fd": 0, "write_atomic_without_dir_fd": 0,
            "reconcile_retry_state_passes": 0,
            "replace_calls": 0, "unlink_calls": 0,
            "completed_retire_recoveries": 0, "handover_authority_calls": 0,
            "record_retire_calls": 0,
            "service_runner_calls": 0, "watch_activation_calls": 0,
            "batch_activation_calls": 0,
            "store_list_cpu_seconds": 0.0, "reconcile_cpu_seconds": 0.0,
            "external_cpu_seconds": 0.0,
            "message_cpu_seconds": 0.0, "registered_cpu_seconds": 0.0,
            "fixture_path_guard_cpu_seconds": 0.0,
        }
        window_phase = ["cold"]
        event = {}
        sleep_delays = []
        window = 20.0
        measurement = {}
        runner_store = []
        main_active_discovery_ids = []
        lock_identities = []
        lock_events = []
        expected_binding_locks = {}
        binding_observer_errors = []
        authority_counts = {}
        lineage_unknown_counts = {}
        retire_recovery_events = []
        record_retire_events = []
        retire_delivery_input = {}
        original_retire = self.store.get(self.completed_retire_ids[0])
        initial_jobs_digest = hashlib.sha256(self.jobs.read_bytes()).hexdigest()
        real_sleep = time.sleep
        real_os_open = os.open
        real_os_replace = os.replace
        real_os_unlink = os.unlink
        real_path_read_text = Path.read_text
        original_roots = peer_message.steward_marker_roots
        original_state_root = peer_message.peer_state_root
        original_watch_root = peer_steward._watch_root
        original_jobs_paths = dispatch_batch_obligations._jobs_paths
        original_observer_root = dispatch_batch_obligations._observer_root
        original_obligations_root = peer_obligations.obligations_root
        original_pending = peer_message._read_pending
        original_transfer = peer_message._read_transfer_record
        original_write = peer_obligations._write_atomic
        original_read_record = peer_obligations._read_record
        original_store_list = peer_obligations.ObligationStore.list
        original_store_update = peer_obligations.ObligationStore.update
        original_message_checkpoint = peer_obligations.ObligationStore.update_message_checkpoint
        original_external_reconcile = peer_steward._reconcile_external_obligations
        original_retire_handover = peer_steward._retire_handover
        import route_authority
        original_decide_continuation = route_authority.decide_continuation
        import dispatch_seat_handover
        original_record_retire = dispatch_seat_handover.record_retire_handover
        original_binding_mutexes = dispatch_seat_handover.binding_mutexes
        import session_tidy
        seat_lock_dir = session_tidy.state_root() / "locks"
        seat_lock_dir_text = os.fspath(seat_lock_dir)
        assert_fixture_path(seat_lock_dir, "seat lock directory")
        original_flock = fcntl.flock
        original_registry_lines = dispatch_registry_cache.registry_lines
        original_attempt_lookup = dispatch_completion_join.current_attempt_row
        original_context_lookup = PeerReadContext.current_attempt_row
        original_reconcile = peer_steward._reconcile_obligations
        original_resume_message = peer_steward._resume_message_obligation
        original_resume_registered = peer_steward._resume_registered_obligation
        original_save_pending = peer_message._save_pending
        original_prepare_message = peer_message._prepare_peer_message
        previous_kill = os.kill

        def checked_roots():
            roots, source = original_roots()
            for root in roots:
                discovered_roots.add(str(assert_fixture_path(root, "discovered peer-state root")))
            if monitoring[0] and phase[0] == "runner":
                counts["root_discoveries"] += 1
                counts["root_candidates"] += len(roots)
            return roots, source

        def checked_state_root():
            return assert_fixture_path(original_state_root(), "peer state root")

        def checked_watch_root():
            return assert_fixture_path(original_watch_root(), "watch root")

        def checked_jobs_paths(jobs, *, strict=True):
            paths = original_jobs_paths(jobs, strict=strict)
            for path in paths:
                assert_fixture_path(path, "dispatch registry")
            return paths

        def checked_observer_root(jobs):
            root = original_observer_root(jobs)
            if root is not None:
                assert_fixture_path(root, "batch obligation root")
            return root

        def checked_obligations_root(root=None):
            return assert_fixture_path(original_obligations_root(root), "peer obligation root")

        def checked_pending(ref, **kwargs):
            assert_fixture_path(peer_message._pending_path(ref), "pending-row read")
            if monitoring[0] and phase[0] == "runner":
                counts["pending_reads"] += 1
            return original_pending(ref, **kwargs)

        def checked_transfer(ref, *, roots=None):
            assert_fixture_path(peer_message._transfer_path(ref), "transfer-row read")
            for root in roots or ():
                assert_fixture_path(root, "transfer candidate root")
            if monitoring[0] and phase[0] == "runner":
                counts["transfer_record_reads"] += 1
            return original_transfer(ref, roots=roots)

        def checked_write(path, value, *, dir_fd=None):
            target = assert_fixture_path(path, "obligation write", dir_fd=dir_fd)
            record_readiness_fd_path("atomic-write", target, dir_fd)
            if monitoring[0]:
                if phase[0] == "input":
                    counts["input_obligation_writes"] += 1
                elif phase[0] == "runner":
                    counts["obligation_writes"] += 1
                    counts["write_atomic_with_dir_fd" if dir_fd is not None
                           else "write_atomic_without_dir_fd"] += 1
                    observation = (value.get("observation") or {}) if isinstance(value, dict) else {}
                    if (isinstance(value, dict) and value.get("state") == "complete"
                            and value.get("result") in {"normal-exit", "target-already-gone"}
                            and observation.get("terminal_followup") == "complete"):
                        duty_id = value.get("id")
                        if duty_id not in completed_followup_write_ids:
                            completed_followup_write_ids.add(duty_id)
                            counts["retire_followup_completion_writes"] += 1
                write_targets.add(str(target))
            return original_write(path, value, dir_fd=dir_fd)

        def checked_read_record(path):
            assert_fixture_path(path, "obligation record read")
            if monitoring[0] and phase[0] == "runner":
                counts["store_record_reads"] += 1
            return original_read_record(path)

        def checked_store_list(store, *args, **kwargs):
            assert_fixture_path(store.root, "obligation listing root")
            is_main_unfiltered_list = not args and not kwargs
            if not runner_store and is_main_unfiltered_list:
                runner_store.append(store)
            is_runner_store = bool(runner_store and store is runner_store[0])
            if (is_runner_store and monitoring[0] and phase[0] == "runner"):
                counts["store_list_calls"] += 1
            cpu_started = time.process_time()
            before_metrics = store.discovery_metrics() if is_runner_store else None
            try:
                result = original_store_list(store, *args, **kwargs)
                after_metrics = store.discovery_metrics() if is_runner_store else None
                if (is_runner_store and is_main_unfiltered_list
                        and not main_active_discovery_ids
                        and after_metrics["json_reads"] > before_metrics["json_reads"]):
                    main_active_discovery_ids.append(
                        tuple(row.get("id") for row in result if isinstance(row, dict)))
                return result
            finally:
                if (is_runner_store and monitoring[0] and phase[0] == "runner"):
                    counts["store_list_cpu_seconds"] += time.process_time() - cpu_started

        def checked_store_update(store, duty_id, **kwargs):
            if (runner_store and store is runner_store[0]
                    and monitoring[0] and phase[0] == "runner"):
                counts["store_update_calls"] += 1
            return original_store_update(store, duty_id, **kwargs)

        def checked_message_checkpoint(store, duty_id, **kwargs):
            if monitoring[0] and phase[0] == "runner":
                counts["store_update_calls"] += 1
            return original_message_checkpoint(store, duty_id, **kwargs)

        def checked_registry_lines(jobs):
            assert_fixture_path(jobs, "jobs registry read")
            if monitoring[0] and phase[0] == "runner":
                counts["registry_line_calls"] += 1
            return original_registry_lines(jobs)

        def checked_attempt_lookup(jobs, attempt_id, *, read_context=None):
            assert_fixture_path(jobs, "attempt-row lookup")
            if monitoring[0] and phase[0] == "runner":
                counts["attempt_row_lookups"] += 1
            return original_attempt_lookup(jobs, attempt_id, read_context=read_context)

        def checked_context_lookup(context, jobs, attempt_id, *, append_only=None):
            assert_fixture_path(jobs, "cached attempt-row lookup")
            if monitoring[0] and phase[0] == "runner":
                counts["context_attempt_row_lookups"] += 1
            return original_context_lookup(context, jobs, attempt_id,
                                           append_only=append_only)

        def checked_retire_handover(duty, *, wait=None):
            if monitoring[0] and phase[0] == "runner":
                counts["completed_retire_recoveries"] += 1
                retire_recovery_events.append({
                    "duty_id": duty["id"], "pass": counts["passes"],
                    "window": window_phase[0],
                })
            return original_retire_handover(duty, wait=wait)

        def checked_authority(*args, **kwargs):
            choice = original_decide_continuation(*args, **kwargs)
            if monitoring[0] and phase[0] == "runner" and kwargs.get("action") == "handover":
                counts["handover_authority_calls"] += 1
                meta = kwargs.get("metadata") or {}
                attempt_id = meta.get("attempt_id")
                window_counts = authority_counts.setdefault(attempt_id, {})
                result_key = f"{window_phase[0]}_{'allowed' if choice.allowed else 'denied'}"
                window_counts[result_key] = window_counts.get(result_key, 0) + 1
                if (retire_delivery_input and attempt_id == retire_delivery_input["attempt_id"]
                        and choice.allowed and "observed_at" not in retire_delivery_input):
                    retire_delivery_input["observed_at"] = time.monotonic()
                    retire_delivery_input["observed_pass"] = counts["passes"]
                    retire_delivery_input["observed_window"] = window_phase[0]
            return choice

        def checked_record_retire(*args, **kwargs):
            if monitoring[0] and phase[0] == "runner":
                counts["record_retire_calls"] += 1
                record_retire_events.append({
                    "predecessor_sid": args[0] if args else kwargs.get("predecessor_sid"),
                    "pass": counts["passes"], "window": window_phase[0],
                })
            try:
                return original_record_retire(*args, **kwargs)
            except dispatch_seat_handover.RegistryUnreadable:
                source_sid = args[0] if args else kwargs.get("predecessor_sid")
                key = (source_sid, window_phase[0])
                lineage_unknown_counts[key] = lineage_unknown_counts.get(key, 0) + 1
                raise

        def checked_binding_mutexes(bindings, *, wait=None):
            bindings = list(bindings)
            if monitoring[0] and phase[0] == "runner":
                try:
                    # Derive exact logical identities from admitted operations.
                    # Phases are observed dynamically, including post-change.
                    phase_name = window_phase[0]
                    expected = expected_binding_locks.setdefault(phase_name, set())
                    for binding in bindings:
                        if not isinstance(binding, dict):
                            continue
                        identity = (str(binding.get("jobs") or ""),
                                    str(binding.get("route") or ""),
                                    str(binding.get("hash") or ""),
                                    str(binding.get("node") or ""))
                        if not identity[0] or not identity[1]:
                            continue
                        encoded = json.dumps(identity, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")
                        expected.add(
                            "lineage-binding-" + hashlib.sha256(encoded).hexdigest())
                except Exception as exc:  # the observer must not alter production behavior
                    binding_observer_errors.append(
                        f"{type(exc).__name__}: {exc} (phase={window_phase[0]!r})")
            return original_binding_mutexes(bindings, wait=wait)

        def checked_flock(fd, operation):
            result = original_flock(fd, operation)
            if (monitoring[0] and phase[0] == "runner"
                    and operation & fcntl.LOCK_EX):
                try:
                    target = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:
                    target = None
                if target is not None and os.path.dirname(target) == seat_lock_dir_text:
                    lock_key = os.path.basename(target).removesuffix(".lock")
                    lock_identities.append(lock_key)
                    lock_events.append({"key": lock_key, "pass": counts["passes"],
                                        "window": window_phase[0]})
            return result

        def checked_exec_service_runner(*args, **kwargs):
            if monitoring[0] and phase[0] == "runner":
                counts["service_runner_calls"] += 1
                self.assertTrue(args[2] if len(args) > 2 else kwargs.get("service_current"))
            return "same-release-noop"

        def checked_watch_activation(*args, **kwargs):
            if monitoring[0] and phase[0] == "runner":
                counts["watch_activation_calls"] += 1
            return None

        def checked_batch_activation(*args, **kwargs):
            if monitoring[0] and phase[0] == "runner":
                counts["batch_activation_calls"] += 1
            return None

        def checked_reconcile(store, duties=None, *, read_context=None, retry_state=None,
                              notice_selection=None, runner_incarnation=None):
            if not runner_store:
                runner_store.append(store)
            if (runner_store[0] is store
                    and monitoring[0] and phase[0] == "runner"):
                counts["passes"] += 1
                counts["passes_after_input" if event else "passes_before_input"] += 1
                if retry_state is not None:
                    counts["reconcile_retry_state_passes"] += 1
            cpu_started = time.process_time()
            try:
                return original_reconcile(
                    store, duties, read_context=read_context, retry_state=retry_state,
                    notice_selection=notice_selection,
                    runner_incarnation=runner_incarnation,
                )
            finally:
                if (runner_store[0] is store
                        and monitoring[0] and phase[0] == "runner"):
                    counts["reconcile_cpu_seconds"] += time.process_time() - cpu_started

        def checked_external_reconcile(*args, **kwargs):
            cpu_started = time.process_time()
            try:
                return original_external_reconcile(*args, **kwargs)
            finally:
                if monitoring[0] and phase[0] == "runner":
                    counts["external_cpu_seconds"] += time.process_time() - cpu_started

        def checked_resume_message(duty, store, *, read_context=None):
            if monitoring[0] and phase[0] == "runner":
                counts["message_resumes"] += 1
            cpu_started = time.process_time()
            try:
                return original_resume_message(duty, store, read_context=read_context)
            finally:
                if monitoring[0] and phase[0] == "runner":
                    counts["message_cpu_seconds"] += time.process_time() - cpu_started

        def checked_resume_registered(duty, store, *, read_context=None):
            if monitoring[0] and phase[0] == "runner":
                counts["registered_resumes"] += 1
            cpu_started = time.process_time()
            try:
                return original_resume_registered(duty, store, read_context=read_context)
            finally:
                if monitoring[0] and phase[0] == "runner":
                    counts["registered_cpu_seconds"] += time.process_time() - cpu_started

        def fake_native_provider(target):
            if monitoring[0] and phase[0] == "runner":
                counts["native_lookups"] += 1
                if event and target == event["target"] and "processed_at" not in event:
                    event["processed_at"] = time.monotonic()
            return ("unknown", {"harness": "opencode", "session_id": "synthetic-changed-session",
                                "name": target, "pane": f"synthetic-pane-{target[-3:]}"},
                    0, "target-identity-changed")

        def checked_path_read_text(path, *args, **kwargs):
            target = Path(path).resolve(strict=False)
            if target == self.jobs.resolve(strict=False):
                assert_fixture_path(target, "jobs.log read")
                if monitoring[0] and phase[0] == "runner":
                    counts["jobs_file_reads"] += 1
            return real_path_read_text(path, *args, **kwargs)

        def checked_os_open(path, flags, *args, **kwargs):
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_flags and monitoring[0]:
                directory_fd = kwargs.get("dir_fd")
                target = assert_fixture_path(path, "write open", dir_fd=directory_fd)
                record_readiness_fd_path("open", target, directory_fd)
                counts["write_open_calls"] += 1
                write_targets.add(str(target))
            return real_os_open(path, flags, *args, **kwargs)

        def checked_replace(source, destination, *args, **kwargs):
            if monitoring[0]:
                src_dir_fd = kwargs.get("src_dir_fd")
                dst_dir_fd = kwargs.get("dst_dir_fd")
                src = assert_fixture_path(source, "atomic write source", dir_fd=src_dir_fd)
                dst = assert_fixture_path(destination, "atomic write destination", dir_fd=dst_dir_fd)
                record_readiness_fd_path("replace", src, src_dir_fd)
                record_readiness_fd_path("replace", dst, dst_dir_fd)
                counts["replace_calls"] += 1
                write_targets.update((str(src), str(dst)))
            return real_os_replace(source, destination, *args, **kwargs)

        def checked_unlink(path, *args, **kwargs):
            if monitoring[0]:
                directory_fd = kwargs.get("dir_fd")
                target = assert_fixture_path(path, "write cleanup target", dir_fd=directory_fd)
                record_readiness_fd_path("unlink", target, directory_fd)
                counts["unlink_calls"] += 1
                write_targets.add(str(target))
            return real_os_unlink(path, *args, **kwargs)

        def checked_save_pending(row):
            assert_fixture_path(peer_message._pending_path(row["ref"]), "pending-row write")
            if monitoring[0] and phase[0] == "input":
                counts["input_pending_writes"] += 1
            return original_save_pending(row)

        def checked_prepare_message(*args, **kwargs):
            result = original_prepare_message(*args, **kwargs)
            assert_fixture_path(peer_message._transfer_path(result[1]), "transfer-row write")
            if monitoring[0] and phase[0] == "input":
                counts["input_transfer_writes"] += 1
            return result

        def checked_kill(pid, sig):
            if sig != 0:
                raise AssertionError(f"operational signal prohibited in fixture: {pid} {sig}")
            return previous_kill(pid, sig)

        class MeasurementWindowEnded(Exception):
            pass

        lock_path = self.store.root / peer_obligations.RUNNER_LOCK_NAME
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner_args = SimpleNamespace(state_root=str(self.dispatch), lock_fd=lock_fd,
                                      service_current="fixture-managed-current")

        def process_ticks():
            raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
            pid = int(raw[:raw.find(" (")])
            return pid, raw[raw.rfind(")") + 2:].split()

        def close_measurement_window(name):
            ended = time.monotonic()
            end_pid, after_fields = process_ticks()
            record = measurement[name]
            before_fields = record["before_fields"]
            record.update({
                "end_monotonic": ended,
                "elapsed": ended - record["start_monotonic"],
                "end_pid": end_pid,
                "after_fields": after_fields,
                "cpu_ticks": ((int(after_fields[11]) + int(after_fields[12]))
                              - (int(before_fields[11]) + int(before_fields[12]))),
                "registry": dispatch_registry_cache.metrics_snapshot(),
                "store": runner_store[0].discovery_metrics() if runner_store else {},
                "counts": dict(counts),
            })
            if name == "cold":
                steady_pid, steady_fields = end_pid, after_fields
                measurement["steady"] = {
                    "start_monotonic": ended,
                    "start_pid": steady_pid,
                    "before_fields": steady_fields,
                }
                window_phase[0] = "steady"
            else:
                window_phase[0] = "post-change"

        def controlled_sleep(delay):
            requested = max(0.0, float(delay))
            sleep_delays.append(requested)
            measured_phase = window_phase[0]
            if measured_phase in {"cold", "steady"}:
                record = measurement[measured_phase]
                deadline = record["start_monotonic"] + window
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    real_sleep(min(requested, remaining))
                if time.monotonic() >= deadline:
                    close_measurement_window(measured_phase)
                return
            if measured_phase == "post-change" and not event:
                first_wait = min(requested * 0.25, requested)
                if first_wait:
                    real_sleep(first_wait)
                phase[0] = "input"
                if recurring_negative:
                    delivery_input = self._create_open_retire_delivery(0)
                    retire_delivery_input.update(delivery_input)
                    retire_delivery_input.update({
                        "duty_id": self.completed_retire_ids[0],
                        "created_at": time.monotonic(),
                        "pass_at_create": counts["passes"],
                        "trigger_delay": max(0.0, requested - first_wait),
                    })
                duty_id, ref, target = self._message(193)
                event.update({"duty_id": duty_id, "ref": ref, "target": target,
                              "created_at": time.monotonic(), "pass_at_create": counts["passes"],
                              "trigger_delay": max(0.0, requested - first_wait)})
                phase[0] = "runner"
                remaining = max(0.0, requested - first_wait)
                if remaining:
                    real_sleep(remaining)
                return
            if event.get("processed_at"):
                measurement["final_pid"], measurement["final_fields"] = process_ticks()
                raise MeasurementWindowEnded()
            if requested:
                real_sleep(requested)

        def controlled_select(readers, writers, exceptional, timeout=None):
            controlled_sleep(0.0 if timeout is None else timeout)
            return [], [], []

        ticks = os.sysconf("SC_CLK_TCK")
        monitoring[0] = True
        phase[0] = "runner"
        runner_ended_at_window = False
        runner_return = None
        try:
            with ExitStack() as patch_stack:
                patchers = (
                    mock.patch.object(peer_message, "steward_marker_roots", side_effect=checked_roots),
                    mock.patch.object(peer_message, "peer_state_root", side_effect=checked_state_root),
                    mock.patch.object(peer_steward, "_watch_root", side_effect=checked_watch_root),
                    mock.patch.object(dispatch_batch_obligations, "_jobs_paths",
                                      side_effect=checked_jobs_paths),
                    mock.patch.object(dispatch_batch_obligations, "_observer_root",
                                      side_effect=checked_observer_root),
                    mock.patch.object(peer_obligations, "obligations_root",
                                      side_effect=checked_obligations_root),
                    mock.patch.object(peer_message, "_read_pending", side_effect=checked_pending),
                    mock.patch.object(peer_message, "_read_transfer_record", side_effect=checked_transfer),
                    mock.patch.object(peer_message, "_save_pending", side_effect=checked_save_pending),
                    mock.patch.object(peer_message, "_prepare_peer_message",
                                      side_effect=checked_prepare_message),
                    mock.patch.object(peer_obligations, "_write_atomic", side_effect=checked_write),
                    mock.patch.object(peer_obligations, "_read_record", side_effect=checked_read_record),
                    mock.patch.object(peer_obligations.ObligationStore, "list", checked_store_list),
                    mock.patch.object(peer_obligations.ObligationStore, "update", checked_store_update),
                    mock.patch.object(peer_obligations.ObligationStore, "update_message_checkpoint",
                                      checked_message_checkpoint),
                    mock.patch.object(peer_steward, "_retire_handover", side_effect=checked_retire_handover),
                    mock.patch.object(route_authority, "decide_continuation", side_effect=checked_authority),
                    mock.patch.object(dispatch_seat_handover, "record_retire_handover",
                                      side_effect=checked_record_retire),
                    mock.patch.object(dispatch_seat_handover, "binding_mutexes",
                                      side_effect=checked_binding_mutexes),
                    mock.patch.object(fcntl, "flock", side_effect=checked_flock),
                    mock.patch.object(dispatch_registry_cache, "registry_lines",
                                      side_effect=checked_registry_lines),
                    mock.patch.object(dispatch_completion_join, "registry_lines",
                                      side_effect=checked_registry_lines),
                    mock.patch.object(dispatch_completion_join, "current_attempt_row",
                                      side_effect=checked_attempt_lookup),
                    mock.patch.object(PeerReadContext, "current_attempt_row", checked_context_lookup),
                    mock.patch.object(peer_steward, "_reconcile_obligations",
                                      side_effect=checked_reconcile),
                    mock.patch.object(peer_steward, "_reconcile_external_obligations",
                                      side_effect=checked_external_reconcile),
                    mock.patch.object(peer_steward, "_resume_message_obligation",
                                      side_effect=checked_resume_message),
                    mock.patch.object(peer_steward, "_resume_registered_obligation",
                                      side_effect=checked_resume_registered),
                    mock.patch.object(peer_steward.herdr_observation, "pane_census",
                                      return_value=None),
                    mock.patch.object(peer_steward, "_retire_target",
                                      side_effect=fake_native_provider),
                    mock.patch.object(Path, "read_text", checked_path_read_text),
                    mock.patch.object(os, "open", side_effect=checked_os_open),
                    mock.patch.object(os, "replace", side_effect=checked_replace),
                    mock.patch.object(os, "unlink", side_effect=checked_unlink),
                    mock.patch.object(os, "kill", side_effect=checked_kill),
                    mock.patch.object(peer_steward.select, "select", side_effect=controlled_select),
                    mock.patch.object(peer_steward.time, "sleep", side_effect=controlled_sleep),
                    mock.patch.object(peer_message, "deliver_pending_codex",
                                      side_effect=AssertionError("fixture attempted a native send")),
                    mock.patch.object(peer_steward, "cmd_prompt",
                                      side_effect=AssertionError("fixture attempted a prompt send")),
                    mock.patch.object(peer_obligations, "acknowledge_registered_delivery",
                                      side_effect=AssertionError("fixture attempted an ACK")),
                    mock.patch.object(peer_obligations, "ensure_normal_runner",
                                      side_effect=AssertionError("fixture attempted service activation")),
                    mock.patch.object(peer_obligations, "ensure_runner",
                                      side_effect=AssertionError("fixture attempted service activation")),
                    mock.patch.object(peer_obligations, "exec_service_runner",
                                      side_effect=checked_exec_service_runner),
                    mock.patch.object(peer_steward, "_ensure_watch_observers",
                                      side_effect=checked_watch_activation),
                    mock.patch.object(dispatch_batch_obligations, "ensure_observers",
                                      side_effect=checked_batch_activation),
                    mock.patch.object(peer_steward.subprocess, "run",
                                      side_effect=AssertionError("fixture attempted external transport")),
                )
                for patcher in patchers:
                    patch_stack.enter_context(patcher)
                dispatch_registry_cache.clear_cache()
                dispatch_registry_cache.reset_metrics()
                started = time.monotonic()
                start_pid, start_fields = process_ticks()
                measurement["cold"] = {"start_monotonic": started, "start_pid": start_pid,
                                        "before_fields": start_fields}
                window_phase[0] = "cold"
                try:
                    runner_return = peer_steward.cmd_obligation_runner(runner_args)
                except MeasurementWindowEnded:
                    runner_ended_at_window = True
        finally:
            monitoring[0] = False
            phase[0] = "finished"
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertTrue(runner_ended_at_window, f"runner returned early with {runner_return!r}")
        self.assertIn("steady", measurement)
        cold = measurement["cold"]
        steady = measurement["steady"]
        cold_cpu = cold["cpu_ticks"] * 100.0 / (ticks * cold["elapsed"])
        steady_cpu = steady["cpu_ticks"] * 100.0 / (ticks * steady["elapsed"])
        self.assertEqual(start_pid, os.getpid())
        self.assertEqual(cold["end_pid"], start_pid)
        self.assertEqual(steady["start_pid"], start_pid)
        self.assertEqual(steady["end_pid"], start_pid)
        self.assertEqual(measurement["final_pid"], start_pid)
        self.assertEqual(cold["before_fields"][19], cold["after_fields"][19])
        self.assertEqual(steady["before_fields"][19], steady["after_fields"][19])
        self.assertEqual(measurement["final_fields"][19], cold["before_fields"][19])
        for observed in (cold["elapsed"], steady["elapsed"]):
            self.assertGreaterEqual(observed, 19.0)
            self.assertLessEqual(observed, 21.0)

        self.assertEqual(cold["registry"]["content_reads"], 1)
        self.assertEqual(cold["registry"]["bytes_read"], self.jobs.stat().st_size)
        self.assertEqual(cold["registry"]["full_metadata_parses"], 1)
        self.assertGreater(cold["registry"]["metadata_rows_parsed"], 2)
        self.assertEqual(steady["registry"]["content_reads"] - cold["registry"]["content_reads"], 0)
        self.assertEqual(steady["registry"]["bytes_read"] - cold["registry"]["bytes_read"], 0)
        self.assertEqual(steady["registry"]["full_metadata_parses"]
                         - cold["registry"]["full_metadata_parses"], 0)
        self.assertEqual(steady["registry"]["metadata_rows_parsed"]
                         - cold["registry"]["metadata_rows_parsed"], 0)
        self.assertTrue(main_active_discovery_ids,
                        "the main runner pass must expose its active-index target")
        discovered_active_ids = set(main_active_discovery_ids[0])
        self.assertEqual(discovered_active_ids, expected_cold_active_ids,
                         "cold discovery must match fixture membership and the case-required "
                         "retire followups")
        expected_cold_json_records = len(expected_cold_active_ids)
        self.assertEqual(cold["store"]["json_reads"], expected_cold_json_records,
                         (len(expected_cold_active_ids), cold["store"]))
        self.assertEqual(cold["store"]["json_parses"], expected_cold_json_records)
        self.assertEqual(steady["store"]["json_reads"] - cold["store"]["json_reads"], 0)
        self.assertEqual(steady["store"]["json_parses"] - cold["store"]["json_parses"], 0)
        self.assertGreater(steady["store"]["stat_calls"] - cold["store"]["stat_calls"], 0)
        self.assertGreaterEqual(counts["passes"], 4)
        self.assertEqual(counts["reconcile_retry_state_passes"], counts["passes"])
        self.assertGreaterEqual(counts["passes_before_input"], 4)
        self.assertGreater(counts["passes_after_input"], 0)
        self.assertLessEqual(counts["message_resumes"], 194 * counts["passes"])
        self.assertGreaterEqual(counts["registered_resumes"], 6 * counts["passes"])
        self.assertLessEqual(counts["pending_reads"], 194 * counts["passes"])
        # Five duties deliberately lack pending rows, so each pass also checks
        # their immutable transfer source before retaining "pending-row-missing".
        self.assertLessEqual(counts["transfer_record_reads"], 194 * counts["passes"])
        self.assertLessEqual(counts["attempt_row_lookups"], 8 * counts["passes"])
        self.assertLessEqual(counts["context_attempt_row_lookups"], 7 * counts["passes"])
        self.assertGreaterEqual(counts["completed_retire_recoveries"], 2)
        self.assertGreaterEqual(counts["record_retire_calls"], 2)
        self.assertGreaterEqual(counts["handover_authority_calls"], 1 if recurring_negative else 2)
        self.assertEqual(counts["service_runner_calls"], counts["passes"])
        self.assertEqual(counts["batch_activation_calls"], counts["passes"])
        self.assertEqual(counts["watch_activation_calls"], 0)
        self.assertEqual(counts["jobs_file_reads"], 0,
                         "registry content is read through the fd generation reader")
        self.assertEqual(counts["write_atomic_without_dir_fd"], 0)
        self.assertEqual(
            cold["counts"]["obligation_writes"]
            - cold["counts"]["retire_followup_completion_writes"], 0,
            "the first resolved retire followups are counted separately from unchanged duties",
        )
        self.assertEqual(
            (steady["counts"]["obligation_writes"]
             - steady["counts"]["retire_followup_completion_writes"])
            - (cold["counts"]["obligation_writes"]
               - cold["counts"]["retire_followup_completion_writes"]),
            0,
            "steady unchanged duties must not checkpoint",
        )
        self.assertEqual(
            counts["obligation_writes"] - counts["retire_followup_completion_writes"], 1,
                         "only the changed duty may checkpoint after the measurement windows")
        self.assertEqual(counts["input_obligation_writes"], 1)
        self.assertEqual(counts["input_pending_writes"], 1)
        self.assertEqual(counts["input_transfer_writes"], 1)
        self.assertEqual(
            counts["store_update_calls"] - counts["retire_followup_completion_writes"], 1)
        self.assertTrue(discovered_roots)
        self.assertTrue(write_targets)
        self.assertTrue(all(Path(path).resolve(strict=False).is_relative_to(fixture_root)
                            for path in discovered_roots | write_targets | observed_roots))
        readiness_replaces = [entry for entry in fd_readiness_events
                              if entry["operation"] == "replace"]
        readiness_temporary = next(
            (entry for entry in readiness_replaces
             if Path(entry["path"]).name.startswith(".readiness-")
             and Path(entry["path"]).name.endswith(".tmp")), None)
        readiness_final = next(
            (entry for entry in readiness_replaces
             if Path(entry["path"]).name == "runner.json"), None)
        self.assertIsNotNone(readiness_temporary,
                             "the real runner incarnation temporary must publish through dir_fd")
        self.assertIsNotNone(readiness_final,
                             "the real runner incarnation target must publish through dir_fd")
        self.assertEqual(Path(readiness_temporary["path"]).parent,
                         Path(readiness_final["path"]).parent)
        self.assertEqual((readiness_temporary["directory_dev"],
                          readiness_temporary["directory_ino"]),
                         (readiness_final["directory_dev"],
                          readiness_final["directory_ino"]))
        print("readiness_fd_publish_provenance=" + json.dumps({
            "temporary_path": readiness_temporary["path"],
            "final_path": readiness_final["path"],
            "directory_dev": readiness_final["directory_dev"],
            "directory_ino": readiness_final["directory_ino"],
            "operation": "replace(src_dir_fd,dst_dir_fd)",
        }, sort_keys=True, separators=(",", ":")))
        self.assertGreaterEqual(event["pass_at_create"], 4)
        self.assertIn("processed_at", event)
        input_latency = event["processed_at"] - event["created_at"]
        self.assertLessEqual(input_latency, event["trigger_delay"] + 2.0)
        self.assertLessEqual(event["trigger_delay"], 15.0)
        self.assertEqual(self.store.get(event["duty_id"])["state"], "unknown")
        self.assertEqual(self.store.get(event["duty_id"])["observation"]["reason"],
                         "target-identity-changed")
        self.assertEqual(hashlib.sha256(self.jobs.read_bytes()).hexdigest(), initial_jobs_digest,
                         "authority input changes must leave jobs.log unchanged")
        if recurring_negative:
            target_meta, target_successor = self.retire_handover_meta[0]
            other_meta, _other_successor = self.retire_handover_meta[1]
            for meta in (target_meta, other_meta):
                for measured_window in ("cold", "steady"):
                    self.assertGreaterEqual(lineage_unknown_counts.get((meta["parent_sid"], measured_window), 0),
                        2, f"{meta['attempt_id']} must reobserve the same unreadable lineage on multiple {measured_window} passes")
            for duty_id in (self.completed_retire_ids[0], self.completed_retire_ids[1]):
                for measured_window in ("cold", "steady"):
                    resumed = [row for row in retire_recovery_events
                               if row["duty_id"] == duty_id and row["window"] == measured_window]
                    self.assertGreaterEqual(len(resumed), 2, (duty_id, measured_window))
            self.assertEqual(authority_counts.get(target_meta["attempt_id"], {}).get(
                "post-change_allowed", 0), 1, authority_counts)
            self.assertEqual(authority_counts.get(other_meta["attempt_id"], {}).get(
                "post-change_allowed", 0), 0, authority_counts)
            self.assertGreater(retire_delivery_input.get("observed_pass", 0),
                               retire_delivery_input.get("pass_at_create", -1))
            self.assertEqual(retire_delivery_input.get("observed_window"), "post-change")
            delivery = dispatch_pending_delivery.read(
                self.dispatch, retire_delivery_input["recipient_key"],
                retire_delivery_input["delivery_id"])
            self.assertEqual(delivery["state"], "pending")
            self.assertEqual(delivery["attempt_ids"], [target_meta["attempt_id"]])
            self.assertIsNone(delivery["claim_owner"], "the fixture must not claim the delivery")
            self.assertEqual(delivery["parent_attempt_id"], target_meta["attempt_id"])
            original_after = self.store.get(retire_delivery_input["duty_id"])
            self.assertEqual(original_after["id"], original_retire["id"])
            self.assertEqual(original_after["state"], "complete")
            self.assertEqual(original_after["accepted_at"], original_retire["accepted_at"])
            self.assertEqual(original_after["intent"], original_retire["intent"])
            self.assertEqual(original_after["intent"]["requester"], {
                "server": "default", "pane": "w1:pNew0", "harness": "codex",
                "session_id": target_successor,
            })
            self.assertEqual(delivery["receipt"]["job_registry"], str(self.jobs))
            self.assertEqual(delivery["receipt"]["children"][0]["attempt_id"],
                             target_meta["attempt_id"])
            self.assertTrue(peer_steward._seat_successor_path("w1:pNew1").exists(),
                            "the still-ineligible duty keeps its original successor mark")
            self.assertEqual(dispatch_seat_handover.effective_parent(other_meta, self.jobs), "")
        else:
            self.assertEqual(counts["completed_retire_recoveries"], 2,
                             f"authority={authority_counts}; retire={retire_recovery_events}; "
                             f"record={record_retire_events}; locks={lock_events}")
            self.assertEqual(counts["record_retire_calls"], 2)
            self.assertEqual(counts["handover_authority_calls"], 2)
        self.assertTrue(all(self.store.get(duty_id)["state"] == "unknown"
                            for duty_id, _ref, _target in self.message_duties))
        expected_final_active_ids = expected_cold_active_ids | {event["duty_id"]}
        for index, duty_id in enumerate(self.completed_retire_ids):
            completed_followup = not recurring_negative or index == 0
            retire_observation = self.store.get(duty_id)["observation"]
            if completed_followup:
                self.assertEqual(retire_observation.get("terminal_followup"), "complete")
                expected_final_active_ids.discard(duty_id)
            else:
                self.assertIsNone(retire_observation.get("terminal_followup"),
                                 "the still-ineligible fixture duty keeps its seeded state")
                expected_final_active_ids.add(duty_id)
        self.assertEqual(
            {row["id"] for row in self.store.list(fast_active_only=True)},
            expected_final_active_ids,
            "the post-change handover must update the final active index exactly",
        )
        final_registered = [self.store.get(duty_id)["state"] for duty_id in registered_ids]
        self.assertEqual(final_registered.count("unknown"), 5)
        self.assertEqual(final_registered.count("pending"), 1)
        notice_row = peer_message._read_pending(self.message_duties[187][1])
        self.assertEqual(notice_row["state"], "unverified")
        self.assertEqual(sum(bool(row.get("dispatch_notice")) for row in initial_rows), 1)

        successor_locks = [key for key in lock_identities if key.startswith("peer-successor-")]
        binding_locks = [key for key in lock_identities if key.startswith("lineage-binding-")]
        seat_locks = [key for key in lock_identities
                      if key != "lineage-write" and not key.startswith(("peer-successor-", "lineage-binding-", "lineage-source-"))]
        self.assertEqual(binding_observer_errors, [],
                         f"binding lock test observer raised: {binding_observer_errors}")
        self.assertEqual(len(set(successor_locks)), 2, lock_identities)
        self.assertEqual(len(set(seat_locks)), 2, lock_identities)
        coordination_locks = {key for key in lock_identities
                              if key == "lineage-write" or key.startswith("lineage-source-")}
        expected_coordination_locks = {"lineage-write"} | {
            "lineage-source-" + hashlib.sha256(json.dumps(["claude", meta["parent_sid"]]).encode()).hexdigest()
            for meta, _sid in self.retire_handover_meta}
        self.assertEqual(coordination_locks, expected_coordination_locks)
        observed_binding_locks = set(binding_locks)
        expected_binding_lock_set = set().union(*expected_binding_locks.values())
        self.assertEqual(observed_binding_locks, expected_binding_lock_set,
                         f"logical binding locks must exactly match admitted operations: "
                         f"observed={observed_binding_locks}; expected={expected_binding_lock_set}")
        self.assertEqual(len(set(lock_identities)),
                         4 + len(expected_binding_lock_set) + len(expected_coordination_locks), lock_identities)
        for measured_window in expected_binding_locks:
            seen = {row["key"] for row in lock_events if row["window"] == measured_window}
            seen_bindings = {key for key in seen if key.startswith("lineage-binding-")}
            self.assertEqual(seen_bindings, expected_binding_locks[measured_window],
                             f"{measured_window} logical lock identities must match "
                             f"admitted bindings: seen={seen_bindings}; "
                             f"expected={expected_binding_locks[measured_window]}")
        if recurring_negative:
            expected_physical_locks = set(seat_locks) | set(successor_locks)
            self.assertEqual(len(expected_physical_locks), 4, lock_identities)
            for measured_window in ("cold", "steady"):
                seen = {row["key"] for row in lock_events
                        if row["window"] == measured_window}
                seen_physical = {key for key in seen
                                 if key != "lineage-write" and not key.startswith(("lineage-binding-", "lineage-source-"))}
                self.assertEqual(
                    seen_physical, expected_physical_locks,
                    f"all four physical seat/successor lock identities must be entered "
                    f"in {measured_window}: seen={seen_physical}; "
                    f"expected={expected_physical_locks}")
            self.assertGreaterEqual(
                sum(row["window"] == "steady" for row in record_retire_events), 4,
                "steady negative discovery must enter the real retire handover path repeatedly")
        for (meta, successor_sid), index in zip(self.retire_handover_meta, range(2)):
            pane = f"w1:pNew{index}"
            seat = session_tidy._pane_seat_of(pane)
            snapshot = dispatch_seat_handover.read_snapshot(seat.key)
            handed_over = not recurring_negative or index == 0
            if handed_over:
                self.assertEqual(snapshot["from"]["sid"], successor_sid)
                self.assertTrue(any(binding.get("attempt") == meta["attempt_id"]
                                    for binding in snapshot["bindings"]))
                ledger = dispatch_seat_handover.handover_rows(seat)
                self.assertTrue(any(session_tidy._handover_origin(row)[1] == meta["parent_sid"]
                                    and row.get("sid") == successor_sid
                                    and any(item.get("attempt") == meta["attempt_id"]
                                            for item in row.get("bindings", []))
                                    for row in ledger))
                self.assertEqual(dispatch_seat_handover.effective_parent(meta, self.jobs), successor_sid)
                self.assertFalse(peer_steward._seat_successor_path(pane).exists())
            else:
                self.assertIsNone(snapshot)
                self.assertEqual(dispatch_seat_handover.effective_parent(meta, self.jobs), "")
                self.assertTrue(peer_steward._seat_successor_path(pane).exists())
        self.assertEqual(self.jobs.stat().st_size, 23_499_871)
        denied_counts = {
            name: sum(result_counts.get(f"{name}_denied", 0)
                      for result_counts in authority_counts.values())
            for name in ("cold", "steady")
        }
        successful_handover_count = 2 if not recurring_negative else 1
        case_name = "recurring-negative-retire" if recurring_negative else "successful-handover"
        cpu_recommendation = "met" if steady_cpu <= 1.0 else "exceeded"
        print(
            f"real-runner-workload-correction case={case_name} "
            f"runner_pid={start_pid} starttime={cold['before_fields'][19]} HZ={ticks} "
            "one_core=100% child_cpu=excluded(no child spawned) "
            f"registry_bytes={self.jobs.stat().st_size} obligations={fixture_record_count} "
            f"active_obligations={len(expected_active_ids)} "
            f"measured_active_obligations={len(discovered_active_ids)} readable_transfers=188 "
            f"successor_locks={len(set(successor_locks))} seat_locks={len(set(seat_locks))} "
            f"binding_locks={len(observed_binding_locks)} "
            f"completed_retire_handover={successful_handover_count} "
            f"cold_window={cold['elapsed']:.2f}s cold_cpu={cold_cpu:.3f}% "
            f"steady_window={steady['elapsed']:.2f}s steady_cpu={steady_cpu:.3f}% "
            f"steady_cpu_recommendation={cpu_recommendation} "
            f"cold_cpu_ticks={cold['cpu_ticks']} steady_cpu_ticks={steady['cpu_ticks']} "
            f"cold_jobs_reads={cold['registry']['content_reads']} cold_jobs_bytes={cold['registry']['bytes_read']} "
            f"cold_metadata_parses={cold['registry']['full_metadata_parses']} "
            f"cold_metadata_rows_parsed={cold['registry']['metadata_rows_parsed']} "
            f"steady_jobs_read_delta={steady['registry']['content_reads'] - cold['registry']['content_reads']} "
            f"steady_jobs_byte_delta={steady['registry']['bytes_read'] - cold['registry']['bytes_read']} "
            f"steady_metadata_parse_delta={steady['registry']['full_metadata_parses'] - cold['registry']['full_metadata_parses']} "
            f"steady_metadata_rows_parse_delta={steady['registry']['metadata_rows_parsed'] - cold['registry']['metadata_rows_parsed']} "
            f"cold_duty_json_reads={cold['store']['json_reads']} cold_duty_json_parses={cold['store']['json_parses']} "
            f"steady_duty_json_read_delta={steady['store']['json_reads'] - cold['store']['json_reads']} "
            f"steady_duty_json_parse_delta={steady['store']['json_parses'] - cold['store']['json_parses']} "
            f"steady_duty_stat_delta={steady['store']['stat_calls'] - cold['store']['stat_calls']} "
            f"passes={counts['passes']} cold_passes={cold['counts']['passes']} "
            f"steady_passes={steady['counts']['passes'] - cold['counts']['passes']} requested_sleeps={sleep_delays} "
            f"message_cpu={counts['message_cpu_seconds']:.4f}s registered_cpu={counts['registered_cpu_seconds']:.4f}s "
            f"list_cpu={counts['store_list_cpu_seconds']:.4f}s reconcile_cpu={counts['reconcile_cpu_seconds']:.4f}s "
            f"external_cpu={counts['external_cpu_seconds']:.4f}s path_guard_cpu={counts['fixture_path_guard_cpu_seconds']:.4f}s "
            f"store_list_calls={counts['store_list_calls']} store_record_reads={counts['store_record_reads']} "
            f"message_resumes={counts['message_resumes']} registered_resumes={counts['registered_resumes']} "
            f"pending_reads={counts['pending_reads']} transfer_record_reads={counts['transfer_record_reads']} "
            f"native_lookups_fake={counts['native_lookups']} root_discoveries={counts['root_discoveries']} "
            f"root_candidates={counts['root_candidates']} attempt_row_lookups={counts['attempt_row_lookups']} "
            f"context_attempt_lookups={counts['context_attempt_row_lookups']} "
            f"registry_line_calls={counts['registry_line_calls']} fd_registry_reads={steady['registry']['content_reads']} "
            f"store_update_calls={counts['store_update_calls']} "
            f"completed_retire_recoveries={counts['completed_retire_recoveries']} "
            f"record_retire_calls={counts['record_retire_calls']} "
            f"handover_authority_calls={counts['handover_authority_calls']} "
            f"negative_authority_denials_cold={denied_counts['cold']} "
            f"negative_authority_denials_steady={denied_counts['steady']} "
            f"lineage_unknown_cold={sum(count for (_sid, window_name), count in lineage_unknown_counts.items() if window_name == 'cold')} "
            f"lineage_unknown_steady={sum(count for (_sid, window_name), count in lineage_unknown_counts.items() if window_name == 'steady')} "
            f"retire_delivery_authority_change={'yes' if retire_delivery_input else 'no'} "
            f"retire_delivery_authority_seen_pass={retire_delivery_input.get('observed_pass', 'n/a')} "
            f"retire_delivery_claim_owner={((retire_delivery_input.get('record') or {}).get('claim_owner') if retire_delivery_input else 'n/a')} "
            f"lock_acquisitions_cold={sum(row['window'] == 'cold' for row in lock_events)} "
            f"lock_acquisitions_steady={sum(row['window'] == 'steady' for row in lock_events)} "
            f"lock_identities={sorted(set(lock_identities))} "
            f"unchanged_checkpoint_skips={counts['message_resumes'] - (counts['store_update_calls'] - counts['retire_followup_completion_writes'])} "
            f"input_obligation_writes={counts['input_obligation_writes']} "
            f"runner_obligation_writes={counts['obligation_writes']} "
            f"retire_followup_completion_writes={counts['retire_followup_completion_writes']} "
            f"write_atomic_with_dir_fd={counts['write_atomic_with_dir_fd']} "
            f"write_atomic_without_dir_fd={counts['write_atomic_without_dir_fd']} "
            f"reconcile_retry_state_passes={counts['reconcile_retry_state_passes']} "
            f"input_pending_writes={counts['input_pending_writes']} "
            f"input_transfer_writes={counts['input_transfer_writes']} "
            f"write_open_calls={counts['write_open_calls']} replace_calls={counts['replace_calls']} "
            f"unlink_calls={counts['unlink_calls']} fixture_paths={len(observed_roots)} "
            f"event_resume={input_latency:.3f}s event_wait_remaining={event['trigger_delay']:.3f}s "
            f"post_change_cpu_ticks={int(measurement['final_fields'][11]) + int(measurement['final_fields'][12]) - (int(steady['after_fields'][11]) + int(steady['after_fields'][12]))} "
            "fake_provider=target-identity-changed; real holder /proc check; native transport disabled"
        )

if __name__ == "__main__":
    unittest.main()
