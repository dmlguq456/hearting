#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import distribution
import fixture_env


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
    unittest.main()
