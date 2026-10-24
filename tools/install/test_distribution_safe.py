#!/usr/bin/env python3

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

import fixture_env
import distribution


class StandaloneDistributionSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        source = base / "source"
        source.mkdir()
        self.environment = fixture_env.build_environment(
            base / "fixture", source, base={"PATH": os.environ.get("PATH", "")}
        )
        fixture_env.prepare_environment(self.environment)
        self.environment["HARNESS_TEST_PLATFORM"] = "linux"
        self.environment["HARNESS_SCHEDULER_NO_ACTIVATE"] = "1"
        self.patch = mock.patch.dict(os.environ, self.environment, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_registry_lock_reenters_on_owner_thread_but_blocks_other_threads(self) -> None:
        jobs = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "registry-boundary" / "jobs.log"
        started = threading.Event()
        acquired = threading.Event()

        def competing_admission() -> None:
            started.set()
            with distribution._registry_lock(jobs):
                acquired.set()

        worker = threading.Thread(target=competing_admission)
        with distribution._registry_lock(jobs):
            with distribution._registry_lock(jobs):
                self.assertTrue(Path(f"{jobs}.lock").is_file())
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertFalse(acquired.wait(timeout=0.1), "another thread bypassed the owner lock")

        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "competing admission did not resume after unlock")
        self.assertTrue(acquired.is_set())

    @unittest.skipUnless(hasattr(os, "fork"), "fork lock ownership is Linux/POSIX-specific")
    def test_registry_lock_after_fork_waits_for_parent_and_releases_in_child(self) -> None:
        jobs = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "fork-boundary" / "jobs.log"
        read_fd, write_fd = os.pipe()
        child = None
        try:
            with distribution._registry_lock(jobs):
                child = os.fork()
                if child == 0:
                    os.close(read_fd)
                    try:
                        with distribution._registry_lock(jobs):
                            os.write(write_fd, b"entered")
                    finally:
                        os._exit(0)
                os.close(write_fd)
                ready, _, _ = select.select([read_fd], [], [], 0.5)
                self.assertFalse(ready, "fork child reused its parent's reentrant ownership")

            ready, _, _ = select.select([read_fd], [], [], 3.0)
            self.assertTrue(ready, "fork child did not enter after parent released the lock")
            self.assertEqual(os.read(read_fd, 32), b"entered")
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertEqual(status, 0)
            child = None
        finally:
            os.close(read_fd)
            try:
                os.close(write_fd)
            except OSError:
                pass
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child, 0)

    def test_process_guard_excludes_only_the_exact_owned_lock_descriptor(self) -> None:
        candidate = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "descriptor-release"
        jobs = candidate / ".dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True)
        with distribution._registry_lock(jobs):
            self.assertEqual(distribution._release_held_by_live_process(candidate), (False, ""))
            descriptor_open = threading.Event()
            release_descriptor = threading.Event()

            def open_independent_descriptor() -> None:
                with open(f"{jobs}.lock", "a+b"):
                    descriptor_open.set()
                    release_descriptor.wait(timeout=2)

            worker = threading.Thread(target=open_independent_descriptor)
            worker.start()
            try:
                self.assertTrue(descriptor_open.wait(timeout=2))
                held, reason = distribution._release_held_by_live_process(candidate)
                self.assertTrue(held, "an unowned descriptor of the owned lock path was ignored")
                self.assertTrue(reason.startswith("live-process:"), reason)
            finally:
                release_descriptor.set()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), "independent descriptor fixture did not close")

    # destructive-ok: reason=install a same-byte foreign successor for CAS verification; boundary=two exact symlink/file fixture leaves below HEARTING_FIXTURE_ROOT
    def test_rollback_preserves_exact_successors(self) -> None:
        root = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "successors"
        root.mkdir()

        pointer = root / "current"
        pointer_pre = distribution._capture_leaf(pointer)
        pointer_post = distribution._atomic_symlink(
            pointer, root / "release-a", expected=pointer_pre
        )
        pointer_temp = root / "pointer-successor"
        pointer_temp.symlink_to(root / "release-b")
        os.replace(pointer_temp, pointer)
        with self.assertRaisesRegex(distribution.DistributionError, "concurrent-successor"):
            distribution._restore_link(pointer, pointer_pre, pointer_post)
        self.assertEqual(pointer.readlink(), root / "release-b")

        state = root / "state.json"
        state_pre = distribution._capture_leaf(state)
        payload = b'{"state":"managed"}\n'
        state_post = distribution._atomic_bytes(state, payload, expected=state_pre)
        state_temp = root / "state-successor"
        state_temp.write_bytes(payload)
        os.replace(state_temp, state)
        with self.assertRaisesRegex(distribution.DistributionError, "concurrent-successor"):
            distribution._restore_bytes(state, state_pre, state_post, None)
        self.assertEqual(state.read_bytes(), payload)

    def test_release_keep_message_names_the_actual_evidence(self) -> None:
        # Only an open attempt row is a "live dispatch attempt". A route record without a
        # closing outcome, or evidence that could not be read, must be named as what it is;
        # routine cleanup must not ask a person to close the record manually.
        release = Path("/releases/v9")
        attempt = distribution._release_keep_message(release, "open-attempt:att-123")
        self.assertIn("still referenced by a live dispatch attempt (open-attempt:att-123)", attempt)
        legacy = distribution._release_keep_message(release, "legacy-open-row-in-release-registry")
        self.assertIn("live dispatch attempt", legacy)
        route = distribution._release_keep_message(release, "open-route:rt-abc")
        self.assertNotIn("live dispatch attempt", route)
        self.assertIn("unresolved route reference (open-route:rt-abc)", route)
        self.assertIn("execution or continuation", route)
        self.assertNotIn("capability-route.py close", route)
        for why in ("registry-unreadable:/x/jobs.log", "registry-row-unparsable:/x/jobs.log",
                    "route-launch-home-unresolvable:rt-abc", "route-discovery-unreliable:/x/bindings"):
            with self.subTest(why=why):
                line = distribution._release_keep_message(release, why)
                self.assertNotIn("live dispatch attempt", line)
                self.assertIn(f"cannot be proved unused ({why})", line)
        for line in (attempt, legacy, route):
            self.assertTrue(line.startswith(f"harness release: {release} "), line)

    def test_target_lock_domain_ignores_runtime_homes(self) -> None:
        target = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "shared" / "current"
        target.parent.mkdir()
        key = distribution.hashlib.sha256(os.fsencode(str(target))).hexdigest()
        lock = distribution._standalone_lock_root() / f"{key}.lock"
        with distribution._target_lock(target):
            first = os.lstat(lock)
        os.environ["CODEX_HOME"] = str(
            Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "other-codex"
        )
        os.environ["HARNESS_STATE_ROOT"] = str(
            Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "other-state"
        )
        with distribution._target_lock(target):
            second = os.lstat(lock)
        self.assertEqual((first.st_dev, first.st_ino), (second.st_dev, second.st_ino))

    def test_foreign_scheduler_unit_is_preserved(self) -> None:
        service, _timer = distribution._systemd_paths()
        service.parent.mkdir(parents=True)
        service.write_text("foreign\n", encoding="utf-8")
        before = distribution._capture_leaf(service)
        with self.assertRaisesRegex(distribution.DistributionError, "ownership-unproved"):
            distribution.disable_auto_update()
        self.assertEqual(distribution._capture_leaf(service), before)
        self.assertEqual(service.read_text(encoding="utf-8"), "foreign\n")

    def test_systemd_probe_reads_the_last_result_from_the_service(self) -> None:
        # The timer only schedules the run; Result and ExecMainStatus of the
        # last update belong to the service it starts.
        service, timer = distribution._systemd_paths()
        replies = {
            timer.name: "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                        "LastTriggerUSec=Wed 2026-10-07 03:06:00 KST\n",
            service.name: "Result=exit-code\nExecMainCode=1\nExecMainStatus=1\n",
        }
        asked = []

        def probe(command):
            asked.append(command[3])
            return True, replies[command[3]]

        with mock.patch.object(distribution, "_probe_command", side_effect=probe):
            result = distribution._probe_systemd()
        self.assertEqual(asked, [timer.name, service.name])
        self.assertEqual(result["last_result"], "exit-code")
        self.assertEqual(result["exit_status"], 1)


class DispatchMigrationReanchorTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.candidate = base / "release"
        self.source_root = self.candidate / ".dispatch"
        self.environment = {"HARNESS_STATE_ROOT": str(base / "state")}
        self.target_root = distribution.stable_state_root(self.environment)
        relative = Path("completion/rt-test/plan.att-test.attempt.json")
        self.source = self.source_root / relative
        self.target = self.target_root / relative
        self.source.parent.mkdir(parents=True)
        self.link = {
            "schema_version": 2,
            "route_id": "rt-test", "node_id": "plan", "attempt_id": "att-test",
            "dispatch_depth": 1, "transport": "headless",
            "execution_surface": "headless", "registered_worker": True,
            "fallback_hop": "", "evidence_sha256": "a" * 64,
            "completion_marker": str(self.source.parent / "plan.json"),
            "completion_marker_history": str(self.source.parent / "plan.1.json"),
            # Normal owner-closure sidecars extend the original field set.
            "stage_authority": "owner-closure",
            "owner_closure_proof": {"reviews": ["review-a"], "accepted": True},
            "future_metadata": {"label": "추가 필드", "revision": 1},
        }
        self.source.write_bytes(self._serialize(self.link))
        for name in ("plan.json", "plan.1.json"):
            (self.source.parent / name).write_text("{}\n", encoding="utf-8")
        result = distribution.run_dispatch_state_migration(
            self.source_root, environ=self.environment
        )
        self.assertEqual(result["status"], "completed")

    @staticmethod
    def _serialize(value: object) -> bytes:
        return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def test_reanchored_sidecar_allows_deletion_with_extension_fields(self) -> None:
        self.assertNotEqual(self.source.read_bytes(), self.target.read_bytes())
        expected = dict(self.link)
        for key in ("completion_marker", "completion_marker_history"):
            expected[key] = str(self.target.parent / Path(self.link[key]).name)
        self.assertEqual(self.target.read_bytes(), self._serialize(expected))
        for pass_number in range(2):
            with self.subTest(pass_number=pass_number):
                # Retry must retain the correctly reanchored target as well.
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
                self.assertEqual(
                    distribution._migration_deletion_precondition(
                        self.candidate, self.environment
                    ),
                    (True, ""),
                )
                self.assertEqual(self.source.read_bytes(), self._serialize(self.link))

    def test_observation_collision_is_archived_without_replacing_current_bytes(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"legacy observation\n")
        target.write_bytes(b"current observation\n")
        root_digest = hashlib.sha256(
            os.fsencode(str(self.source_root.resolve()))
        ).hexdigest()
        content_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        archived = (Path(self.environment["HARNESS_STATE_ROOT"])
                    / "dispatch-observation-archive" / root_digest
                    / relative / content_digest)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            self.assertEqual(archived.stat().st_nlink, 1)
            self.assertEqual(list(archived.parent.iterdir()), [archived])
            self.assertEqual(
                distribution._migration_deletion_precondition(
                    self.candidate, self.environment
                ),
                (True, ""),
            )
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            self.assertEqual(archived.stat().st_nlink, 1)
            self.assertEqual(list(archived.parent.iterdir()), [archived])
        self.assertEqual(target.read_bytes(), b"current observation\n")
        self.assertEqual(archived.read_bytes(), b"legacy observation\n")

    def test_observation_classifier_matches_only_the_verified_name_forms(self) -> None:
        positives = (
            Path("logs/dispatch-session-sweep.log"),
            Path("logs/legacy-worker.heartbeat"),
            Path("plugin-load.legacy-worker.mark"),
        )
        negatives = (
            Path("logs/dispatch-session-sweep.log.1"),
            Path("logs/nested/legacy-worker.heartbeat"),
            Path("logs/legacy-worker.heartbeat.bak"),
            Path("logs/.heartbeat"),
            Path("logs/worker name.heartbeat"),
            Path("plugin-load.legacy-worker.mark.bak"),
            Path("plugin-load..mark"),
            Path("plugin-load.worker name.mark"),
            Path("heartbeats/att-test.json"),
            Path("completion/proof.json"),
            Path("unrecognized.log"),
        )
        for relative in positives:
            with self.subTest(relative=relative):
                self.assertTrue(distribution._dispatch_observation_file(relative))
        for relative in negatives:
            with self.subTest(relative=relative):
                self.assertFalse(distribution._dispatch_observation_file(relative))

    def test_observation_written_after_carry_is_archived_by_final_delta_check(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"legacy observation\n")
        target.write_bytes(b"current observation\n")
        root_digest = hashlib.sha256(
            os.fsencode(str(self.source_root.resolve()))
        ).hexdigest()
        archive_root = (Path(self.environment["HARNESS_STATE_ROOT"])
                        / "dispatch-observation-archive" / root_digest
                        / relative)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            source.write_bytes(b"legacy observation plus late write\n")
            self.assertEqual(
                distribution._migration_deletion_precondition(
                    self.candidate, self.environment
                ),
                (True, ""),
            )
        digests = {hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in archive_root.iterdir()}
        self.assertEqual(
            digests,
            {
                hashlib.sha256(b"legacy observation\n").hexdigest(),
                hashlib.sha256(b"legacy observation plus late write\n").hexdigest(),
            },
        )
        self.assertEqual(target.read_bytes(), b"current observation\n")

    def test_completion_proof_collision_still_blocks_release_deletion(self) -> None:
        relative = Path("completion/proof.json")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'{"proof":"legacy"}\n')
        target.write_bytes(b'{"proof":"current"}\n')
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
            allowed, reason = distribution._migration_deletion_precondition(
                self.candidate, self.environment
            )
        self.assertFalse(allowed)
        self.assertIn("delta-digest-mismatch", reason)
        self.assertEqual(source.read_bytes(), b'{"proof":"legacy"}\n')
        self.assertEqual(target.read_bytes(), b'{"proof":"current"}\n')
        self.assertFalse(
            (Path(self.environment["HARNESS_STATE_ROOT"])
             / "dispatch-observation-archive").exists()
        )

    def test_corrupt_archive_and_archive_write_failure_keep_the_release(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"legacy observation\n")
        target.write_bytes(b"current observation\n")
        root_digest = hashlib.sha256(
            os.fsencode(str(self.source_root.resolve()))
        ).hexdigest()
        content_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        archived = (Path(self.environment["HARNESS_STATE_ROOT"])
                    / "dispatch-observation-archive" / root_digest
                    / relative / content_digest)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            archived.write_bytes(b"corrupt archive")
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
            self.assertFalse(distribution._migration_deletion_precondition(
                self.candidate, self.environment
            )[0])
            archived.write_bytes(b"legacy observation\n")
            # destructive-ok: reason=inject a missing archive object before the copy-failure case; boundary=one object below this test's temporary state root
            archived.unlink()
            with mock.patch.object(distribution, "_atomic_migrate_copy", return_value=False):
                self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
        self.assertEqual(source.read_bytes(), b"legacy observation\n")
        self.assertEqual(target.read_bytes(), b"current observation\n")

    def test_archive_link_and_fsync_failures_are_retryable(self) -> None:
        failures = ("copy", "link", "file-fsync", "directory-fsync")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            for failure in failures:
                with self.subTest(failure=failure):
                    relative = Path(f"logs/{failure}-worker.heartbeat")
                    source = self.source_root / relative
                    target = self.target_root / relative
                    source.parent.mkdir(parents=True, exist_ok=True)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    legacy = f"legacy {failure}\n".encode()
                    current = f"current {failure}\n".encode()
                    source.write_bytes(legacy)
                    target.write_bytes(current)
                    root_digest = hashlib.sha256(
                        os.fsencode(str(self.source_root.resolve()))
                    ).hexdigest()
                    content_digest = hashlib.sha256(legacy).hexdigest()
                    archived = (Path(self.environment["HARNESS_STATE_ROOT"])
                                / "dispatch-observation-archive" / root_digest
                                / relative / content_digest)

                    if failure == "copy":
                        failure_patch = mock.patch.object(
                            distribution.shutil, "copyfileobj",
                            side_effect=OSError("copy fixture"),
                        )
                    elif failure == "link":
                        failure_patch = mock.patch.object(
                            distribution.os, "link", side_effect=OSError("link fixture")
                        )
                    elif failure == "file-fsync":
                        failure_patch = mock.patch.object(
                            distribution.os, "fsync", side_effect=OSError("file fsync fixture")
                        )
                    else:
                        real_fsync = os.fsync
                        calls = 0

                        def fail_directory_fsync(fd):
                            nonlocal calls
                            calls += 1
                            if calls == 2:
                                raise OSError("directory fsync fixture")
                            return real_fsync(fd)

                        failure_patch = mock.patch.object(
                            distribution.os, "fsync", side_effect=fail_directory_fsync
                        )

                    with failure_patch:
                        self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
                    self.assertEqual(source.read_bytes(), legacy)
                    self.assertEqual(target.read_bytes(), current)
                    self.assertFalse(any(".migrate-" in path.name
                                         for path in archived.parent.iterdir())
                                     if archived.parent.exists() else False)

                    self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
                    self.assertEqual(archived.read_bytes(), legacy)
                    self.assertEqual(archived.stat().st_nlink, 1)
                    self.assertEqual(list(archived.parent.iterdir()), [archived])
                    self.assertEqual(target.read_bytes(), current)
                    self.assertTrue(distribution._migration_deletion_precondition(
                        self.candidate, self.environment
                    )[0])

    def test_symlink_in_dispatch_tree_keeps_source_and_refuses_carry(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        outside = self.candidate.parent / "outside-observation.log"
        outside.write_bytes(b"outside bytes\n")
        source.symlink_to(outside)
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
            allowed, reason = distribution._migration_deletion_precondition(
                self.candidate, self.environment
            )
        self.assertFalse(allowed)
        self.assertIn("delta-unreadable", reason)
        self.assertTrue(source.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside bytes\n")

    def test_symlinked_live_sidecar_is_never_reanchored_outside_target_root(self) -> None:
        relative = Path("completion/rt-test/plan.att-test.attempt.json")
        source = self.source_root / relative
        target = self.target_root / relative
        outside = self.candidate.parent / "outside-sidecar.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps({"completion_marker": str(source.parent / "plan.json")}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        outside.write_text('{"completion_marker":"outside"}\n', encoding="utf-8")
        # destructive-ok: reason=replace the migrated fixture destination with a symlink attack; boundary=one destination below this test's temporary state root
        target.unlink()
        target.symlink_to(outside)
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"),
                         '{"completion_marker":"outside"}\n')

    def test_other_sidecar_changes_still_block_deletion(self) -> None:
        original = self.target.read_bytes()
        mutations = {
            "evidence": {"evidence_sha256": "b" * 64},
            "boolean_type": {"registered_worker": 1},
            "extension": {"owner_closure_proof": {"reviews": [], "accepted": True}},
            "history": {"completion_marker_history": str(self.target.parent / "plan.2.json")},
            "wrong_root": {"completion_marker": str(self.source.parent / "plan.json")},
        }
        for name, changes in mutations.items():
            with self.subTest(change=name):
                changed = json.loads(original)
                changed.update(changes)
                self.target.write_bytes(self._serialize(changed))
                self.assertEqual(
                    distribution._migration_deletion_precondition(self.candidate, self.environment),
                    (False, "dispatch-state-migration-blocked-live-attempt:delta-digest-mismatch"),
                )
        self.target.write_bytes(original)
        marker = self.target.parent / "plan.json"
        marker.write_text('{"changed": true}\n', encoding="utf-8")
        self.assertFalse(distribution._migration_deletion_precondition(
            self.candidate, self.environment
        )[0], "ordinary files must still be byte-identical")

    def test_ambiguous_source_is_not_normalized_into_deletion_proof(self) -> None:
        original = self.source.read_bytes()
        duplicate = original.replace(b'"schema_version": 2,', b'"schema_version": 1, "schema_version": 2,')
        escaping = dict(self.link)
        escaping["completion_marker"] = str(self.source_root / ".." / "outside.json")
        rounded = dict(self.link, future_metadata=0.12345678901234568)
        cases = {
            "duplicate_key": duplicate,
            "non_object": b"[ ]\n",
            "escaping_path": self._serialize(escaping),
            "lossy_number": self._serialize(rounded).replace(
                b"0.12345678901234568", b"0.12345678901234567890123456789"
            ),
        }
        for name, raw in cases.items():
            with self.subTest(source=name):
                self.source.write_bytes(raw)
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for key in ("completion_marker", "completion_marker_history"):
                        relative = distribution._relative_to_release(Path(parsed[key]), self.source_root)
                        parsed[key] = str(self.target_root / relative)
                self.target.write_bytes(self._serialize(parsed))
                self.assertFalse(distribution._migration_deletion_precondition(
                    self.candidate, self.environment
                )[0])


class ReleasePycachePruneTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.share = self.root / "share" / "hearting"
        self.releases = self.share / "releases"
        self.releases.mkdir(parents=True)
        self.state = self.root / "state" / "hearting"
        self.pycache = self.state / "pycache"

    def _cache_for(self, release: Path) -> Path:
        return self.pycache.joinpath(*self.releases.parts[1:], release.name)

    def _patch_cleanup(self, stack: ExitStack, *, in_use=(False, ""), route=([], "")) -> None:
        for name, value in {
            "data_root": self.share,
            "state_root": self.state,
            "_stable_registry_snapshot": [],
            "_open_route_launch_homes": route,
            "_release_in_use": in_use,
            "_release_held_by_live_process": (False, ""),
            "_release_projection_referenced": False,
            "_succeed_dispatch_state": True,
            "_migration_deletion_precondition": (True, ""),
            "_retention_containment_precondition": (True, ""),
        }.items():
            if callable(value):
                stack.enter_context(mock.patch.object(
                    distribution, name, side_effect=value
                ))
            else:
                stack.enter_context(mock.patch.object(
                    distribution, name, return_value=value
                ))

    def _create_release(self, name: str, timestamp: int) -> Path:
        release = self.releases / name
        release.mkdir()
        (release / "cache_probe.py").write_text("VALUE = 42\n", encoding="utf-8")
        os.utime(release, (timestamp, timestamp))
        return release

    def test_prune_removes_only_corresponding_default_pycache(self) -> None:
        old = self._create_release("v-old", 1)
        middle = self._create_release("v-middle", 2)
        newest = self._create_release("v-newest", 3)
        import_env = os.environ.copy()
        import_env["PYTHONPYCACHEPREFIX"] = str(self.pycache)
        import_env.pop("PYTHONDONTWRITEBYTECODE", None)
        imported = subprocess.run(
            [sys.executable, "-S", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import cache_probe",
             str(old)],
            capture_output=True, text=True, env=import_env, check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        old_cache = self._cache_for(old)
        self.assertTrue(list(old_cache.rglob("*.pyc")))
        for release in (middle, newest):
            cache = self._cache_for(release)
            cache.mkdir(parents=True)
            (cache / "retained.pyc").write_bytes(b"retained")
        sibling = self.pycache.joinpath(*self.releases.parts[1:-1])
        sibling = sibling / "releases-neighbor" / "v-unrelated"
        sibling.mkdir(parents=True)
        (sibling / "unrelated.pyc").write_bytes(b"unrelated")

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            distribution._cleanup_releases(set())

        self.assertFalse(old.exists())
        self.assertFalse(old_cache.exists())
        self.assertTrue(middle.exists() and self._cache_for(middle).exists())
        self.assertTrue(newest.exists() and self._cache_for(newest).exists())
        self.assertTrue((sibling / "unrelated.pyc").is_file())

    def test_cleanup_discovers_unchanged_global_routes_once_for_many_releases(self) -> None:
        releases = [self._create_release(f"v-{index}", index) for index in range(5)]
        calls = 0

        def discover(_environ, **_kwargs):
            nonlocal calls
            calls += 1
            return [], ""

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            stack.enter_context(mock.patch.object(
                distribution, "_open_route_launch_homes", side_effect=discover
            ))
            distribution._cleanup_releases(set())

        self.assertEqual(calls, 1, "unchanged global route records were rediscovered per release")
        self.assertEqual(sum(release.exists() for release in releases), 2)

    def test_unrelated_admission_proceeds_during_delayed_global_discovery(self) -> None:
        releases = [self._create_release(f"v-{index}", index) for index in range(3)]
        unrelated_jobs = releases[-1] / ".dispatch" / "jobs.log"
        unrelated_jobs.parent.mkdir()
        discovery_started = threading.Event()
        allow_discovery = threading.Event()
        admitted = threading.Event()
        worker_errors = []

        def delayed_discovery(_environ, **_kwargs):
            discovery_started.set()
            if not allow_discovery.wait(timeout=3):
                raise TimeoutError("fixture did not release the bounded discovery")
            return [], ""

        def cleanup() -> None:
            try:
                with ExitStack() as stack:
                    self._patch_cleanup(stack)
                    stack.enter_context(mock.patch.object(
                        distribution, "_open_route_launch_homes",
                        side_effect=delayed_discovery,
                    ))
                    distribution._cleanup_releases(set())
            except BaseException as exc:
                worker_errors.append(exc)

        thread = threading.Thread(target=cleanup)
        thread.start()
        try:
            self.assertTrue(discovery_started.wait(timeout=2))
            with distribution._registry_lock(unrelated_jobs):
                admitted.set()
            self.assertTrue(admitted.is_set(), "global discovery blocked unrelated admission")
        finally:
            allow_discovery.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "cleanup fixture did not finish within its bound")
        self.assertEqual(worker_errors, [])

    def test_unrelated_admission_proceeds_during_delayed_refresh(self) -> None:
        releases = [self._create_release(f"refresh-{index}", index) for index in range(3)]
        boundary_jobs = releases[0] / ".dispatch" / "jobs.log"
        unrelated_jobs = releases[-1] / ".dispatch" / "jobs.log"
        boundary_jobs.parent.mkdir()
        unrelated_jobs.parent.mkdir()
        for index, release in enumerate(releases):
            os.utime(release, (index, index))
        generation_dir = self.root / "refresh-routes"
        generation_dir.mkdir()
        refresh_started = threading.Event()
        allow_refresh = threading.Event()
        admitted = threading.Event()
        worker_errors = []
        discovery_calls = 0

        def inventory_for_scan(inventory):
            inventory.clear()
            inventory.update({
                "files": set(), "directories": {generation_dir}, "registries": set(),
                "start_files": {},
                "start_directories": {
                    generation_dir: distribution._path_fingerprint(generation_dir)
                },
                "unstable": False,
            })
            inventory["fingerprints"] = distribution._snapshot_discovery_inputs(
                inventory, ()
            )

        def delayed_refresh(_environ, *, inventory, **_kwargs):
            nonlocal discovery_calls
            discovery_calls += 1
            inventory_for_scan(inventory)
            if discovery_calls == 1:
                (generation_dir / "published.json").write_text("{}\n", encoding="utf-8")
            elif discovery_calls == 2:
                refresh_started.set()
                if not allow_refresh.wait(timeout=3):
                    raise TimeoutError("fixture did not release the bounded refresh")
            return [], ""

        def cleanup() -> None:
            try:
                with ExitStack() as stack:
                    self._patch_cleanup(stack)
                    stack.enter_context(mock.patch.object(
                        distribution, "_open_route_launch_homes",
                        side_effect=delayed_refresh,
                    ))
                    stack.enter_context(mock.patch.object(
                        distribution, "_dispatch_cleanup_boundary_paths",
                        return_value=[boundary_jobs],
                    ))
                    distribution._cleanup_releases(set())
            except BaseException as exc:
                worker_errors.append(exc)

        thread = threading.Thread(target=cleanup)
        thread.start()
        try:
            self.assertTrue(refresh_started.wait(timeout=2))

            def admit_unrelated() -> None:
                with distribution._registry_lock(unrelated_jobs):
                    admitted.set()

            competitor = threading.Thread(target=admit_unrelated)
            competitor.start()
            self.assertTrue(
                admitted.wait(timeout=1),
                "delayed refresh held an unrelated dispatch admission lock",
            )
            competitor.join(timeout=2)
            self.assertFalse(competitor.is_alive())
        finally:
            allow_refresh.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "cleanup fixture did not finish within its bound")
        self.assertEqual(discovery_calls, 2)
        self.assertEqual(worker_errors, [])

    def test_unrelated_admission_proceeds_during_delayed_cache_deletion(self) -> None:
        releases = [self._create_release(f"cache-delay-{index}", index) for index in range(3)]
        boundary_jobs = releases[0] / ".dispatch" / "jobs.log"
        unrelated_jobs = releases[-1] / ".dispatch" / "jobs.log"
        boundary_jobs.parent.mkdir()
        unrelated_jobs.parent.mkdir()
        for index, release in enumerate(releases):
            os.utime(release, (index, index))
        cache_started = threading.Event()
        allow_cache = threading.Event()
        admitted = threading.Event()
        worker_errors = []
        real_remove_cache = distribution._remove_release_pycache

        def delayed_cache_removal(release):
            if Path(release) == releases[0]:
                cache_started.set()
                if not allow_cache.wait(timeout=3):
                    raise TimeoutError("fixture did not release cache deletion")
            return real_remove_cache(release)

        def cleanup() -> None:
            try:
                with ExitStack() as stack:
                    self._patch_cleanup(stack)
                    stack.enter_context(mock.patch.object(
                        distribution, "_remove_release_pycache",
                        side_effect=delayed_cache_removal,
                    ))
                    stack.enter_context(mock.patch.object(
                        distribution, "_dispatch_cleanup_boundary_paths",
                        return_value=[boundary_jobs],
                    ))
                    distribution._cleanup_releases(set())
            except BaseException as exc:
                worker_errors.append(exc)

        thread = threading.Thread(target=cleanup)
        thread.start()
        try:
            self.assertTrue(cache_started.wait(timeout=2))

            def admit_unrelated() -> None:
                with distribution._registry_lock(unrelated_jobs):
                    admitted.set()

            competitor = threading.Thread(target=admit_unrelated)
            competitor.start()
            self.assertTrue(
                admitted.wait(timeout=1),
                "delayed cache deletion held an unrelated dispatch admission lock",
            )
            competitor.join(timeout=2)
            self.assertFalse(competitor.is_alive())
        finally:
            allow_cache.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "cleanup fixture did not finish within its bound")
        self.assertFalse(releases[0].exists())
        self.assertEqual(worker_errors, [])

    def test_known_observation_collisions_prune_but_completion_conflict_retains(self) -> None:
        real_succeed_dispatch_state = distribution._succeed_dispatch_state
        observations = {
            Path("logs/dispatch-session-sweep.log"): (
                b"legacy sweep bytes\n", b"current sweep bytes\n"),
            Path("logs/legacy-worker.heartbeat"): (
                b"legacy heartbeat bytes\n", b"current heartbeat bytes\n"),
            Path("plugin-load.legacy-worker.mark"): (
                b"legacy plugin marker\n", b"current plugin marker\n"),
        }

        for conflict in (False, True):
            case_root = self.root / ("proof-conflict" if conflict else "observations-only")
            self.share = case_root / "share" / "hearting"
            self.releases = self.share / "releases"
            self.releases.mkdir(parents=True)
            self.state = case_root / "state" / "hearting"
            self.pycache = self.state / "pycache"

            old = self._create_release("v-old", 1)
            middle = self._create_release("v-middle", 2)
            newest = self._create_release("v-newest", 3)
            current = self.share / "current"
            current.symlink_to(newest)

            source_dispatch = old / ".dispatch"
            current_dispatch = newest / ".dispatch"
            for relative, (legacy_bytes, current_bytes) in observations.items():
                source = source_dispatch / relative
                target = current_dispatch / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(legacy_bytes)
                target.write_bytes(current_bytes)

            source_proof = source_dispatch / "completion" / "proof.json"
            target_proof = current_dispatch / "completion" / "proof.json"
            source_proof.parent.mkdir(parents=True, exist_ok=True)
            target_proof.parent.mkdir(parents=True, exist_ok=True)
            source_proof.write_bytes(b'{"proof":"legacy"}\n')
            target_proof.write_bytes(
                b'{"proof":"current"}\n' if conflict else b'{"proof":"legacy"}\n'
            )

            cache = self._cache_for(old)
            cache.mkdir(parents=True)
            (cache / "legacy.pyc").write_bytes(b"derived cache")
            for release, timestamp in ((old, 1), (middle, 2), (newest, 3)):
                os.utime(release, (timestamp, timestamp))

            environment = {
                "HOME": str(case_root / "home"),
                "XDG_STATE_HOME": str(case_root / "xdg-state"),
                "HARNESS_DATA_ROOT": str(self.share),
                "HARNESS_STATE_ROOT": str(self.state),
            }
            with mock.patch.dict(os.environ, environment, clear=True), ExitStack() as stack:
                self._patch_cleanup(stack)
                stack.enter_context(mock.patch.object(
                    distribution, "_succeed_dispatch_state",
                    side_effect=real_succeed_dispatch_state,
                ))
                distribution._cleanup_releases(set())

            self.assertEqual(old.exists(), conflict)
            self.assertEqual(cache.exists(), conflict)
            self.assertTrue(middle.is_dir() and newest.is_dir())
            if conflict:
                self.assertEqual(source_proof.read_bytes(), b'{"proof":"legacy"}\n')
            else:
                self.assertFalse(source_proof.exists())
            self.assertEqual(
                target_proof.read_bytes(),
                b'{"proof":"current"}\n' if conflict else b'{"proof":"legacy"}\n',
            )

            source_digest = hashlib.sha256(os.fsencode(str(source_dispatch.resolve()))).hexdigest()
            archive_root = self.state / "dispatch-observation-archive" / source_digest
            for relative, (legacy_bytes, current_bytes) in observations.items():
                archive = (archive_root / relative
                           / hashlib.sha256(legacy_bytes).hexdigest())
                target = current_dispatch / relative
                self.assertEqual(archive.read_bytes(), legacy_bytes)
                self.assertEqual(target.read_bytes(), current_bytes)
                self.assertFalse(any(".migrate-" in path.name
                                     for path in archive.parent.iterdir()))

    def test_orphan_cache_waits_for_live_or_unresolved_release_evidence(self) -> None:
        cache = self._cache_for(self.releases / "gone")
        cache.mkdir(parents=True)
        (cache / "old.pyc").write_bytes(b"orphan")

        def prove_use(_release, _stable, route_snapshot, _registry_snapshot=None):
            if route_snapshot[1]:
                return True, route_snapshot[1]
            return True, "open-attempt:att-live"

        for route in (([], ""), ([], "route-discovery-unreliable:/fixture")):
            with self.subTest(route=route):
                with ExitStack() as stack:
                    self._patch_cleanup(stack, in_use=prove_use, route=route)
                    distribution._cleanup_releases(set())
                self.assertTrue((cache / "old.pyc").is_file())

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            distribution._cleanup_releases(set())
        self.assertFalse(cache.exists())

    def test_failed_cache_removal_is_retried_on_the_next_cleanup(self) -> None:
        old = self._create_release("v-old", 1)
        self._create_release("v-middle", 2)
        self._create_release("v-newest", 3)
        cache = self._cache_for(old)
        cache.mkdir(parents=True)
        (cache / "old.pyc").write_bytes(b"old")
        real_rmtree = distribution.shutil.rmtree

        def fail_cache(path, *args, **kwargs):
            if Path(path) == cache:
                raise PermissionError("fixture cache failure")
            return real_rmtree(path, *args, **kwargs)

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            stack.enter_context(mock.patch.object(
                distribution.shutil, "rmtree", side_effect=fail_cache
            ))
            distribution._cleanup_releases(set())
        self.assertFalse(old.exists())
        self.assertTrue((cache / "old.pyc").is_file())

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            distribution._cleanup_releases(set())
        self.assertFalse(cache.exists())

    def test_release_removal_failure_keeps_its_derived_cache(self) -> None:
        old = self._create_release("v-old", 1)
        self._create_release("v-middle", 2)
        self._create_release("v-newest", 3)
        cache = self._cache_for(old)
        cache.mkdir(parents=True)
        (cache / "old.pyc").write_bytes(b"old")

        def fail_release(path, *_args, **_kwargs):
            if Path(path) == old:
                raise PermissionError("fixture release failure")
            return None

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            stack.enter_context(mock.patch.object(
                distribution.shutil, "rmtree", side_effect=fail_release
            ))
            distribution._cleanup_releases(set())
        self.assertTrue(old.is_dir())
        self.assertTrue((cache / "old.pyc").is_file())


