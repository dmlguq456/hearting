#!/usr/bin/env python3
"""Release retention judges each release by its own evidence (2026-11-27).

`/` was full: 221 installed releases (23 GB) were kept because one uncertain
input kept every release -- a registry that moved while the global route scan
ran, one open route record under an unexpected name in another project, and
long-lived shell windows started through a runtime's `hearting` projection.
Every scene here is synthetic: fake names, a private temporary root.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import distribution  # noqa: E402

ROUTE_A = "rt-aaaaaaaaaaaaaaaa"
ROUTE_B = "rt-bbbbbbbbbbbbbbbb"
ROUTE_C = "rt-cccccccccccccccc"
HOUR = 3600.0


class PerReleaseRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="rr-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data" / "hearting"
        self.releases = self.data / "releases"
        self.releases.mkdir(parents=True)
        self.state = self.root / "state" / "hearting"
        self.jobs = self.state / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.write_text("", encoding="utf-8")
        self.project = self.root / "project" / ".agent_reports"
        self.other = self.root / "other-project" / ".agent_reports"
        environment = {
            "HARNESS_DATA_ROOT": str(self.data),
            "HARNESS_STATE_ROOT": str(self.state),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        self._row("done", f"artifact_root={self.project}")
        self._row("done", f"artifact_root={self.other}")

    # -- fixtures -----------------------------------------------------------
    def _release(self, name: str, order: int) -> Path:
        release = self.releases / name
        (release / "utilities").mkdir(parents=True)
        (release / "utilities" / "runner.py").write_text(
            "import sys\nprint('ready', flush=True)\nsys.stdin.readline()\n", encoding="utf-8")
        os.utime(release, (1_700_000_000 + order, 1_700_000_000 + order))
        return release

    def _family(self, *names: str) -> list[Path]:
        releases = [self._release(name, order) for order, name in enumerate(names)]
        (self.data / "current").symlink_to(releases[-1], target_is_directory=True)
        return releases

    def _row(self, status: str, metadata: str, jobs: Path | None = None) -> None:
        with (jobs or self.jobs).open("a", encoding="utf-8") as handle:
            handle.write("\t".join(["2026-11-27T00:00:00Z", status, "repo", "/wt", "slug",
                                    metadata]) + "\n")

    def _routes(self, artifact_root: Path) -> Path:
        routes = artifact_root / ".runtime" / "routes"
        routes.mkdir(parents=True, exist_ok=True)
        return routes

    def _route(self, artifact_root: Path, route_id: str, release: Path) -> Path:
        path = self._routes(artifact_root) / f"{route_id}.json"
        path.write_text(json.dumps({
            "route_id": route_id, "schema_version": 2,
            "launch_compatibility_tuple": {
                "contract_version": 1,
                "launch_home": {"kind": "launch_home", "path": str(release)},
                "jobs_path": {"kind": "jobs_path", "path": str(self.jobs)},
            },
            "nodes": [{"id": "plan", "kind": "pipeline-stage", "terminal": True}],
        }), encoding="utf-8")
        return path

    def _old_shape(self, path: Path, mention: str = "") -> None:
        # The 2026-11-27 shape: about 4 KB, `nodes` only, no launch tuple.
        nodes = [{"id": f"stage-{index}", "kind": "pipeline-stage",
                  "note": "x" * 120} for index in range(28)]
        if mention:
            nodes[0]["note"] = mention
        path.write_text(json.dumps({"schema_version": 1, "nodes": nodes}), encoding="utf-8")

    def _cleanup(self, keep: set[Path] | None = None) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            distribution._cleanup_releases(keep or set())
        return stderr.getvalue()

    def _started(self, argv: list[str], env: dict[str, str] | None = None):
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.root, env=env)
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.communicate, b"\n")
        # Read /proc only after the new program has started (its arguments and
        # environment are empty for a moment around exec).
        self.assertEqual(process.stdout.readline().strip(), b"ready")
        return process

    # -- (나) one unjudgeable record keeps only what it names ---------------
    def test_an_unrecognised_old_shape_record_elsewhere_keeps_only_what_it_names(self):
        unrelated, named, middle, newer, newest = self._family(
            "v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0", "v4.5.0")
        routes = self._routes(self.other)
        self._old_shape(routes / "2026-08-13_legacy-plan.json")
        self._old_shape(routes / "2026-08-14_legacy-held.json",
                        mention=f"resumed under {named}/utilities")
        # A canonical record `_route_record_launch_home` cannot decide.
        self._old_shape(routes / "rt-dddddddddddddddd.json")
        output = self._cleanup()
        self.assertFalse(unrelated.exists(), output)
        self.assertFalse(middle.exists(), output)
        self.assertTrue(named.is_dir(), output)
        self.assertIn("open-route:2026-08-14_legacy-held", output)
        self.assertTrue(newer.is_dir() and newest.is_dir())

    # -- (가) a registry that moves during the scan keeps nothing extra -----
    def test_registry_appends_during_the_scan_do_not_keep_every_release(self):
        unrelated, routed, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        self._route(self.project, ROUTE_A, routed)
        self._row("open", f"route_id={ROUTE_A},attempt_id=att-live")
        real_names = distribution._installed_release_names
        scans = []

        def busy_host():
            # A worker appends its heartbeat row while the scan runs.
            scans.append(1)
            self._row("running", f"attempt_id=att-{len(scans)},launch_home={newest}")
            return real_names()

        with mock.patch.object(distribution, "_installed_release_names", side_effect=busy_host):
            output = self._cleanup()
        self.assertGreaterEqual(len(scans), 1)
        self.assertFalse(unrelated.exists(), output)
        self.assertTrue(routed.is_dir(), output)
        self.assertNotIn("route-discovery-unstable", output)

    def test_a_settled_route_that_reopens_after_the_scan_keeps_its_release(self):
        reopened, unrelated, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        self._route(self.project, ROUTE_B, reopened)
        self._row("done", f"route_id={ROUTE_B},attempt_id=att-first")
        real_paths = distribution._dispatch_cleanup_boundary_paths
        appended = []

        def retry_lands(*args, **kwargs):
            # After the scan settled ROUTE_B, before the boundary is taken.
            if not appended:
                appended.append(1)
                self._row("open", f"route_id={ROUTE_B},attempt_id=att-retry")
            return real_paths(*args, **kwargs)

        with mock.patch.object(distribution, "_dispatch_cleanup_boundary_paths",
                               side_effect=retry_lands):
            output = self._cleanup()
        self.assertTrue(appended)
        self.assertTrue(reopened.is_dir(), output)
        self.assertIn(f"open-route:{ROUTE_B}", output)
        self.assertFalse(unrelated.exists(), output)

    def test_a_route_published_after_the_scan_keeps_its_release(self):
        late, unrelated, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        self._routes(self.project)
        real_paths = distribution._dispatch_cleanup_boundary_paths
        published = []

        def publish(*args, **kwargs):
            if not published:
                published.append(self._route(self.project, ROUTE_C, late))
            return real_paths(*args, **kwargs)

        with mock.patch.object(distribution, "_dispatch_cleanup_boundary_paths",
                               side_effect=publish):
            output = self._cleanup()
        self.assertTrue(published)
        self.assertTrue(late.is_dir(), output)
        self.assertIn(f"open-route:{ROUTE_C}", output)
        self.assertFalse(unrelated.exists(), output)

    # -- (나′) a process that holds a release only through its environment --
    def test_a_process_holding_a_release_only_in_its_environment_keeps_it(self):
        held, unrelated, newer, newest = self._family("v3.35.48", "v3.36.0", "v4.0.0", "v4.1.0")
        env = {"PATH": os.environ.get("PATH", ""), "AGENT_RUNTIME_ROOT": str(held)}
        process = self._started(
            [sys.executable, "-c",
             "import sys; print('ready', flush=True); sys.stdin.readline()"], env)
        self.assertEqual(distribution._release_held_by_live_process(held),
                         (True, f"live-process:{process.pid}"))
        output = self._cleanup()
        self.assertTrue(held.is_dir(), output)
        self.assertFalse(unrelated.exists(), output)

    # -- (다) a shell window started through a runtime projection ----------
    def _bundles(self, releases: list[Path], start: float) -> dict[Path, float]:
        """One linked claude bundle per release, an hour apart; returns their times."""
        bundles = self.root / "claude" / ".harness" / "bundles"
        times = {}
        for index, release in enumerate(releases):
            when = start + index * HOUR
            bundle = bundles / f"release-{release.name}-000000000000-000000000000"
            bundle.mkdir(parents=True)
            (bundle / "source").symlink_to(release, target_is_directory=True)
            (bundle / "bundle.json").write_text(json.dumps({
                "schema": 2, "runtime": "claude", "source_root": str(release),
                "source_revision": f"release:{release.name}:000000000000",
                "source_link": True, "checksum": "0" * 64, "external_dependencies": [],
                "created_at": dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(),
            }), encoding="utf-8")
            times[release] = when
        projection = self.root / "claude" / "hearting"
        projection.symlink_to(bundles / f"release-{releases[-1].name}-000000000000-000000000000"
                              / "source", target_is_directory=True)
        moved = start + (len(releases) - 1) * HOUR
        os.utime(projection, (moved, moved), follow_symlinks=False)
        os.utime(self.data / "current", (moved, moved), follow_symlinks=False)
        return times

    def test_a_long_lived_shell_window_keeps_only_the_release_it_started_under(self):
        names = ["v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0", "v4.44.0", "v4.45.0", "v4.46.0"]
        family = self._family(*names)
        start = 1_790_000_000.0
        times = self._bundles(family, start)
        # Started ten minutes after v4.42.0 became the projection target.
        started_at = times[family[2]] + 600
        window = self._started([sys.executable,
                                str(self.root / "claude" / "hearting" / "utilities" / "runner.py")])
        real_started = distribution._process_started_at

        def started(entry):
            return started_at if entry.name == str(window.pid) else real_started(entry)

        with mock.patch.object(distribution, "_process_started_at", side_effect=started):
            held = {release.name: distribution._release_held_by_live_process(release)[0]
                    for release in family}
            output = self._cleanup()
        # The release it started under and one on each side.
        self.assertEqual(held, {
            "v4.40.0": False, "v4.41.0": True, "v4.42.0": True, "v4.43.0": True,
            "v4.44.0": False, "v4.45.0": False, "v4.46.0": False,
        })
        for name in ("v4.41.0", "v4.42.0", "v4.43.0", "v4.45.0", "v4.46.0"):
            self.assertTrue((self.releases / name).is_dir(), f"{name}: {output}")
        for name in ("v4.40.0", "v4.44.0"):
            self.assertFalse((self.releases / name).exists(), f"{name}: {output}")
        # Their dead linked bundles went with them; every other bundle stays.
        bundles = self.root / "claude" / ".harness" / "bundles"
        self.assertEqual(sorted(path.name.split("-")[1] for path in bundles.iterdir()),
                         ["v4.41.0", "v4.42.0", "v4.43.0", "v4.45.0", "v4.46.0"])

    def test_a_start_older_than_every_activation_still_keeps_the_family(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0")
        times = self._bundles(family, 1_790_000_000.0)
        window = self._started([sys.executable,
                                str(self.root / "claude" / "hearting" / "utilities" / "runner.py")])
        real_started = distribution._process_started_at

        def started(entry):
            if entry.name == str(window.pid):
                return times[family[0]] - HOUR
            return real_started(entry)

        with mock.patch.object(distribution, "_process_started_at", side_effect=started):
            self.assertEqual(distribution._release_held_by_live_process(family[0]),
                             (True, f"live-process:{window.pid}:unsealed-projection"))

    def test_a_release_the_history_cannot_place_stays_with_old_windows(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0")
        times = self._bundles(family, 1_790_000_000.0)
        unplaced = self._release("v4.39.9-hand-made", 10)
        window = self._started([sys.executable,
                                str(self.root / "claude" / "hearting" / "utilities" / "runner.py")])
        real_started = distribution._process_started_at

        def started(entry):
            if entry.name == str(window.pid):
                return times[family[1]] + 60 + 1
            return real_started(entry)

        with mock.patch.object(distribution, "_process_started_at", side_effect=started):
            self.assertTrue(distribution._release_held_by_live_process(unplaced)[0])
            self.assertFalse(distribution._release_held_by_live_process(family[3])[0])

    def test_a_proc_self_argument_is_not_a_projection(self):
        # dispatch passes `--worktree /proc/self/fd/5`; resolved by the scanner
        # it named the scanner's own lock inside a release.
        held, newest = self._family("v4.1.0", "v4.2.0")
        lock = held / ".dispatch" / "jobs.log.lock"
        lock.parent.mkdir()
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        info = os.fstat(descriptor)
        process = self._started([sys.executable, "-c",
                                 "import sys; print('ready', flush=True); sys.stdin.readline()",
                                 "--worktree", f"/proc/self/fd/{descriptor}"])
        with mock.patch.object(distribution, "_registry_lock_owned_fds",
                               return_value={descriptor: (info.st_dev, info.st_ino)}):
            self.assertEqual(distribution._release_held_by_live_process(held), (False, ""),
                             f"pid {process.pid}")


class DeadReleaseBundleTest(unittest.TestCase):
    def test_only_dead_linked_bundles_of_managed_releases_go(self):
        with tempfile.TemporaryDirectory(prefix="rb-") as tmp:
            root = Path(tmp).resolve()
            releases = root / "data" / "hearting" / "releases"
            live = releases / "v4.2.0"
            live.mkdir(parents=True)
            gone = releases / "v4.1.0"
            homes = {"claude": root / "claude", "codex": root / "codex",
                     "opencode": root / "config" / "opencode"}

            def bundle(home: Path, runtime: str, release: Path, **changes) -> Path:
                path = home / ".harness" / "bundles" / f"release-{release.name}-k-k"
                path.mkdir(parents=True)
                (path / "source").symlink_to(release, target_is_directory=True)
                metadata = {"schema": 2, "runtime": runtime, "source_root": str(release),
                            "source_revision": f"release:{release.name}:k", "source_link": True,
                            "checksum": "0" * 64, "created_at": "2026-11-27T00:00:00+00:00"}
                metadata.update(changes)
                (path / "bundle.json").write_text(json.dumps(metadata), encoding="utf-8")
                return path

            dead = {runtime: bundle(home, runtime, gone) for runtime, home in homes.items()}
            kept_live = bundle(homes["claude"], "claude", live)
            copied = homes["codex"] / ".harness" / "bundles" / "release-v4.0.0-c-c"
            (copied / "source").mkdir(parents=True)
            (copied / "bundle.json").write_text(json.dumps({
                "runtime": "codex", "source_link": False, "source_root": str(releases / "v4.0.0"),
                "source_revision": "release:v4.0.0:c"}), encoding="utf-8")
            foreign = bundle(homes["opencode"], "opencode", releases / "v3.0.0")
            (foreign / "notes.txt").write_text("someone's file", encoding="utf-8")
            unmarked = bundle(homes["codex"], "codex", releases / "v3.9.0", source_link=False)
            projected = bundle(homes["opencode"], "opencode", releases / "v3.8.0")
            (homes["opencode"] / "hearting").symlink_to(projected / "source")
            environment = {
                "HARNESS_DATA_ROOT": str(root / "data" / "hearting"),
                "CLAUDE_CONFIG_DIR": str(homes["claude"]), "CODEX_HOME": str(homes["codex"]),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            with mock.patch.dict(os.environ, environment):
                removed = distribution._remove_dead_release_bundles()
            self.assertEqual(sorted(removed), sorted(dead.values()))
            for path in dead.values():
                self.assertFalse(path.exists())
            for path in (kept_live, copied, foreign, unmarked, projected):
                self.assertTrue(path.is_dir(), path)


if __name__ == "__main__":
    unittest.main()
