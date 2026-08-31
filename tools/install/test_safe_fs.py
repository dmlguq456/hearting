#!/usr/bin/env python3
"""Regression tests for destructive authority, stable locks, and CAS rollback."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import safe_fs


def _lock_worker(target: str, marker: str, delay: float) -> None:
    with safe_fs.TargetLock(Path(target)):
        path = Path(marker)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        time.sleep(delay)
        path.write_text(before + "x", encoding="utf-8")


class SafeFsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def auth(self, path: Path, state: safe_fs.PathState | None = None) -> safe_fs.Authority:
        return safe_fs.authority(
            path,
            owner="test-manifest:exact",
            allowed_roots=(self.root,),
            expected=state,
        )

    def test_rejects_root_home_prefix_and_symlink_parent(self) -> None:
        with self.assertRaisesRegex(safe_fs.SafetyError, "unsafe-ambient-path"):
            safe_fs.authority(Path("/"), owner="x", allowed_paths=(Path("/"),))
        home = Path(os.environ["HOME"]).resolve()
        with self.assertRaisesRegex(safe_fs.SafetyError, "unsafe-ambient-path"):
            safe_fs.authority(home, owner="x", allowed_paths=(home,))
        outside = self.root.with_name(self.root.name + "-lookalike") / "file"
        with self.assertRaisesRegex(safe_fs.SafetyError, "ownership-unproved"):
            safe_fs.authority(outside, owner="x", allowed_roots=(self.root,))
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(safe_fs.SafetyError, "unsafe-ambient-path"):
            safe_fs.authority(link / "victim", owner="x", allowed_roots=(self.root,))

    def test_atomic_file_and_symlink_transitions_have_exact_state(self) -> None:
        target = self.root / "entry"
        missing = safe_fs.capture_state(target)
        file_state = safe_fs.atomic_write_bytes(
            self.auth(target, missing), b"file", 0o640
        )
        self.assertEqual(file_state.kind, "file")
        self.assertEqual(target.read_bytes(), b"file")
        link_state = safe_fs.atomic_write_symlink(
            self.auth(target, file_state), "vendor/codex"
        )
        self.assertEqual(link_state.kind, "symlink")
        self.assertEqual(os.readlink(target), "vendor/codex")
        removed = safe_fs.remove_exact(self.auth(target, link_state))
        self.assertEqual(removed.kind, "missing")

    def test_atomic_replacement_never_exposes_an_existing_leaf_as_missing(self) -> None:
        target = self.root / "atomic-leaf"
        target.write_bytes(b"before")
        before = safe_fs.capture_state(target)
        observations: list[bool] = []
        real_replace = os.replace

        def observed_replace(source, destination):
            if Path(destination) == target:
                observations.append(os.path.lexists(destination))
            return real_replace(source, destination)

        with mock.patch.object(safe_fs.os, "replace", side_effect=observed_replace):
            file_state = safe_fs.atomic_write_bytes(
                self.auth(target, before), b"after", 0o600
            )
            safe_fs.atomic_write_symlink(
                self.auth(target, file_state), "vendor-successor"
            )
        self.assertEqual(observations, [True, True])

    # destructive-ok: reason=construct a same-byte successor inode; boundary=one target below self.root in this TemporaryDirectory
    def test_cas_restore_preserves_unrelated_and_same_byte_successors(self) -> None:
        target = self.root / "profile"
        target.write_bytes(b"before")
        pre = safe_fs.capture_state(target, include_payload=True)
        post = safe_fs.atomic_write_bytes(self.auth(target, pre), b"ours", 0o600)
        target.write_bytes(b"successor")
        with self.assertRaisesRegex(safe_fs.SafetyError, "concurrent-successor"):
            safe_fs.cas_restore(self.auth(target), pre, post)
        self.assertEqual(target.read_bytes(), b"successor")

        # Same bytes through a new inode are still a successor.
        target.unlink()
        target.write_bytes(b"ours")
        with self.assertRaisesRegex(safe_fs.SafetyError, "concurrent-successor"):
            safe_fs.cas_restore(self.auth(target), pre, post)
        self.assertEqual(target.read_bytes(), b"ours")

    def test_transaction_restores_file_symlink_and_missing(self) -> None:
        file_path = self.root / "file"
        link_path = self.root / "link"
        missing_path = self.root / "missing"
        file_path.write_bytes(b"old")
        link_path.symlink_to("old-target")
        authorities = {
            name: self.auth(path)
            for name, path in {
                "file": file_path,
                "link": link_path,
                "missing": missing_path,
            }.items()
        }
        with safe_fs.transaction(authorities) as tx:
            file_post = safe_fs.atomic_write_bytes(
                authorities["file"].with_expected(tx.preimages["file"]), b"new", 0o600
            )
            link_post = safe_fs.atomic_write_symlink(
                authorities["link"].with_expected(tx.preimages["link"]), "new-target"
            )
            missing_post = safe_fs.atomic_write_bytes(
                authorities["missing"].with_expected(tx.preimages["missing"]), b"new", 0o600
            )
            self.assertEqual({file_post.kind, link_post.kind, missing_post.kind}, {"file", "symlink"})
            tx.seal()
            tx.restore()
        self.assertEqual(file_path.read_bytes(), b"old")
        self.assertEqual(os.readlink(link_path), "old-target")
        self.assertFalse(missing_path.exists())

    def test_transaction_successor_blocks_every_rollback_leaf(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.write_bytes(b"before-first")
        second.write_bytes(b"before-second")
        authorities = {"first": self.auth(first), "second": self.auth(second)}
        with safe_fs.transaction(authorities) as transaction:
            first_post = safe_fs.atomic_write_bytes(
                authorities["first"].with_expected(transaction.preimages["first"]),
                b"ours-first",
                0o600,
            )
            safe_fs.atomic_write_bytes(
                authorities["second"].with_expected(transaction.preimages["second"]),
                b"ours-second",
                0o600,
            )
            transaction.seal()
            # destructive-ok: reason=construct one unrelated successor; boundary=first below self.root in this TemporaryDirectory
            first.unlink()
            first.write_bytes(b"successor-first")
            with self.assertRaisesRegex(safe_fs.SafetyError, "concurrent-successor"):
                transaction.restore()
            self.assertNotEqual(safe_fs.capture_state(first), first_post)
            self.assertEqual(second.read_bytes(), b"ours-second")

    @unittest.skipIf(safe_fs.fcntl is None, "POSIX flock required")
    def test_replaced_lock_path_is_rejected(self) -> None:
        target = self.root / "replaced-lock-target"
        path = safe_fs.lock_path(target)
        real_open = os.open
        replaced = False

        def replacing_open(candidate, flags, mode=0o777, **kwargs):
            nonlocal replaced
            descriptor = real_open(candidate, flags, mode, **kwargs)
            if Path(candidate) == path and not replaced:
                replaced = True
                # destructive-ok: reason=inject a replaced lock pathname; boundary=one safe_fs lock leaf selected for this fixture target
                path.unlink()
                path.write_bytes(b"replacement")
            return descriptor

        with mock.patch.object(safe_fs.os, "open", side_effect=replacing_open):
            with self.assertRaisesRegex(safe_fs.SafetyError, "lock identity is unsafe"):
                with safe_fs.TargetLock(target):
                    pass
        self.assertTrue(replaced)

    @unittest.skipIf(safe_fs.fcntl is None, "POSIX flock required")
    def test_target_lock_is_shared_and_pathname_remains_stable(self) -> None:
        target = self.root / "shared"
        marker = self.root / "marker"
        lock = safe_fs.lock_path(target)
        processes = [
            multiprocessing.Process(
                target=_lock_worker, args=(str(target), str(marker), 0.03)
            )
            for _ in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "xxxx")
        self.assertTrue(lock.is_file())
        first = lock.stat()
        with safe_fs.TargetLock(target):
            second = lock.stat()
        self.assertEqual((first.st_dev, first.st_ino), (second.st_dev, second.st_ino))


if __name__ == "__main__":
    unittest.main()