class ManagedRuntimeAdoptionTest(unittest.TestCase):
    REQUIRED = {
        "hearting/harness-manifest.json": "{}\n",
        "hearting/core/CORE.md": "# core\n",
        "hearting/tools/install/harness.sh": "#!/bin/sh\n",
        "hearting/tools/install/installer.py": "# fixture\n",
        "hearting/tools/install/distribution.py": "# fixture\n",
        "hearting/utilities/compute-hosts": "#!/bin/sh\n",
        "hearting/utilities/compute-hosts.py": "#!/usr/bin/env python3\n",
        "hearting/tools/fleet/fleet.sh": "#!/bin/sh\n",
        "hearting/tools/fleet/fleet2.sh": "#!/bin/sh\n",
        "hearting/tools/memory/mem.py": "#!/usr/bin/env python3\n",
        "hearting/tools/memory/protocol_v2.py": "#!/usr/bin/env python3\n",
        "hearting/tools/memory/git_exchange_v2.py": "#!/usr/bin/env python3\n",
        "hearting/tools/memory/sync_v2.py": "#!/usr/bin/env python3\n",
        "hearting/tools/memory/migration_v2.py": "#!/usr/bin/env python3\n",
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        source = base / "source"
        source.mkdir()
        self.assets = base / "assets"
        self.assets.mkdir()
        self.index = base / "release.json"
        self.environment = fixture_env.build_environment(
            base / "fixture", source, base={"PATH": os.environ.get("PATH", "")}
        )
        fixture_env.prepare_environment(self.environment)
        self.environment["HARNESS_TEST_PLATFORM"] = "linux"
        self.environment["HARNESS_SCHEDULER_NO_ACTIVATE"] = "1"
        self.environment["HARNESS_ALLOW_FILE_RELEASES"] = "1"
        self.environment["HARNESS_RELEASE_INDEX_URL"] = self.index.as_uri()
        self.patch = mock.patch.dict(os.environ, self.environment, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.activations = []
        for name, replacement in (
            ("_activate_release", self._fake_activate),
            ("_probe_command", lambda command: (False, "probe unavailable")),
        ):
            patcher = mock.patch.object(distribution, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_release(self, version: str) -> None:
        archive = self.assets / f"{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            files = dict(self.REQUIRED)
            files["hearting/RELEASE_VERSION"] = version + "\n"
            for name, text in files.items():
                payload = text.encode()
                info = tarfile.TarInfo(name)
                info.mode = 0o755 if name.endswith((".sh", "/compute-hosts")) else 0o644
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = self.assets / f"{version}.sha256"
        checksum.write_text(digest + "  hearting.tar.gz\n")
        self.index.write_text(json.dumps({
            "tag_name": version,
            "assets": [
                {"name": "hearting.tar.gz", "browser_download_url": archive.as_uri()},
                {"name": "hearting.tar.gz.sha256", "browser_download_url": checksum.as_uri()},
            ],
        }))

    def _record_activation(self, release_root: Path, runtime: str) -> None:
        path = distribution._runtime_home(runtime) / ".harness/activation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": "packaged", "source_root": str(release_root)}))

    def _fake_activate(self, release_root, runtimes) -> dict:
        selected = list(runtimes)
        self.activations.append((Path(release_root).name, tuple(selected)))
        for runtime in selected:
            self._record_activation(Path(release_root), runtime)
        return {"runtimes": selected, "session_action": {}}

    def _bootstrap_claude_codex_then_activate_opencode(self) -> None:
        self._make_release("v1.0.0")
        installed = distribution.bootstrap("example/harness", "latest", ["claude", "codex"], False)
        self.assertEqual(distribution.managed_status()["runtimes"], ["claude", "codex"])
        # A runtime enabled after the first install activates against the current release.
        self._record_activation(Path(installed["release_root"]), "opencode")
        self.activations.clear()

    def test_runtime_activated_after_install_is_adopted(self) -> None:
        self._bootstrap_claude_codex_then_activate_opencode()
        same = distribution.update()
        self.assertEqual(same["status"], "up-to-date")
        self.assertEqual(distribution.managed_status()["runtimes"], ["claude", "codex", "opencode"])
        self._make_release("v1.1.0")
        distribution.update()
        self.assertEqual(self.activations, [("v1.1.0", ("claude", "codex", "opencode"))])
        status = distribution.managed_status()
        self.assertEqual(status["version"], "v1.1.0")
        self.assertEqual(status["runtimes"], ["claude", "codex", "opencode"])

    def test_explicit_runtime_update_keeps_recorded_runtimes(self) -> None:
        self._bootstrap_claude_codex_then_activate_opencode()
        self._make_release("v1.1.0")
        distribution.update(runtimes=["opencode"])
        self.assertEqual(self.activations, [("v1.1.0", ("opencode",))])
        self.assertEqual(distribution.managed_status()["runtimes"], ["claude", "codex", "opencode"])


