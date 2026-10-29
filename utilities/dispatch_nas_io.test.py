#!/usr/bin/env python3
"""Finite launch/registration ordering checks; never touch live jobs or NAS.

Run under the existing verification-background-lease for descendant ownership.
The 0.4s source-fence injection matches the recorded private baseline; events,
not a timing ratio, establish independent project progress.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import dispatch_contract as DC
import review_source_subject as SUBJECT

CURRENT = ("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
           "execution_surface=registered-headless,registered_worker=1,"
           "fallback_hop=same-harness-headless")


class NasDispatchIOTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="nas-io-")
        self.base = Path(self.scratch.name)
        self.jobs = self.base / "jobs.log"
        self.children = []
        self.threads = []
        self.release_events = []
        self.env = mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.finish)

    def finish(self):
        for event in self.release_events:
            event.set()
        for thread in self.threads:
            thread.join(10)
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        # Do not retire diagnostics under a live/unreadable owned descendant.
        self.assertTrue(all(not t.is_alive() for t in self.threads))
        self.scratch.cleanup()

    def row(self, attempt, project="A", extra=""):
        return (f"fixture\topen\t/{project}/repo\t/{project}/wt\t{attempt}\t"
                f"{CURRENT},harness=codex,capability=code-plan,attempt_id={attempt}{extra}")

    def register(self, attempt, project="A", extra=""):
        row = self.row(attempt, project, extra)
        self.assertTrue(DC.claim_attempt_row(self.jobs, attempt, row))
        return row

    def spawn(self, marker):
        def start(gate_fd):
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("launch-fence.py")),
                 "--parent-pid", str(os.getpid()), "--gate-fd", str(gate_fd), "--",
                 sys.executable, "-c", "from pathlib import Path; "
                 f"Path({str(marker)!r}).write_text('released')"],
                pass_fds=(gate_fd,), start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.children.append(proc)
            return proc
        return start

    def assert_global_locks_free(self):
        for suffix in (".lock", ".launch.lock"):
            with Path(f"{self.jobs}{suffix}").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def async_call(self, callback):
        done, result = threading.Event(), {}
        def run():
            try:
                result["value"] = callback()
            except BaseException as exc:
                result["error"] = exc
            finally:
                done.set()
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return done, result

    def paused_source(self, attempt, original):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        def callback(jobs, fields, metadata, **kwargs):
            if metadata.get("attempt_id") == attempt:
                self.assert_global_locks_free()
                entered.set()
                self.assertTrue(release.wait(5), "test barrier was not released")
                time.sleep(0.4)
            return original(jobs, fields, metadata, **kwargs)
        return entered, release, callback

    def test_source_wait_allows_independent_registration_and_terminal_completion(self):
        self.register("att-terminal-B", "B")
        entered, release, callback = self.paused_source(
            "att-A", DC._review_source_registration_fence)
        with mock.patch.object(DC, "_review_source_registration_fence", side_effect=callback):
            done_a, result_a = self.async_call(
                lambda: DC.claim_attempt_row(self.jobs, "att-A", self.row("att-A")))
            try:
                self.assertTrue(entered.wait(5))
                def project_b():
                    started = time.monotonic()
                    self.register("att-register-B", "B")
                    registered = time.monotonic()
                    closed = DC.close_attempt_row(self.jobs, "att-terminal-B", "completed-marker")
                    return closed, registered - started, time.monotonic() - registered
                done_b, result_b = self.async_call(project_b)
                self.assertTrue(done_b.wait(3), "B serialized behind A source validation")
                self.assertNotIn("error", result_b)
                self.assertTrue(result_b["value"][0])
                print(f"private after: B register={result_b['value'][1]:.6f}s "
                      f"terminal={result_b['value'][2]:.6f}s; A still source-fenced")
                self.assertFalse(done_a.is_set())
            finally:
                release.set()
            self.assertTrue(done_a.wait(5))
            self.assertNotIn("error", result_a)
            self.assertTrue(result_a["value"])
        print("ordering: A-source-enter < B-register+terminal-done < A-release; delay=0.4s")

    def test_source_wait_allows_independent_spawn_and_observers_own_no_global_lock(self):
        self.register("att-A")
        self.register("att-B", "B")
        entered, release, callback = self.paused_source("att-A", SUBJECT.validate_attempt)
        with mock.patch.object(SUBJECT, "validate_attempt", side_effect=callback):
            done_a, result_a = self.async_call(lambda: DC.spawn_claimed_attempt(
                self.jobs, "att-A", parent_binding=None, spawn=self.spawn(self.base / "A"),
                pre_release=lambda _identity: self.assert_global_locks_free(),
                post_claim=lambda _identity: self.assert_global_locks_free()))
            try:
                self.assertTrue(entered.wait(5))
                done_b, result_b = self.async_call(lambda: DC.spawn_claimed_attempt(
                    self.jobs, "att-B", parent_binding=None, spawn=self.spawn(self.base / "B")))
                self.assertTrue(done_b.wait(3), "B spawn serialized behind A external preparation")
                self.assertNotIn("error", result_b)
                result_b["value"][0].wait(timeout=5)
                self.assertTrue((self.base / "B").exists())
                self.assertFalse(done_a.is_set())
            finally:
                release.set()
            self.assertTrue(done_a.wait(5))
            self.assertNotIn("error", result_a)
            result_a["value"][0].wait(timeout=5)
        print("ordering: A-source-enter < B-spawn+payload-done < A-release")

    def test_normal_control_and_same_attempt_concurrent_start_release_once(self):
        self.register("att-race")
        barrier = threading.Barrier(2)
        marker = self.base / "winner"
        def launch():
            barrier.wait(timeout=5)
            return DC.spawn_claimed_attempt(self.jobs, "att-race", parent_binding=None,
                                            spawn=self.spawn(marker))
        attempts = [self.async_call(launch) for _ in range(2)]
        for done, _result in attempts:
            self.assertTrue(done.wait(10))
        winners = [result["value"] for _done, result in attempts if "value" in result]
        errors = [result["error"] for _done, result in attempts if "error" in result]
        self.assertEqual(len(winners), 1)
        self.assertEqual([e.reason for e in errors], ["attempt-launch-already-claimed"])
        self.assertEqual(len(self.children), 1)
        winners[0][0].wait(timeout=5)
        self.assertTrue(marker.exists())

    def test_parent_liveness_is_fresh_after_both_registry_lock_waits(self):
        spec = importlib.util.spec_from_file_location(
            "nas_parent_fixture", Path(__file__).with_name("dispatch_contract.test.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        case = module.DispatchContractTest()
        for commit in (1, 2):
            for change in ("death", "pid-reuse", "unknown", "lease-return"):
                with self.subTest(commit=commit, change=change), tempfile.TemporaryDirectory() as td:
                    suffix = f"lock-wait-{commit}-{change}"
                    parent, jobs, binding = case._fenced_spawn(td, suffix)
                    self.children.append(parent)
                    holder = None
                    namespace = mock.patch.object(DC, "process_namespace_identity", wraps=DC.process_namespace_identity)
                    if change == "lease-return":
                        attempt = f"att-parent-{suffix}"
                        lease = DC.supervisor_lease_path(jobs, attempt)
                        lease.parent.mkdir(parents=True, exist_ok=True)
                        holder = lease.open("w+")
                        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        nonce = "c" * 64
                        holder.write(f"kind={DC.SUPERVISOR_LEASE_KIND}\nattempt_id={attempt}\nnonce={nonce}\n")
                        holder.flush()
                        lines = jobs.read_text().splitlines()
                        extra = (",harness=codex,runtime_sandbox=workspace-write,"
                                 "completion_delivery=app-server-supervised,"
                                 f"supervisor_lease={DC.SUPERVISOR_LEASE_KIND},"
                                 f"supervisor_lease_file={lease},supervisor_lease_nonce={nonce},"
                                 "pid_scope=namespace-local,pid_observer_ns=pid:[outer],pid_ns=pid:[outer]")
                        lines[0] = case.owner_row(attempt, parent.pid, DC.process_start_ticks(parent.pid), extra=extra)
                        jobs.write_text("\n".join(lines) + "\n")
                        namespace = mock.patch.object(DC, "process_namespace_identity", return_value="pid:[inner]")
                    entered, holding, release = threading.Event(), threading.Event(), threading.Event()
                    self.release_events.append(release)
                    rounds, invalid = [], []
                    original_transaction = DC._launch_registry_transaction
                    original_observation = DC._proc_observation
                    def observe(pid):
                        if pid == parent.pid and invalid:
                            return (("present", "different-start", "S") if change == "pid-reuse"
                                    else ("inaccessible", "", ""))
                        return original_observation(pid)
                    @contextmanager
                    def wait_gap(path):
                        rounds.append(True)
                        if len(rounds) == commit:
                            def hold_writer():
                                with Path(f"{path}.lock").open("a") as lock:
                                    fcntl.flock(lock, fcntl.LOCK_EX)
                                    holding.set()
                                    release.wait(10)
                            writer = threading.Thread(target=hold_writer)
                            self.threads.append(writer)
                            writer.start()
                            self.assertTrue(holding.wait(5))
                            entered.set()
                        with original_transaction(path) as lines:
                            yield lines
                    marker = Path(td) / "must-not-run"
                    try:
                        with namespace, mock.patch.object(DC, "_proc_observation", side_effect=observe):
                            if holder is not None:
                                binding = DC.resolve_live_parent_attempt(
                                    jobs, parent_slug="owner", repo="/repo", worktree="/wt",
                                    expected_attempt_id=f"att-parent-{suffix}")
                                self.assertEqual(binding.liveness_source, "supervisor-lease")
                            with mock.patch.object(DC, "_launch_registry_transaction", side_effect=wait_gap):
                                done, result = self.async_call(lambda: DC.spawn_claimed_attempt(
                                    jobs, f"att-child-{suffix}", parent_binding=binding, spawn=self.spawn(marker)))
                                self.assertTrue(entered.wait(5))
                                self.assertFalse(done.is_set())
                                if change == "death":
                                    parent.terminate(); parent.wait(timeout=5)
                                elif holder is not None:
                                    fcntl.flock(holder, fcntl.LOCK_UN); holder.close()
                                else:
                                    invalid.append(True)
                                # Registry stays open and byte-identical while the
                                # exact local authority changes behind the lock.
                                self.assertEqual(jobs.read_text().splitlines()[0].split("\t")[1], "open")
                                release.set()
                                self.assertTrue(done.wait(10))
                        error = result.get("error")
                        self.assertIsInstance(error, DC.DispatchContractError, result)
                        self.assertEqual(error.reason, "parent-attempt-not-live-" +
                                         ("after-spawn" if commit == 1 else "after-post-claim"))
                        self.assertEqual(len(rounds), commit)
                        self.assertFalse(marker.exists())
                        meta = DC.parse_registry_metadata(jobs.read_text().splitlines()[1].split("\t")[5])
                        self.assertEqual(meta["launch_claimed"], "0" if commit == 1 else "1")
                        self.assertIsNotNone(self.children[-1].poll())
                        print(f"parent-lock-wait commit={commit} change={change} released=false row=open")
                    finally:
                        release.set()
                        if holder is not None and not holder.closed:
                            holder.close()
                        if parent.poll() is None:
                            parent.terminate()
                        parent.wait(timeout=5)

    def test_legacy_global_launcher_fresh_identity_wins_only_one_release(self):
        self.register("att-legacy")
        legacy_marker, new_marker = self.base / "legacy", self.base / "new"
        legacy = []
        def old_launcher(_identity):
            # Emulate the old global-only launch transaction, without taking
            # the new attempt preparation lock. The new child is still fenced.
            with Path(f"{self.jobs}.launch.lock").open("a") as old_lock:
                fcntl.flock(old_lock.fileno(), fcntl.LOCK_EX)
                with DC._launch_registry_transaction(self.jobs) as lines:
                    gate_read, gate_write = os.pipe()
                    proc = self.spawn(legacy_marker)(gate_read)
                    os.close(gate_read)
                    identity = DC.process_launch_identity(proc.pid)
                    identity.update(DC.process_launch_cgroup(proc.pid))
                    fields = lines[0].split("\t")
                    fields[5] = fields[5].replace("launch_claimed=0", "launch_claimed=1")
                    fields[5] += "," + ",".join(f"{k}={v}" for k, v in identity.items())
                    DC._atomic_registry_replace(self.jobs, ["\t".join(fields)])
                    os.write(gate_write, b"1")
                    os.close(gate_write)
                    legacy.append(proc)
        with self.assertRaises(DC.DispatchContractError) as caught:
            DC.spawn_claimed_attempt(self.jobs, "att-legacy", parent_binding=None,
                                     spawn=self.spawn(new_marker), pre_release=old_launcher)
        self.assertEqual(caught.exception.reason, "attempt-pre-release-identity-changed")
        legacy[0].wait(timeout=5)
        self.assertTrue(legacy_marker.exists())
        self.assertFalse(new_marker.exists())
        self.assertTrue(all(proc.poll() is not None for proc in self.children))

    def test_cancellation_and_row_mutation_during_observer_refuse_fenced_payload(self):
        for key, value in (("parent_close_requested", "1"), ("launch_home", "/changed")):
            with self.subTest(key=key):
                attempt = f"att-{key}"
                self.register(attempt)
                marker = self.base / key
                def mutate(_identity):
                    self.assert_global_locks_free()
                    with DC._launch_registry_transaction(self.jobs) as lines:
                        index, fields, _meta = DC._launch_row(lines, attempt)
                        fields[5] += f",{key}={value}"
                        lines[index] = "\t".join(fields)
                        DC._atomic_registry_replace(self.jobs, lines)
                with self.assertRaises(DC.DispatchContractError) as caught:
                    DC.spawn_claimed_attempt(self.jobs, attempt, parent_binding=None,
                                             spawn=self.spawn(marker), pre_release=mutate)
                self.assertIn(caught.exception.reason,
                              ("cancelled-by-parent", "attempt-pre-release-identity-changed"))
                self.assertFalse(marker.exists())
                self.assertIsNotNone(self.children[-1].poll())

    def test_opaque_mutation_and_preclaim_callbacks_run_unlocked_and_pin_input(self):
        for name in ("mutation_precheck", "preclaim"):
            attempt = f"att-{name}"
            row = self.register(attempt)
            before = self.jobs.read_bytes()
            def reject(lines):
                self.assert_global_locks_free()
                self.assertEqual(lines, before.decode().splitlines())
                raise DC.DispatchContractError("fixture-refused")
            with self.assertRaises(DC.DispatchContractError) as caught:
                DC.claim_attempt_row(self.jobs, attempt, row, launch=True, **{name: reject})
            self.assertEqual(caught.exception.reason, "fixture-refused")
            self.assertEqual(self.jobs.read_bytes(), before)
        # An opaque callback can depend on another project or on row absence.
        # Changed input is refused once; it never runs under the writer lock.
        def change_registry(_lines):
            self.register("att-new-dependency", "B")
        with self.assertRaises(DC.DispatchContractError) as caught:
            DC.claim_attempt_row(self.jobs, attempt, row, launch=True, preclaim=change_registry)
        self.assertEqual(caught.exception.reason, "attempt-registration-dependency-changed")

    def test_observer_callback_failure_and_post_claim_abort_run_unlocked(self):
        for phase in ("pre_release", "post_claim"):
            attempt = f"att-{phase}"
            self.register(attempt)
            marker = self.base / phase
            def fail(_identity):
                self.assert_global_locks_free()
                raise RuntimeError("fixture-observer-failure")
            with self.assertRaises(DC.DispatchContractError) as caught:
                DC.spawn_claimed_attempt(self.jobs, attempt, parent_binding=None,
                                         spawn=self.spawn(marker), **{phase: fail})
            self.assertEqual(caught.exception.reason, f"attempt-{phase.replace('_', '-')}-callback-failed")
            self.assertFalse(marker.exists())
            self.assertIsNotNone(self.children[-1].poll())

    def test_full_source_content_rechecked_after_post_claim_before_release(self):
        self.register("att-content")
        source = self.base / "source"
        source.write_text("A")
        marker = self.base / "must-not-run"
        calls = []
        def validate(_jobs, _fields, _metadata):
            self.assert_global_locks_free()
            calls.append(source.read_text())
            if calls[-1] != "A":
                raise SUBJECT.SourceSubjectError("review-source-subject-changed-before-launch", "fixture")
        with mock.patch.object(SUBJECT, "validate_attempt", side_effect=validate):
            with self.assertRaises(DC.DispatchContractError) as caught:
                DC.spawn_claimed_attempt(self.jobs, "att-content", parent_binding=None,
                    spawn=self.spawn(marker), post_claim=lambda _identity: (source.write_text("B") and {}))
        self.assertEqual(caught.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(calls, ["A", "A", "B"])
        self.assertFalse(marker.exists())
        self.assertIsNotNone(self.children[-1].poll())

    def test_marker_permission_rechecked_after_observer_before_release(self):
        self.register("att-marker")
        marker = self.base / "gate-marker"
        marker.write_text("valid")
        payload = self.base / "must-not-run"
        def preclaim(_lines):
            self.assert_global_locks_free()
            if marker.read_text() != "valid":
                raise DC.DispatchContractError("completion-marker-stale")
        with self.assertRaises(DC.DispatchContractError) as caught:
            DC.spawn_claimed_attempt(self.jobs, "att-marker", parent_binding=None,
                spawn=self.spawn(payload), preclaim=preclaim,
                pre_release=lambda _identity: (marker.write_text("changed") and {}))
        self.assertEqual(caught.exception.reason, "completion-marker-stale")
        self.assertFalse(payload.exists())

    def test_fresh_terminal_claim_after_external_preparation_is_refused(self):
        attempt, route = "att-terminal", "rt-terminal"
        row = self.register(attempt, extra=f",route_id={route}")
        def terminal(_lines):
            self.assert_global_locks_free()
            with Path(f"{self.jobs}.lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                DC.claim_terminal_route_locked(self.jobs, route, attempt, lock_fd=lock.fileno(),
                                               proof={"fixture": True})
        with self.assertRaises(DC.DispatchContractError) as caught:
            DC.claim_attempt_row(self.jobs, attempt, row, launch=True, mutation_precheck=terminal)
        self.assertEqual(caught.exception.reason, "terminal-claim-conflict")
        self.assertNotIn("launch_claimed=1", self.jobs.read_text())

    def test_reservation_witness_stays_held_through_preparation_and_fenced_admission(self):
        root = self.base / "governor"
        governor = Path(__file__).with_name("model-worker-governor.py")
        token, _reservation = DC.reserve_governor_token(governor, root, "dispatch")
        handle = DC._GOVERNOR_WITNESS_HANDLES[DC._governor_witness_key(root, token)]
        self.addCleanup(lambda: DC.cancel_governor_reservation(governor, root, token)
                        if not handle._closed else None)
        self.register("att-reservation")
        witness = handle.path
        seen = []
        def check(_value):
            self.assert_global_locks_free()
            self.assertFalse(handle._closed)
            with witness.open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append(True)
            return {}
        with mock.patch.object(SUBJECT, "validate_attempt", side_effect=lambda *args: check(args)):
            proc, _identity = DC.spawn_claimed_attempt(self.jobs, "att-reservation", parent_binding=None,
                spawn=self.spawn(self.base / "payload"), pre_release=check, post_claim=check)
        proc.wait(timeout=5)
        self.assertEqual(len(seen), 5)
        # The launcher, not admission or observers, still owns the witness.
        check(None)
        DC.cancel_governor_reservation(governor, root, token)
        self.assertTrue(handle._closed)
        with witness.open("a") as contender:
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def source_fixture(self, attempt):
        # Reuse PR984's real Git/source-binding fixture; its helper stays read-only.
        spec = importlib.util.spec_from_file_location(
            "nas_source_fixture", Path(__file__).with_name("review_source_subject_integration.test.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        fixture = module.SourceSubjectIntegrationTest("test_admission")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.jobs = fixture.jobs
        metadata = fixture._metadata(fixture.review_node, attempt, "review")
        metadata.update({"capability": "autopilot-code", "capability_mode": "debug",
            "qa": "standard", "intensity": "standard", "unit": "qa/code-review",
            "write_scope": "_internal/dev_reviews/**", "completion_gate": "code-impl-review",
            "artifact_root": str(fixture.artifact_root), "log_file": str(fixture.base / "worker.log"),
            "launch_home": str(fixture.agent_home), "launch_authority": "conductor",
            "launch_fence": "registry-v1"})
        row = "\t".join(("fixture", "open", str(fixture.repo), str(fixture.repo), attempt,
                         ",".join(f"{key}={value}" for key, value in metadata.items())))
        self.assertTrue(DC.claim_attempt_row(self.jobs, attempt, row))
        return module, fixture

    def test_real_source_content_change_with_same_head_size_and_mtime_is_refused(self):
        _module, fixture = self.source_fixture("att-real-source")
        engine = fixture.repo / "engine.py"
        before = engine.stat()
        head = fixture.git("rev-parse", "HEAD")
        payload = self.base / "must-not-run-real"
        def mutate(_identity):
            self.assert_global_locks_free()
            engine.write_text("value = 'B'\n")
            os.utime(engine, ns=(before.st_atime_ns, before.st_mtime_ns))
            return {}
        with self.assertRaises(DC.DispatchContractError) as caught:
            DC.spawn_claimed_attempt(self.jobs, "att-real-source", parent_binding=None,
                spawn=self.spawn(payload), post_claim=mutate)
        self.assertEqual(caught.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(fixture.git("rev-parse", "HEAD"), head)
        self.assertEqual(engine.stat().st_size, before.st_size)
        self.assertEqual(engine.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(payload.exists())
        self.assertIsNotNone(self.children[-1].poll())

    def test_real_marker_change_after_observer_is_refused_by_existing_gate(self):
        module, fixture = self.source_fixture("att-real-marker")
        fixture.current_smoke_markers("nas-marker")
        # These semantic fixture producers never spawned a payload. Give the
        # existing process fence that exact durable fixture outcome as well.
        fixture._row(fixture._row_metadata("att-nas-marker-smoke"),
                     extra={"launch_claimed": "0", "launch_outcome": "never-launched"})
        marker = module.ROUTE_WRITER.completion_dir(
            fixture.route["route_id"], jobs=self.jobs) / "smoke.json"
        payload = self.base / "must-not-run-marker"
        def gate(lines):
            self.assert_global_locks_free()
            DC.completion_marker_gate(str(fixture.route_file), "impl-review", "start",
                fixture.agent_home, self.jobs, registry_lines=lines, attempt_id="att-real-marker")
        def mutate(_identity):
            self.assert_global_locks_free()
            marker.unlink()
            return {}
        with self.assertRaises(DC.DispatchContractError) as caught:
            DC.spawn_claimed_attempt(self.jobs, "att-real-marker", parent_binding=None,
                spawn=self.spawn(payload), preclaim=gate, pre_release=mutate)
        self.assertEqual(caught.exception.reason, "completion-marker-missing")
        self.assertFalse(marker.exists())  # The observer actually ran.
        self.assertFalse(payload.exists())

    def test_source_currentness_runs_after_last_permission_callback(self):
        self.register("att-last-callback")
        valid = [True]
        callbacks = []
        def permission(_lines):
            self.assert_global_locks_free()
            callbacks.append(True)
            if len(callbacks) == 3:
                valid[0] = False
        def source(*_args):
            self.assert_global_locks_free()
            if not valid[0]:
                raise SUBJECT.SourceSubjectError("review-source-subject-changed-before-launch", "fixture")
        payload = self.base / "must-not-run-callback"
        with mock.patch.object(SUBJECT, "validate_attempt", side_effect=source):
            with self.assertRaises(DC.DispatchContractError) as caught:
                DC.spawn_claimed_attempt(self.jobs, "att-last-callback", parent_binding=None,
                    spawn=self.spawn(payload), preclaim=permission)
        self.assertEqual(caught.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(len(callbacks), 3)
        self.assertFalse(payload.exists())

    def test_fresh_commit_rejects_source_binding_changed_after_preparation(self):
        self.register("att-binding-race")
        payload = self.base / "must-not-run-binding"
        real_transaction = DC._launch_registry_transaction
        rounds = []
        @contextmanager
        def race(jobs):
            with real_transaction(jobs) as lines:
                rounds.append(True)
                if len(rounds) == 2:
                    index, fields, _meta = DC._launch_row(lines, "att-binding-race")
                    fields[5] += ",review_subject_digest=changed-after-source-observation"
                    lines[index] = "\t".join(fields)
                    DC._atomic_registry_replace(jobs, lines)
                yield lines
        with mock.patch.object(DC, "_launch_registry_transaction", side_effect=race):
            with self.assertRaises(DC.DispatchContractError) as caught:
                DC.spawn_claimed_attempt(self.jobs, "att-binding-race", parent_binding=None,
                                         spawn=self.spawn(payload))
        self.assertEqual(caught.exception.reason, "attempt-post-claim-identity-changed")
        self.assertEqual(len(rounds), 2)
        self.assertFalse(payload.exists())
        self.assertIsNotNone(self.children[-1].poll())

    def test_parallel_nodes_register_and_launch_while_a_peer_prepares(self):
        route = ",route_id=rt-nas-parallel,route_hash=sha256:" + "a" * 64
        # A parent's recorded replacement remains a parent dependency; its
        # other children do not become consumed work of this peer.
        self.jobs.write_text(self.row("att-old-parent") + "\n" + self.row(
            "att-current-parent", extra=",automatic_retry_of=att-old-parent") + "\n")
        route += ",parent_attempt_id=att-current-parent"
        entered, release, callback = self.paused_source(
            "att-peer-A", DC._review_source_registration_fence)
        with mock.patch.object(DC, "_review_source_registration_fence", side_effect=callback):
            done_a, result_a = self.async_call(lambda: DC.claim_attempt_row(
                self.jobs, "att-peer-A", self.row("att-peer-A", extra=route + ",route_node=review-A")))
            try:
                self.assertTrue(entered.wait(5))
                self.register("att-peer-B", extra=route + ",route_node=review-B")
                proc, _ = DC.spawn_claimed_attempt(self.jobs, "att-peer-B", parent_binding=None,
                                                  spawn=self.spawn(self.base / "peer-B"))
                proc.wait(timeout=5)
                self.assertTrue((self.base / "peer-B").exists())
                self.assertFalse(done_a.is_set())
            finally:
                release.set()
            self.assertTrue(done_a.wait(5))
            self.assertNotIn("error", result_a, result_a)
            self.assertTrue(result_a["value"])
        proc, _ = DC.spawn_claimed_attempt(self.jobs, "att-peer-A", parent_binding=None,
                                          spawn=self.spawn(self.base / "peer-A"))
        proc.wait(timeout=5)
        self.assertTrue((self.base / "peer-A").exists())

    def test_observer_annotations_survive_both_launch_commits(self):
        self.register("att-annotations")
        rounds = []
        def observer(_identity):
            self.assert_global_locks_free()
            rounds.append(True)
            self.assertTrue(DC.annotate_attempt_row(self.jobs, "att-annotations", {
                "summary_sid": "summary-" + str(len(rounds)),
                "summary_owner_pid": str(os.getpid()), "resolved_model": "fixture-model"}))
        proc, _ = DC.spawn_claimed_attempt(self.jobs, "att-annotations", parent_binding=None,
            spawn=self.spawn(self.base / "annotated-payload"), pre_release=observer, post_claim=observer)
        proc.wait(timeout=5)
        self.assertTrue((self.base / "annotated-payload").exists())
        _, _, meta = DC._launch_row(self.jobs.read_text().splitlines(), "att-annotations")
        self.assertEqual(meta["summary_sid"], "summary-2")

    def test_consumed_predecessor_remains_in_the_final_cas(self):
        route = ",route_id=rt-nas-dependency,route_hash=sha256:" + "b" * 64
        self.register("att-consumed", extra=route + ",route_node=plan-check")
        self.register("att-dependent", extra=route + ",route_node=execute")
        def permission(_lines):
            self.assert_global_locks_free()
        permission._dispatch_registry_scoped = True
        permission._dispatch_registry_nodes = frozenset({"plan-check"})
        original = DC._launch_registry_transaction
        rounds = []
        @contextmanager
        def change_predecessor(jobs):
            with original(jobs) as lines:
                rounds.append(True)
                if len(rounds) == 2:
                    index, fields, _ = DC._launch_row(lines, "att-consumed")
                    fields[1] = "done"
                    lines[index] = "\t".join(fields)
                    DC._atomic_registry_replace(jobs, lines)
                yield lines
        payload = self.base / "must-not-use-changed-predecessor"
        with mock.patch.object(DC, "_launch_registry_transaction", side_effect=change_predecessor):
            with self.assertRaises(DC.DispatchContractError) as refused:
                DC.spawn_claimed_attempt(self.jobs, "att-dependent", parent_binding=None,
                                         preclaim=permission, spawn=self.spawn(payload))
        self.assertEqual(refused.exception.reason, "attempt-post-claim-identity-changed")
        self.assertFalse(payload.exists())
        self.assertIsNotNone(self.children[-1].poll())

    def test_fresh_commit_keeps_authority_and_unknown_fields_exact(self):
        for phase in (1, 2):
            for key in ("pid_start", "launch_claimed", "parent_attempt_id", "runtime_home", "review_input_digest",
                        "review_subject_digest", "future_unknown_authority"):
                with self.subTest(phase=phase, key=key):
                    attempt = f"att-cas-{phase}-{key}"
                    self.register(attempt)
                    payload = self.base / (attempt + ".payload")
                    original = DC._launch_registry_transaction
                    rounds = []
                    @contextmanager
                    def mutate(jobs):
                        with original(jobs) as lines:
                            rounds.append(True)
                            if len(rounds) == phase:
                                index, fields, _ = DC._launch_row(lines, attempt)
                                fields[5] += f",{key}=changed"
                                lines[index] = "\t".join(fields)
                                DC._atomic_registry_replace(jobs, lines)
                            yield lines
                    with mock.patch.object(DC, "_launch_registry_transaction", side_effect=mutate):
                        with self.assertRaises(DC.DispatchContractError) as refused:
                            DC.spawn_claimed_attempt(self.jobs, attempt, parent_binding=None,
                                                     spawn=self.spawn(payload))
                    self.assertEqual(refused.exception.reason,
                        "attempt-" + ("pre-release" if phase == 1 else "post-claim") + "-identity-changed")
                    self.assertFalse(payload.exists())
                    self.assertIsNotNone(self.children[-1].poll())

    def test_late_descendant_of_consumed_work_refuses_release(self):
        route = ",route_id=rt-nas-descendant,route_hash=sha256:" + "c" * 64
        self.register("att-consumed-work", extra=route + ",route_node=plan-check")
        self.register("att-after-work", extra=route + ",route_node=execute")
        def permission(_lines):
            self.assert_global_locks_free()
        permission._dispatch_registry_scoped = True
        permission._dispatch_registry_nodes = frozenset({"plan-check"})
        original = DC._launch_registry_transaction
        rounds = []
        @contextmanager
        def new_child(jobs):
            with original(jobs) as lines:
                rounds.append(True)
                if len(rounds) == 2:
                    lines.append(self.row("att-late-child", extra=route
                        + ",route_node=independent,parent_attempt_id=att-consumed-work"))
                    DC._atomic_registry_replace(jobs, lines)
                yield lines
        payload = self.base / "must-not-overtake-descendant"
        with mock.patch.object(DC, "_launch_registry_transaction", side_effect=new_child):
            with self.assertRaises(DC.DispatchContractError) as refused:
                DC.spawn_claimed_attempt(self.jobs, "att-after-work", parent_binding=None,
                                         preclaim=permission, spawn=self.spawn(payload))
        self.assertEqual(refused.exception.reason, "attempt-post-claim-identity-changed")
        self.assertFalse(payload.exists())
        self.assertIsNotNone(self.children[-1].poll())


if __name__ == "__main__":
    unittest.main()
