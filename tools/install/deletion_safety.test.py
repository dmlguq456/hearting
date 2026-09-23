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
import distribution  # noqa: E402
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


class ReleaseScanSelectsRouteRecordsTest(unittest.TestCase):
    """One sidecar must not disable release pruning (measured 2026-09-06).

    `_open_route_launch_homes` globbed `*.json` in the routes directory and
    skipped only `.outcome.json`. The later `.gate-release.json` sidecar was
    therefore read as a route record, came back undecidable, and made the whole
    scan unreliable -- and an unreliable scan marks EVERY release in use. On
    this machine that held 20 releases and 606 MB with zero open attempts,
    behind one valid 354-byte sidecar.

    The fix selects by the name `canonical_route_path()` writes, so a sidecar
    shape nobody has invented yet cannot re-break it.
    """

    ROUTE_ID = "rt-da62cded1408b893"

    def _routes_dir(self, base: Path) -> Path:
        routes = base / ".agent_reports" / ".runtime" / "routes"
        routes.mkdir(parents=True)
        return routes

    def _record(self, path: Path, launch_home: str) -> None:
        path.write_text(json.dumps({
            "route_id": path.stem, "schema_version": 2,
            "launch_compatibility_tuple": {
                "launch_home": {"kind": "launch_home", "path": launch_home}},
        }), encoding="utf-8")

    def _scan(self, base: Path):
        with mock.patch.object(
            distribution, "_open_route_artifact_roots",
            return_value=([str(base / ".agent_reports")], [], ""),
        ):
            return distribution._open_route_launch_homes({})

    def test_a_gate_release_sidecar_does_not_make_the_scan_unreliable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            (routes / f"{self.ROUTE_ID}.gate-release.json").write_text(json.dumps({
                "route_id": self.ROUTE_ID, "schema_version": 1,
                "gate_releases": [{"gate": "frame-review", "decision": "proceed"}],
            }), encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "", "one sidecar must not poison the scan")
            self.assertEqual([route_id for route_id, _ in results], [self.ROUTE_ID])

    def test_every_sidecar_shape_beside_a_route_record_is_ignored(self):
        # Not a denylist of the shapes we happen to know: anything that is not
        # `<route_id>.json` is not a route record.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            # Sidecars of a DIFFERENT route: an `.outcome.json` beside this
            # route's own record would (correctly) mark it closed, which is a
            # separate rule and not what this test is about.
            other = "rt-0000000000000000"
            for name in (
                f"{other}.outcome.json",
                f"{self.ROUTE_ID}.gate-release.json",
                f"{other}.superseded-20260904T000000Z.outcome.json",
                f"{self.ROUTE_ID}.some-future-sidecar.json",
                "notes.json",
                "rt-NOTHEX.json",
            ):
                (routes / name).write_text("{ not a route record", encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "")
            self.assertEqual([route_id for route_id, _ in results], [self.ROUTE_ID])

    def test_a_genuinely_unparsable_route_record_still_fails_closed(self):
        # The undecidable-is-in-use rule is the point of the scan and must
        # survive: only the SELECTION narrowed, not the judgement.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            (routes / f"{self.ROUTE_ID}.json").write_text("{ truncated", encoding="utf-8")
            results, reason = self._scan(base)
            self.assertTrue(reason.startswith("route-record-unparsable:"), reason)
            self.assertEqual(results, [])

    def test_an_unrecognised_open_route_record_is_never_skipped_silently(self):
        # Skipping a real route record removes a release's protection, and that
        # is the direction that deletes data. A name the reader does not know is
        # adjudicated by content, not waved through: its launch_home protects
        # that release exactly as a canonical record's does.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            legacy = routes / "2026-08-13_wwd-eval-labels.json"
            self._record(legacy, str(base / "other-release"))
            results, reason = self._scan(base)
            self.assertEqual(reason, "")
            self.assertEqual(sorted(results), sorted([
                (self.ROUTE_ID, str(base / "release")),
                (legacy.stem, str(base / "other-release")),
            ]))
            in_use, why = distribution._release_in_use(
                base / "other-release", [], (results, reason))
            self.assertEqual((in_use, why), (True, f"open-route:{legacy.stem}"))

    def test_an_unrecognised_undecidable_route_record_still_fails_closed(self):
        # A legacy-named record that carries a launch tuple but no usable
        # launch_home is as untrustworthy as a canonical one.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            legacy = routes / "2026-08-13_wwd-eval-labels.json"
            legacy.write_text(json.dumps({
                "route_id": "rt-1111111111111111", "schema_version": 2, "nodes": [],
                "launch_compatibility_tuple": {},
            }), encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, f"route-record-unparsable:{legacy}")
            self.assertEqual(results, [])

    def test_a_pre_tuple_open_record_names_no_release(self):
        # Measured 2026-09-23: four open alias-named records compiled before
        # launch identity was sealed (801a50c64) held nine releases, because the
        # scan failed on the first one's name. Such a record names no
        # compile-time launch_home under ANY name, so it protects nothing here
        # and must not retain every release.
        pre_tuple = {"schema_version": 2, "capability": "autopilot-code",
                     "effective_intensity": "standard", "nodes": [{"id": "plan"}]}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            for name, route_id in (
                ("2026-08-11_release_standard.json", "rt-e8ff9a17806ba943"),
                ("rt-de672bda79961d80.json", "rt-de672bda79961d80"),
            ):
                (routes / name).write_text(
                    json.dumps({**pre_tuple, "route_id": route_id}), encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "")
            self.assertEqual(results, [(self.ROUTE_ID, str(base / "release"))])
            self.assertEqual(
                distribution._release_in_use(base / "other-release", [], (results, reason)),
                (False, ""))

    def test_every_undecidable_record_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            broken = [routes / f"{self.ROUTE_ID}.json", routes / "2026-08-13_legacy.json"]
            broken[0].write_text("{ truncated", encoding="utf-8")
            broken[1].write_text(json.dumps({
                "route_id": "rt-1111111111111111", "nodes": [],
                "launch_compatibility_tuple": {"launch_home": None},
            }), encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(
                reason, "route-record-unparsable:" + "; ".join(sorted(map(str, broken))))
            self.assertEqual(results, [])

    def test_retention_message_names_the_evidence_kind(self):
        release = Path("/releases/v1")
        attempt = distribution._release_retention_message(release, "open-attempt:att-1")
        self.assertIn("is still referenced by a live dispatch attempt (open-attempt:att-1)", attempt)
        route = distribution._release_retention_message(release, "open-route:rt-1")
        self.assertIn("launch home of an open route record (open-route:rt-1)", route)
        self.assertNotIn("live dispatch attempt", route)
        unparsable = distribution._release_retention_message(
            release, "route-record-unparsable:/r/a.json; /r/b.json")
        self.assertIn("could not be proven unused (route-record-unparsable:/r/a.json; /r/b.json)", unparsable)
        self.assertNotIn("live dispatch attempt", unparsable)
        self.assertIn("capability-route.py close --route <file> --allow-unproven", unparsable)
        self.assertNotIn("capability-route.py close",
                         distribution._release_retention_message(release, "route-discovery-unreliable:scan-cap"))

    def test_a_closed_record_under_a_legacy_name_costs_nothing(self):
        # 79 such records exist on this machine, all closed. A closed record's
        # launch_home is stale by definition, so it must not poison the scan.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            legacy = routes / "2026-08-13_wwd-eval-labels.json"
            self._record(legacy, str(base / "other-release"))
            legacy.with_name(legacy.stem + ".outcome.json").write_text("{}", encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "")
            self.assertEqual([route_id for route_id, _ in results], [self.ROUTE_ID])


class ReleaseHeldByLiveProcessTest(unittest.TestCase):
    """A release a live process runs out of is in use, however it was launched.

    Measured 2026-09-06: three managed codex sessions had
    `AGENT_HOME=<releases/v2.110.1>` and had been alive two days, while jobs.log
    held only `done` rows for it and no activation named it. Restoring release
    pruning without this source would have deleted that tree out from under
    them. The scan bug had been shielding them since 2026-09-04.
    """

    def test_this_process_holds_its_own_interpreter_tree(self):
        # A positive case that needs no fixture process: this very interpreter's
        # cmdline names the test file, so the repo root is "held".
        held, why = distribution._release_held_by_live_process(
            Path(__file__).resolve().parents[2])
        self.assertTrue(held)
        self.assertTrue(why.startswith("live-process:"), why)

    def test_an_unrelated_tree_is_not_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            held, why = distribution._release_held_by_live_process(Path(tmp))
            self.assertFalse(held, why)

    def test_an_unreadable_process_does_not_mark_every_release_in_use(self):
        # Some of our own processes deny /proc entirely (`(sd-pam)`), and pid 1
        # belongs to another user. If either were treated as undecidable, every
        # release would be in use -- the exact bug this change repairs. They are
        # skipped and counted instead, so the scan still reaches a verdict.
        #
        # The uid filter is noise reduction, not a correctness gate: with
        # unreadable processes skipped, ownership changes no verdict. Saying so
        # here rather than pretending this test proves it.
        with tempfile.TemporaryDirectory() as tmp:
            held, why = distribution._release_held_by_live_process(Path(tmp))
        self.assertFalse(held, why)



class RouteStaleOpenMirrorTest(unittest.TestCase):
    """P2: an unclosed route whose attempts all finished must stop pinning.

    A release is retained by four reference sources. The registry source holds
    one only while a row is `open`. The route source held one until the route
    was **closed** -- and a route whose only attempt died is never closed,
    because nobody runs `close` on a route that failed to launch.

    Measured 2026-09-06 on this machine, before the fix: `rt-0319e7bd` pinned
    `v2.107.0` and `rt-1ccafd47` pinned `v2.109.1`, each with a single `done`
    row carrying a `dead-*` note, no completion marker, no live process, and no
    other reference source naming them. `open-route:` was the sole reason, and
    it would never have expired.

    The direction that deletes data is releasing a release that is still needed,
    so every unknown keeps the pin: no sealed registry, an unreadable one, a
    malformed row, or **no rows at all**.
    """

    ROUTE_ID = "rt-0319e7bdb6bede69"

    def _routes_dir(self, base: Path) -> Path:
        routes = base / ".agent_reports" / ".runtime" / "routes"
        routes.mkdir(parents=True)
        return routes

    def _record(self, path: Path, launch_home: str, jobs: str | None) -> None:
        tuple_: dict = {"launch_home": {"kind": "launch_home", "path": launch_home}}
        if jobs is not None:
            tuple_["jobs_path"] = {"kind": "jobs_path", "path": jobs}
        path.write_text(json.dumps({
            "route_id": path.stem, "schema_version": 2,
            "launch_compatibility_tuple": tuple_,
        }), encoding="utf-8")

    def _registry(self, path: Path, rows) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for status, meta in rows:
            lines.append("\t".join(
                ["2026-09-06T00:00:00Z", status, "repo", "worktree", "slug", meta]))
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _scan(self, base: Path, environ: dict | None = None):
        with mock.patch.object(
            distribution, "_open_route_artifact_roots",
            return_value=([str(base / ".agent_reports")], [], ""),
        ):
            # `{}` on purpose: `stable_state_root` reads only the passed
            # mapping, so an empty one resolves no veto registry and the suite
            # can never read the operator's live jobs.log.
            return distribution._open_route_launch_homes(environ or {})

    def _pins(self, rows, *, jobs="present", write_registry=True):
        """Return whether the route still pins its launch_home."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        jobs_path = base / "state" / "jobs.log"
        self._record(
            routes / f"{self.ROUTE_ID}.json", str(base / "release"),
            str(jobs_path) if jobs == "present" else jobs,
        )
        if write_registry:
            self._registry(jobs_path, rows)
        results, reason = self._scan(base)
        self.assertEqual(reason, "", "the scan itself must stay reliable")
        return [route_id for route_id, _ in results] == [self.ROUTE_ID]

    # -- the measured case ---------------------------------------------------

    def test_a_route_whose_only_attempt_died_stops_pinning(self):
        self.assertFalse(self._pins([
            ("done", f"route_id={self.ROUTE_ID},route_node=one-shot,"
                     "attempt_id=att-75b90f34764c4d62,"
                     "note=dead-launch-runtime-root-mismatch"),
        ]))

    def test_a_route_whose_attempt_completed_normally_also_stops_pinning(self):
        self.assertFalse(self._pins([
            ("done", f"route_id={self.ROUTE_ID},attempt_id=att-1,note=completed-marker"),
        ]))

    # -- everything that must keep the pin -----------------------------------

    def test_a_live_attempt_keeps_the_pin(self):
        for status in ("open", "running"):
            with self.subTest(status):
                self.assertTrue(self._pins([
                    ("done", f"route_id={self.ROUTE_ID},attempt_id=att-1"),
                    (status, f"route_id={self.ROUTE_ID},attempt_id=att-2"),
                ]), f"a {status} attempt is not finished work")

    def test_no_rows_at_all_keeps_the_pin(self):
        # Absence of evidence is not evidence of completion. A route compiled
        # but not yet launched has no rows -- and so does one whose rows were
        # pruned out from under it.
        self.assertTrue(self._pins([]))
        self.assertTrue(self._pins([
            ("done", "route_id=rt-someotherroute,attempt_id=att-x"),
        ]))

    def test_a_missing_or_unreadable_registry_keeps_the_pin(self):
        self.assertTrue(self._pins([], write_registry=False))
        self.assertTrue(self._pins([], jobs=None))
        self.assertTrue(self._pins([], jobs=""))

    def test_a_malformed_registry_row_keeps_the_pin(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        jobs_path = base / "state" / "jobs.log"
        self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"), str(jobs_path))
        jobs_path.parent.mkdir(parents=True)
        jobs_path.write_text(
            f"2026-09-06T00:00:00Z\tdone\trepo\tworktree\tslug\troute_id={self.ROUTE_ID}\n"
            "this row has too few fields\n", encoding="utf-8")
        results, reason = self._scan(base)
        self.assertEqual(reason, "")
        self.assertEqual([r for r, _ in results], [self.ROUTE_ID],
                         "a registry we cannot fully parse cannot certify completion")

    def test_a_closed_route_is_still_released_the_old_way(self):
        # The pre-existing rule must be untouched: an outcome sibling releases
        # the route even with a live row.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        jobs_path = base / "state" / "jobs.log"
        self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"), str(jobs_path))
        self._registry(jobs_path, [("open", f"route_id={self.ROUTE_ID},attempt_id=att-1")])
        (routes / f"{self.ROUTE_ID}.outcome.json").write_text("{}", encoding="utf-8")
        results, reason = self._scan(base)
        self.assertEqual(reason, "")
        self.assertEqual(results, [])

    # -- the two ways this could answer for the wrong route ------------------

    def test_only_the_routes_own_sealed_registry_can_declare_it_finished(self):
        # Defect C, B2: a registry this route never wrote to is not evidence
        # about it. Round 1 (M9): the first version of this test could not
        # falsify a union reader -- the sealed `open` row short-circuited before
        # any ambient row was read, so "sealed only" and "sealed union ambient"
        # gave the same verdict. The discriminating fixture is a sealed registry
        # with NO rows for the route and an ambient one that says `done`:
        # sealed-only keeps the pin, a union reader releases it.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        sealed = base / "sealed" / "jobs.log"
        ambient = base / "ambient" / "jobs.log"
        self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"), str(sealed))
        self._registry(sealed, [("done", "route_id=rt-1111111111111111,attempt_id=att-x")])
        self._registry(ambient, [("done", f"route_id={self.ROUTE_ID},attempt_id=att-1")])
        results, reason = self._scan(base, {"AGENT_DISPATCH_JOBS": str(ambient)})
        self.assertEqual(reason, "")
        self.assertEqual([r for r, _ in results], [self.ROUTE_ID],
                         "an ambient registry may not certify completion")

    def test_any_other_registry_may_still_veto_a_release(self):
        # The asymmetry that closes the alias hole (review 🟡6): only the sealed
        # registry may say "finished", but a live row ANYWHERE keeps the pin.
        # It can only ever be more conservative.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        sealed = base / "sealed" / "jobs.log"
        ambient = base / "ambient" / "jobs.log"
        self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"), str(sealed))
        self._registry(sealed, [("done", f"route_id={self.ROUTE_ID},attempt_id=att-1")])
        # sealed alone would release it
        self.assertEqual([r for r, _ in self._scan(base)[0]], [])
        self._registry(ambient, [("open", f"route_id={self.ROUTE_ID},attempt_id=att-2")])
        results, reason = self._scan(base, {"AGENT_DISPATCH_JOBS": str(ambient)})
        self.assertEqual(reason, "")
        self.assertEqual([r for r, _ in results], [self.ROUTE_ID])

    def test_a_live_depth1_owner_keeps_the_pin(self):
        # Round 1 (blocking 1): a depth-1 owner is not a route node -- its row
        # carries `owner_route_id` and never `route_id`. Matching only
        # `route_id=` made a live owner invisible, so a route whose node rows
        # were all terminal (the normal state between two serial stages, and the
        # whole of `code-report`) read as finished and dropped its pin while its
        # owner was still running. Measured on the live registry: 77 rows carry
        # `owner_route_id=` and no `route_id=`; 0 rows carry both.
        self.assertTrue(self._pins([
            ("done", f"route_id={self.ROUTE_ID},route_node=plan,attempt_id=att-1"),
            ("open", f"owner_route_id={self.ROUTE_ID},route_node=_owner,attempt_id=att-2"),
        ]), "a running owner is not finished work")

    def test_a_terminal_owner_row_alone_does_not_keep_the_pin(self):
        # The other half: `owner_route_id` must be read as this route's row for
        # BOTH verdicts, not only for keeping the pin.
        self.assertFalse(self._pins([
            ("done", f"owner_route_id={self.ROUTE_ID},route_node=_owner,attempt_id=att-2"),
        ]))

    def test_the_status_vocabulary_is_an_allowlist(self):
        # Round 1 (blocking 2): written first as a denylist of live states, so a
        # status word this reader does not know -- a partially written field, a
        # hand-repaired row, a word a future cycle adds the way killed/cancelled
        # were added before anything wrote them -- read as "this route is over".
        for status in ("queued", "", "DONE", "done-ish"):
            with self.subTest(status):
                self.assertTrue(self._pins([
                    (status, f"route_id={self.ROUTE_ID},attempt_id=att-1"),
                ]), f"unknown status {status!r} must keep the pin")
        for status in ("done", "killed", "cancelled"):
            with self.subTest(status):
                self.assertFalse(self._pins([
                    (status, f"route_id={self.ROUTE_ID},attempt_id=att-1"),
                ]), f"{status} is terminal in the rest of the tree")

    def test_a_longer_route_id_is_not_matched_by_its_own_prefix(self):
        # Round 1 (M8): the comment claimed two properties and the suite covered
        # one. `source_route_id=` was tested; prefix containment was not.
        self.assertTrue(self._pins([
            ("done", f"route_id={self.ROUTE_ID}ff,attempt_id=att-1"),
        ]))

    def test_a_record_whose_stem_and_body_disagree_keeps_the_pin(self):
        # Round 1 (🟡5): the caller reports `path.stem` as the route identity and
        # documents it as authoritative; reading `route_id` from the body was a
        # second source for that one value. 83 records on disk already disagree.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        jobs_path = base / "state" / "jobs.log"
        record = routes / f"{self.ROUTE_ID}.json"
        record.write_text(json.dumps({
            "route_id": "rt-1111111111111111", "schema_version": 2,
            "launch_compatibility_tuple": {
                "launch_home": {"kind": "launch_home", "path": str(base / "release")},
                "jobs_path": {"kind": "jobs_path", "path": str(jobs_path)},
            },
        }), encoding="utf-8")
        self._registry(jobs_path, [
            ("done", f"route_id={self.ROUTE_ID},attempt_id=att-1"),
            ("done", "route_id=rt-1111111111111111,attempt_id=att-2"),
        ])
        results, reason = self._scan(base)
        self.assertEqual(reason, "")
        self.assertEqual([r for r, _ in results], [self.ROUTE_ID])

    def test_terminality_never_rescues_a_record_that_names_no_launch_home(self):
        # Round 1 (M7): "an undecidable record stays undecidable" was tested only
        # for an UNPARSABLE record. The case where the ordering actually matters
        # is a record that parses, has a valid sealed registry with all-terminal
        # rows, and no `launch_home` -- `_UNDECIDABLE` (scan fails closed) versus
        # `None` (silently skipped, protection gone).
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        jobs_path = base / "state" / "jobs.log"
        (routes / f"{self.ROUTE_ID}.json").write_text(json.dumps({
            "route_id": self.ROUTE_ID, "schema_version": 2,
            "launch_compatibility_tuple": {
                "jobs_path": {"kind": "jobs_path", "path": str(jobs_path)},
            },
        }), encoding="utf-8")
        self._registry(jobs_path, [("done", f"route_id={self.ROUTE_ID},attempt_id=att-1")])
        results, reason = self._scan(base)
        self.assertTrue(reason.startswith("route-record-unparsable:"), reason)
        self.assertEqual(results, [])

    def test_a_lineage_field_is_not_this_routes_row(self):
        # `source_route_id=rt-x` contains `route_id=rt-x` as a substring. The
        # prefilter is allowed to match it; the confirmation must not.
        self.assertTrue(self._pins([
            ("done", f"source_route_id={self.ROUTE_ID},route_id=rt-1111111111111111,"
                     "attempt_id=att-1"),
        ]), "a continuation descendant's row says nothing about the ancestor")

    def test_an_undecidable_record_stays_undecidable(self):
        # Terminality is read from the record body, so it must never be able to
        # release a record we could not trust in the first place.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        routes = self._routes_dir(base)
        (routes / f"{self.ROUTE_ID}.json").write_text("{ truncated", encoding="utf-8")
        results, reason = self._scan(base)
        self.assertTrue(reason.startswith("route-record-unparsable:"), reason)
        self.assertEqual(results, [])

if __name__ == "__main__":
    unittest.main()