class ActivationFailureDiagnosticTest(unittest.TestCase):
    def test_stdout_json_precedence_and_last_lines_entry(self) -> None:
        self.assertEqual(
            distribution._activation_failure_detail(
                '{"error":"typed error", "detail":"detail", "lines":["old", "last"]}',
                "stderr terminal",
            ),
            "typed error",
        )
        self.assertEqual(
            distribution._activation_failure_detail(
                '{"error":12, "detail": "", "lines":["old", "last"]}',
                "stderr terminal",
            ),
            "last",
        )

    def test_malformed_json_uses_terminal_stderr_then_stdout(self) -> None:
        self.assertEqual(
            distribution._activation_failure_detail('{"error":', "first\nstderr terminal\n"),
            "stderr terminal",
        )
        self.assertEqual(
            distribution._activation_failure_detail("stdout terminal\n", "\n"),
            "stdout terminal",
        )

    def test_tail_bound_controls_redaction_and_unicode_limit(self) -> None:
        stdout = "prefix " + ("x" * 70000) + "\n" + (
            "Error: token = abc password:'def' secret: ghi "
            "authorization=Bearer%20abc api-key=q "
            "https://user:pass@example.test/path?token=abc&x=yz\n"
        )
        detail = distribution._activation_failure_detail(stdout, "")
        self.assertLessEqual(len(detail), 1000)
        self.assertNotIn("abc", detail)
        self.assertNotIn("def", detail)
        self.assertNotIn("ghi", detail)
        self.assertNotIn("user:pass@", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertIn("example.test/path", detail)
        self.assertIn("?token=[REDACTED]&x=[REDACTED]", detail)
        self.assertNotIn("\n", detail)
        self.assertEqual(
            distribution._activation_failure_detail("\u0000\u200bterminal\tpath", ""),
            "terminal path",
        )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="hearting-test-distribution-safe-") as root:
        fixture_root = Path(root).resolve()
        with fixture_env.patched_environment(
            fixture_root / "runtime", Path(__file__).resolve().parents[2]
        ) as env:
            private_root = (fixture_root / "runtime").resolve()
            for key in (
                "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "HARNESS_STATE_ROOT",
                "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_ROOT",
            ):
                Path(env[key]).resolve().relative_to(private_root)
            print("routine_fixture_roots=" + json.dumps({
                key: env[key] for key in (
                    "HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT",
                    "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_ROOT",
                )
            }, sort_keys=True), flush=True)
            import distribution
            unittest.main()
