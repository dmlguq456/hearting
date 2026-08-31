#!/usr/bin/env python3
"""Incident regressions for invalid requests, profile races, and uninstall CAS."""

from __future__ import annotations

from argparse import Namespace
from contextlib import ExitStack
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_launcher  # noqa: E402
import fixture_env  # noqa: E402
import installer  # noqa: E402
import manifest  # noqa: E402
import runtime_activation  # noqa: E402
import safe_fs  # noqa: E402


def _leaf_signature(path: Path) -> tuple[object, ...]:
    info = os.lstat(path)
    kind = stat.S_IFMT(info.st_mode)
    content = None
    if stat.S_ISREG(info.st_mode):
        content = hashlib.sha256(path.read_bytes()).hexdigest()
    elif stat.S_ISLNK(info.st_mode):
        content = os.readlink(path)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        kind,
        content,
    )


def _tree_signature(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return ()
    paths = [root, *sorted(root.rglob("*"), key=os.fspath)]
    return tuple((str(path.relative_to(root)), *_leaf_signature(path)) for path in paths)


def _profile_install_worker(
    fixture: str,
    profile_root: str,
    codex_home: str,
    bin_dir: str,
    vendor: str,
    queue: multiprocessing.Queue,
) -> None:
    os.environ.update(
        {
            "HEARTING_FIXTURE_ROOT": fixture,
            "HOME": str(Path(fixture) / "home"),
            "ZDOTDIR": profile_root,
            "SHELL": "/bin/zsh",
            "CODEX_HOME": codex_home,
            "HARNESS_BIN_DIR": bin_dir,
            "PATH": str(Path(vendor).parent),
        }
    )
    try:
        result = codex_launcher.install(
            codex_home=Path(codex_home),
            bin_dir=Path(bin_dir),
            real_command=vendor,
            profile_policy="manage",
        )
        queue.put(("ok", result["status"]))
    except Exception as exc:  # noqa: BLE001 - child result is asserted by parent.
        queue.put(("blocked", type(exc).__name__))


def _crash_lock_worker(target: str, ready: multiprocessing.Event) -> None:
    with safe_fs.TargetLock(target):
        ready.set()
        os._exit(91)


class DeletionSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.fixture = self.base / "fixture"
        self.repo = Path(__file__).resolve().parents[2]
        self.environment = fixture_env.patched_environment(
            self.fixture,
            self.repo,
            base={"PATH": os.environ.get("PATH", "")},
        )
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)

    def _runtime_args(self, **changes: object) -> Namespace:
        values: dict[str, object] = {
            "runtime": ["codex"],
            "runtime_command": "activate",
            "mode": "linked",
            "source": str(self.repo),
            "scope": "global",
            "strict": False,
            "report_bundle_root": None,
        }
        values.update(changes)
        return Namespace(**values)

    def test_invalid_requests_do_not_capture_lock_or_mutate_ambient_zdotdir(self) -> None:
        outside = self.base / "outside-zdotdir"
        outside.mkdir()
        canary = outside / ".zshrc"
        canary.write_bytes(b"synthetic outside canary\n")
        canary.chmod(0o640)
        os.environ.update({"SHELL": "/bin/zsh", "ZDOTDIR": str(outside)})

        lock_root = safe_fs._lock_root()
        invalid = (
            self._runtime_args(runtime=["invalid-runtime"]),
            self._runtime_args(mode="invalid-mode"),
            self._runtime_args(scope="project"),
            self._runtime_args(source=str(self.fixture / "missing-source")),
        )
        for args in invalid:
            with self.subTest(args=vars(args)):
                canary_before = _leaf_signature(canary)
                fixture_before = _tree_signature(self.fixture)
                locks_before = _tree_signature(lock_root)
                with ExitStack() as stack:
                    launcher_capture = stack.enter_context(
                        mock.patch.object(
                            codex_launcher,
                            "capture_snapshot",
                            wraps=codex_launcher.capture_snapshot,
                        )
                    )
                    runtime_capture = stack.enter_context(
                        mock.patch.object(
                            runtime_activation,
                            "capture_runtime_state",
                            wraps=runtime_activation.capture_runtime_state,
                        )
                    )
                    mutation_spies = [
                        stack.enter_context(mock.patch.object(os, name, wraps=getattr(os, name)))
                        for name in ("unlink", "remove", "rmdir", "replace", "rename")
                    ]
                    mutation_spies.append(
                        stack.enter_context(mock.patch.object(shutil, "rmtree", wraps=shutil.rmtree))
                    )
                    mutation_spies.extend(
                        stack.enter_context(
                            mock.patch.object(tempfile, name, wraps=getattr(tempfile, name))
                        )
                        for name in ("mkstemp", "mkdtemp")
                    )
                    result = installer.cmd_runtime(args)

                self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
                self.assertIn("invalid-before-mutation", json.dumps(result))
                launcher_capture.assert_not_called()
                runtime_capture.assert_not_called()
                for spy in mutation_spies:
                    spy.assert_not_called()
                self.assertEqual(_leaf_signature(canary), canary_before)
                self.assertEqual(_tree_signature(self.fixture), fixture_before)
                self.assertEqual(_tree_signature(lock_root), locks_before)

    def _run_profile_race(self, *, existing: bool) -> None:
        race_root = self.fixture / ("file-preimage" if existing else "missing-preimage")
        profile_root = race_root / "zdot"
        profile_root.mkdir(parents=True)
        profile = profile_root / ".zshrc"
        original = b"pre-existing profile\n"
        if existing:
            profile.write_bytes(original)
            profile.chmod(0o640)
        vendor = race_root / "vendor" / "codex"
        vendor.parent.mkdir(parents=True)
        vendor.write_bytes(b"#!/bin/sh\nexit 0\n")
        vendor.chmod(0o755)

        queue: multiprocessing.Queue = multiprocessing.Queue()
        processes = []
        for index in range(4):
            codex_home = race_root / f"codex-home-{index}"
            bin_dir = race_root / f"bin-{index}"
            process = multiprocessing.Process(
                target=_profile_install_worker,
                args=(
                    str(self.fixture),
                    str(profile_root),
                    str(codex_home),
                    str(bin_dir),
                    str(vendor),
                    queue,
                ),
            )
            processes.append(process)
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=2) for _ in processes]
        self.assertEqual(sum(status == "ok" for status, _ in results), 1)
        self.assertTrue(profile.is_file())
        payload = profile.read_bytes()
        if existing:
            self.assertTrue(payload.startswith(original))
        self.assertEqual(payload.count(codex_launcher.PROFILE_START), 1)
        self.assertEqual(payload.count(codex_launcher.PROFILE_END), 1)

    @unittest.skipIf(safe_fs.fcntl is None, "POSIX flock required")
    def test_four_codex_homes_serialize_file_and_missing_profile_preimages(self) -> None:
        self._run_profile_race(existing=True)
        self._run_profile_race(existing=False)

    @unittest.skipIf(safe_fs.fcntl is None, "POSIX flock required")
    def test_crash_releases_target_lock_without_replacing_lock_inode(self) -> None:
        target = self.fixture / "crash-target"
        ready = multiprocessing.Event()
        process = multiprocessing.Process(
            target=_crash_lock_worker, args=(str(target), ready)
        )
        process.start()
        self.assertTrue(ready.wait(5))
        process.join(5)
        self.assertEqual(process.exitcode, 91)
        lock = safe_fs.lock_path(target)
        before = lock.stat()
        with safe_fs.TargetLock(target):
            during = lock.stat()
        self.assertEqual(
            (before.st_dev, before.st_ino), (during.st_dev, during.st_ino)
        )

    def _uninstall_fixture(self, *, modified_copy: bool, repointed_link: bool) -> tuple[dict, Path, Path]:
        runtime_home = self.fixture / "opencode-home"
        runtime_home.mkdir(parents=True, exist_ok=True)
        copy_path = runtime_home / "models.conf"
        canonical = b"canonical model config\n"
        copy_path.write_bytes(b"user modification\n" if modified_copy else canonical)
        source = self.fixture / "source" / "skill"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"skill\n")
        link = runtime_home / "skills" / "demo"
        link.parent.mkdir(parents=True)
        if repointed_link:
            successor = self.fixture / "user-successor"
            successor.write_bytes(b"successor\n")
            link.symlink_to(successor)
        else:
            link.symlink_to(source)
        manifest_path = self.fixture / "state" / "manifest.json"
        manifest._write_manifest(
            manifest_path,
            {
                "schema": 1,
                "runtime": "opencode",
                "scope": "global",
                "version": "fixture",
                "timestamp": "fixture",
                "files": {"models.conf": hashlib.sha256(canonical).hexdigest()},
            },
        )
        args = Namespace(
            runtimes=["opencode"], target="opencode", scope="global", dry_run=False
        )
        plan = {
            "opencode": [
                {"action": "symlink", "dest": str(link), "source": str(source)}
            ]
        }
        with (
            mock.patch.object(runtime_activation, "validate_scope"),
            mock.patch.object(
                runtime_activation,
                "deactivate",
                return_value={"status": "not-active", "removed": []},
            ),
            mock.patch.object(manifest, "_manifest_path", return_value=manifest_path),
            mock.patch.object(installer.paths, "runtime_home", return_value=runtime_home),
            mock.patch.object(installer.projector, "plan", return_value=plan),
        ):
            result = installer.cmd_uninstall(args)
        return result, copy_path, link

    def test_uninstall_preserves_modified_copy_once_file(self) -> None:
        result, copy_path, link = self._uninstall_fixture(
            modified_copy=True, repointed_link=False
        )
        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertIn("expected-state-mismatch", json.dumps(result))
        self.assertEqual(copy_path.read_bytes(), b"user modification\n")
        self.assertTrue(link.is_symlink())

    def test_uninstall_preserves_repointed_projection(self) -> None:
        result, copy_path, link = self._uninstall_fixture(
            modified_copy=False, repointed_link=True
        )
        successor = os.readlink(link)
        result_text = json.dumps(result)
        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertIn("expected-state-mismatch", result_text)
        self.assertEqual(copy_path.read_bytes(), b"canonical model config\n")
        self.assertEqual(os.readlink(link), successor)

    def test_corrupt_manifest_fails_closed(self) -> None:
        manifest_path = self.fixture / "corrupt-manifest.json"
        manifest_path.write_bytes(b"{not-json")
        with self.assertRaisesRegex(ValueError, "ownership-unproved"):
            manifest.load_ownership_manifest(manifest_path, "codex", "global")


if __name__ == "__main__":
    unittest.main()
