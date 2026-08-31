#!/usr/bin/env python3

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
