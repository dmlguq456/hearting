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
        self._open: set[int] = set()

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
        self.addCleanup(self._stop, process)
        # Read /proc only after the new program has started (its arguments and
        # environment are empty for a moment around exec).
        self.assertEqual(process.stdout.readline().strip(), b"ready")
        return process

    @staticmethod
    def _stop(process) -> None:
        if process.poll() is None:
            try:
                process.communicate(b"\n", timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

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
        real_roots = distribution._open_route_artifact_roots
        scans = []

        def busy_host(*args, **kwargs):
            # A worker appends its heartbeat row while the scan runs (only
            # then: under the boundary a writer waits for the jobs lock).
            scans.append(1)
            if len(scans) == 1:
                self._row("running", f"attempt_id=att-1,launch_home={newest}")
            return real_roots(*args, **kwargs)

        with mock.patch.object(distribution, "_open_route_artifact_roots", side_effect=busy_host):
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

    def _late_route(self, *, routes_existed: bool) -> None:
        late, unrelated, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        if routes_existed:
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

    def test_a_route_published_after_the_scan_keeps_its_release(self):
        self._late_route(routes_existed=True)

    def test_a_route_published_in_a_routes_directory_made_after_the_scan(self):
        self._late_route(routes_existed=False)

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

    # -- 🔴1 a record a writer named first and filled later --------------------
    def _route_body(self, route_id: str, release: Path) -> bytes:
        return json.dumps({
            "route_id": route_id, "schema_version": 2,
            "launch_compatibility_tuple": {
                "contract_version": 1,
                "launch_home": {"kind": "launch_home", "path": str(release)},
                "jobs_path": {"kind": "jobs_path", "path": str(self.jobs)},
            },
            "nodes": [{"id": "plan", "kind": "pipeline-stage", "terminal": True}],
        }).encode("utf-8")

    def _named_first(self, path: Path) -> int:
        """Create `path` the way route writers do: the name first, `O_EXCL`, empty."""
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.addCleanup(lambda: os.close(descriptor) if descriptor in self._open else None)
        self._open.add(descriptor)
        return descriptor

    def _fill(self, descriptor: int, body: bytes) -> None:
        if descriptor in self._open:
            os.write(descriptor, body)
            os.close(descriptor)
            self._open.discard(descriptor)

    def test_a_record_filled_after_the_scan_read_it_empty_keeps_its_release(self):
        named, other, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        record = self._routes(self.project) / f"{ROUTE_A}.json"
        descriptor = self._named_first(record)
        inode = os.fstat(descriptor).st_ino
        real_paths = distribution._dispatch_cleanup_boundary_paths

        def writer_finishes(*args, **kwargs):
            # After the scan read the empty name, before the first boundary.
            self._fill(descriptor, self._route_body(ROUTE_A, other))
            return real_paths(*args, **kwargs)

        with mock.patch.object(distribution, "_dispatch_cleanup_boundary_paths",
                               side_effect=writer_finishes):
            output = self._cleanup()
        self.assertEqual(os.stat(record).st_ino, inode, "filled in place, not replaced")
        self.assertTrue(other.is_dir(), output)
        self.assertIn(f"open-route:{ROUTE_A}", output)
        self.assertFalse(named.exists(), output)

    def test_a_record_filled_after_it_was_read_under_the_boundary_keeps_its_release(self):
        named, target, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        routes = self._routes(self.project)
        record = routes / f"{ROUTE_A}.json"
        state: dict = {}
        real_paths = distribution._dispatch_cleanup_boundary_paths
        real_carry = distribution._succeed_dispatch_state

        def writer_names(*args, **kwargs):
            # The name appears after the scan; the boundary reads it empty.
            if "descriptor" not in state:
                state["descriptor"] = self._named_first(record)
            return real_paths(*args, **kwargs)

        def writer_finishes(candidate, **kwargs):
            # Filled after it was read, before the final check.
            self._fill(state["descriptor"], self._route_body(ROUTE_A, target))
            return real_carry(candidate, **kwargs)

        with mock.patch.object(distribution, "_dispatch_cleanup_boundary_paths",
                               side_effect=writer_names), \
                mock.patch.object(distribution, "_succeed_dispatch_state",
                                  side_effect=writer_finishes):
            output = self._cleanup()
        self.assertTrue(target.is_dir(), output)
        self.assertIn(f"open-route:{ROUTE_A}", output)
        self.assertFalse(named.exists(), output)

    # -- 🔴2 a read that fails leaves the observation as it was ---------------
    def test_a_directory_whose_read_failed_is_read_again_for_the_next_release(self):
        named, first, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        routes = self._routes(self.project)
        real_paths = distribution._dispatch_cleanup_boundary_paths
        published = []

        def publish(*args, **kwargs):
            if not published:
                published.append(self._route(self.project, ROUTE_B, named))
                (routes / "rt-eeeeeeeeeeeeeeee.json").write_bytes(b"x" * 5000)
            return real_paths(*args, **kwargs)

        with mock.patch.object(distribution, "_ROUTE_RECORD_SCAN_MAX_BYTES", 4096), \
                mock.patch.object(distribution, "_dispatch_cleanup_boundary_paths",
                                  side_effect=publish):
            output = self._cleanup()
        # The unbounded record keeps every release it might name, the second
        # candidate included: its directory is listed again, not skipped.
        self.assertTrue(first.is_dir(), output)
        self.assertTrue(named.is_dir(), output)
        self.assertEqual(output.count("route-record-unreadable:"), 2, output)

    def test_an_explicit_route_whose_read_failed_is_read_again_for_the_next_release(self):
        named, first, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        elsewhere = self.root / "elsewhere" / f"{ROUTE_C}.json"
        elsewhere.parent.mkdir()
        bindings = self.state / "dispatch" / "owner-route-bindings"
        real_paths = distribution._dispatch_cleanup_boundary_paths
        calls = []

        def publish(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                elsewhere.write_bytes(b"x" * 5000)
                bindings.mkdir(parents=True)
                (bindings / "owner.json").write_text(
                    json.dumps({"route_file": str(elsewhere)}), encoding="utf-8")
            elif len(calls) == 2:
                # The same file, rewritten in place: its directory does not move.
                with elsewhere.open("r+b") as handle:
                    handle.truncate(0)
                    handle.write(self._route_body(ROUTE_C, named))
            return real_paths(*args, **kwargs)

        with mock.patch.object(distribution, "_ROUTE_RECORD_SCAN_MAX_BYTES", 4096), \
                mock.patch.object(distribution, "_dispatch_cleanup_boundary_paths",
                                  side_effect=publish):
            output = self._cleanup()
        self.assertTrue(first.is_dir(), output)
        self.assertIn("route-record-unreadable:", output)
        self.assertTrue(named.is_dir(), output)
        self.assertIn(f"open-route:{ROUTE_C}", output)

    # -- 🔴3 an attestation reference appended while it was being read --------
    def test_an_attestation_reference_appended_after_its_read_keeps_its_release(self):
        import importlib.util
        named, attested, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        refs = self.state / "dispatch" / "attestation-refs.jsonl"
        refs.write_text("", encoding="utf-8")
        attestation = self.root / "smoke.json"
        attestation.write_text("{}", encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "smoke_writer", Path(__file__).resolve().parents[1] / "smoke-attestation.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        real_snapshot = distribution._attestation_refs_snapshot
        reads = []

        def read_then_writer_appends(environ):
            snapshot = real_snapshot(environ)
            reads.append(1)
            if len(reads) == 2:
                # The smoke writer takes no jobs lock: it lands right after the
                # first boundary's read.
                smoke._record_release_input_refs(
                    attestation, [{"path": str(attested / "utilities" / "runner.py")}],
                    os.environ)
            return snapshot

        with mock.patch.object(distribution, "_attestation_refs_snapshot",
                               side_effect=read_then_writer_appends):
            output = self._cleanup()
        self.assertIn(str(attested), refs.read_text(encoding="utf-8"))
        self.assertTrue(attested.is_dir(), output)
        self.assertIn(f"attested-input:{attestation}", output)
        self.assertFalse(named.exists(), output)

    # -- 🔴4 what an undecidable record spells, and whose names count ---------
    def test_an_escaped_release_name_past_one_mebibyte_still_holds(self):
        unrelated, escaped, broken, newer, newest = self._family(
            "v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0", "v4.5.0")
        routes = self._routes(self.project)
        spelled = str(self.releases) + "/" + escaped.name.replace(".", "\\u002e")
        record = json.dumps({
            "route_id": ROUTE_A, "schema_version": 2,
            "launch_compatibility_tuple": {"contract_version": 1, "launch_home": {
                "kind": "launch_home", "path": "@@"}},
            "nodes": [{"id": "plan", "note": "x" * 1_048_576}],
        }).replace('"@@"', '"' + spelled + '"')
        (routes / f"{ROUTE_A}.json").write_text(record, encoding="utf-8")
        self.assertGreater(len(record), distribution._ROUTE_RECORD_MAX_BYTES)
        self.assertNotIn(escaped.name, record)
        # A broken record under another name spells its release escaped too.
        (routes / "2026-08-15_broken.json").write_text(
            '{"nodes": [{"note": "resumed under ' + broken.name.replace(".", "\\u002e")
            + '"', encoding="utf-8")
        output = self._cleanup()
        self.assertTrue(escaped.is_dir(), output)
        self.assertIn(f"open-route:{ROUTE_A}", output)
        self.assertTrue(broken.is_dir(), output)
        self.assertIn("open-route:2026-08-15_broken", output)
        self.assertFalse(unrelated.exists(), output)

    def test_release_names_come_from_the_listing_that_found_the_candidates(self):
        unrelated, named, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0", "v4.4.0")
        self._old_shape(self._routes(self.other) / "2026-08-14_legacy-held.json",
                        mention=f"resumed under {named.name}")
        # A second listing that comes back empty must not empty every record.
        with mock.patch.object(distribution, "_installed_release_names", return_value={}):
            output = self._cleanup()
        self.assertTrue(named.is_dir(), output)
        self.assertFalse(unrelated.exists(), output)

    def test_a_release_listing_that_fails_keeps_the_single_candidate(self):
        unrelated, newer, newest = self._family("v4.1.0", "v4.2.0", "v4.3.0")
        with mock.patch.object(distribution, "_installed_release_names", return_value=None):
            removed, why = distribution._prune_release_under_writer_boundary(unrelated)
        self.assertEqual((removed, why), (False, "route-discovery-unreliable:release-names"))
        self.assertTrue(unrelated.is_dir())

    # -- 🔴5 a process without its descriptor, placed only by exact times -----
    def _project(self, release: Path, runtime_home: Path | None = None,
                 when: float | None = None) -> Path:
        """Point a runtime's `hearting` link at `release` through a linked bundle.

        `when` writes the activation's times as the first round read them: the
        bundle's `created_at` and the link's mtime (its ctime stays real).
        """
        home = runtime_home or self.root / "claude"
        bundle = home / ".harness" / "bundles" / f"release-{release.name}-000000000000-000000000000"
        if not bundle.exists():
            bundle.mkdir(parents=True)
            (bundle / "source").symlink_to(release, target_is_directory=True)
            if when is not None:
                (bundle / "bundle.json").write_text(json.dumps({
                    "schema": 2, "runtime": home.name, "source_root": str(release),
                    "source_revision": f"release:{release.name}:000000000000",
                    "source_link": True, "checksum": "0" * 64,
                    "created_at": dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(),
                }), encoding="utf-8")
        link = home / "hearting"
        staged = link.with_name("hearting.next")
        staged.symlink_to(bundle / "source", target_is_directory=True)
        # destructive-ok: reason=move this test's synthetic runtime link the way activation does; boundary=one hearting symlink under this test's temporary root
        os.replace(staged, link)
        if when is not None:
            os.utime(link, (when, when), follow_symlinks=False)
        return link

    def _window(self, link: Path):
        return self._started([sys.executable, str(link / "utilities" / "runner.py")])

    def _held_at(self, process, started_at: float, releases: list[Path]) -> dict[str, bool]:
        real_started = distribution._process_started_at

        def started(entry):
            return started_at if entry.name == str(process.pid) else real_started(entry)

        with mock.patch.object(distribution, "_process_started_at", side_effect=started):
            return {release.name: distribution._release_held_by_live_process(release)[0]
                    for release in releases}

    # The three scenes of review 2026-11-27: each lost the release an open
    # window started under, by placing it on the activation history.
    T0 = 1_790_000_000.0

    def test_a_window_started_while_another_harness_moved_keeps_its_release(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0", "v4.44.0")
        a, b, c, d, e = family
        link = self._project(a, when=self.T0)
        for hour, release in ((1, b), (2, c), (3, d)):
            self._project(release, self.root / "codex", when=self.T0 + hour * HOUR)
        window = self._window(link)
        self._project(e, when=self.T0 + 4 * HOUR)
        held = self._held_at(window, self.T0 + 3.5 * HOUR, family)
        self.assertTrue(held[a.name], held)

    def test_a_window_started_while_its_link_was_moved_back_keeps_its_release(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0", "v4.44.0")
        a, b, c, d, e = family
        for hour, release in ((0, a), (1, b), (2, c), (3, d), (4, a)):
            link = self._project(release, when=self.T0 + hour * HOUR)
        window = self._window(link)
        self._project(e, when=self.T0 + 5 * HOUR)
        held = self._held_at(window, self.T0 + 4.5 * HOUR, family)
        self.assertTrue(held[a.name], held)

    def test_a_last_move_whose_mtime_lies_in_the_past_does_not_place_a_window(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0")
        old, mid, under, moved_to = family
        link = self._project(under, when=self.T0)
        window = self._window(link)
        # Moved after the window started, but the link's mtime says two hours
        # before it: only the rename's ctime is when it moved.
        self._project(moved_to, when=self.T0 + HOUR)
        held = self._held_at(window, self.T0 + 2 * HOUR, family)
        self.assertTrue(held[under.name], held)
        self.assertTrue(all(held.values()), "an unplaced window keeps the family")

    def test_a_window_started_after_its_link_last_moved_holds_only_that_target(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0")
        link = self._project(family[2])
        launcher = self.root / "bin" / "tool"
        launcher.parent.mkdir()
        launcher.symlink_to(self.data / "current" / "utilities" / "runner.py")
        after = max(distribution._pointer_moved_at(link),
                    distribution._pointer_moved_at(self.data / "current"))
        after += distribution._POINTER_ROTATION_MARGIN_SECONDS + 1
        window = self._window(link)
        self.assertEqual(self._held_at(window, after, family),
                         {"v4.40.0": False, "v4.41.0": False, "v4.42.0": True, "v4.43.0": False})
        self._stop(window)
        launched = self._started([sys.executable, str(launcher)])
        self.assertEqual(self._held_at(launched, after, family),
                         {"v4.40.0": False, "v4.41.0": False, "v4.42.0": False, "v4.43.0": True})

    # -- 🔴5 the shell window, its host and the rewake hook show their release --
    def _install_shell(self, release: Path) -> None:
        source = Path(__file__).resolve().parents[2]
        before = release.stat()
        for relative in ("utilities/session-shell/entry.py", "utilities/session-shell/host.cjs",
                         "utilities/session_shell.py", "hooks/peer-steward-rewake.py"):
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / relative).read_bytes())
        for directory in (release, release / "utilities", release / "hooks"):
            os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns))

    def _loaded(self, module: Path):
        """A process that imports `module` from the path given, as its launcher would."""
        return self._started([
            sys.executable, "-c",
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('launched', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            "print('ready', flush=True); sys.stdin.readline()\n",
            str(module)])

    def _hosted(self, host: Path):
        import shutil
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        preload = self.root / "hold.cjs"
        # Stop the host where it would leave (no terminal here) and stay up.
        preload.write_text(
            "const leave = process.exit.bind(process);\n"
            "process.exit = () => { throw new Error('held by the test'); };\n"
            "process.on('uncaughtException', () => process.stdout.write('ready\\n'));\n"
            "process.stdin.on('data', () => leave(0));\n", encoding="utf-8")
        return self._started([node, "--require", str(preload), str(host), "claude", "native"])

    def test_the_shell_window_host_and_rewake_hook_hold_their_release_by_descriptor(self):
        family = self._family("v4.40.0", "v4.41.0", "v4.42.0", "v4.43.0", "v4.44.0")
        started_under = family[0]
        self._install_shell(started_under)
        link = self._project(started_under)
        hearting = self.root / "claude" / "hearting"
        for name, launch in (
                ("entry.py", lambda: self._loaded(hearting / "utilities/session-shell/entry.py")),
                ("host.cjs", lambda: self._hosted(hearting / "utilities/session-shell/host.cjs")),
                ("peer-steward-rewake.py",
                 lambda: self._loaded(hearting / "hooks/peer-steward-rewake.py"))):
            with self.subTest(name):
                self._project(started_under)
                process = launch()
                # An update moves the link after the window started.
                self._project(family[-1])
                self.assertEqual(distribution._release_held_by_live_process(started_under),
                                 (True, f"live-process:{process.pid}"))
                self.assertEqual(distribution._release_held_by_live_process(family[1]),
                                 (False, ""))
                self._stop(process)
        self._project(started_under)
        window = self._loaded(hearting / "utilities/session-shell/entry.py")
        self._project(family[-1])
        output = self._cleanup()
        self.assertTrue(started_under.is_dir(), output)
        self.assertIn(f"live-process:{window.pid}", output)
        self.assertFalse(family[1].exists(), output)
        self.assertFalse(family[2].exists(), output)
        self.assertTrue(link.is_symlink())

    def test_the_native_harness_never_inherits_the_window_descriptor(self):
        # Outside a terminal the window execs the native harness at once; the
        # descriptor closes on exec and the argv reaches it unchanged.
        checkout = Path(__file__).resolve().parents[2]
        native = self.root / "native"
        native.write_text(
            "#!" + sys.executable + "\nimport json, os, sys\n"
            "fds = {}\n"
            "for name in os.listdir('/proc/self/fd'):\n"
            "    try: fds[name] = os.readlink('/proc/self/fd/' + name)\n"
            "    except OSError: pass\n"
            "print(json.dumps({'argv': sys.argv[1:], 'fds': fds}))\n", encoding="utf-8")
        native.chmod(0o700)
        result = subprocess.run(
            [sys.executable, str(checkout / "utilities/session-shell/entry.py"), "claude",
             str(native), "a b", "--native-flag"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=True)
        seen = json.loads(result.stdout)
        self.assertEqual(seen["argv"], ["a b", "--native-flag"])
        self.assertNotIn(str(checkout), seen["fds"].values())

    # -- 🔴6 a descriptor the process names, unreadable while it lives --------
    def test_an_unreadable_named_descriptor_of_a_live_process_keeps_the_release(self):
        held, newest = self._family("v4.1.0", "v4.2.0")
        descriptor = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
        try:
            process = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; print('ready', flush=True); sys.stdin.readline()",
                 "--worktree", f"/proc/self/fd/{descriptor}"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(descriptor,), cwd=self.root, env={"PATH": os.environ.get("PATH", "")})
        finally:
            # Only the child holds the release; this scanner must not.
            os.close(descriptor)
        self.addCleanup(process.wait, 5)
        self.assertEqual(process.stdout.readline().strip(), b"ready")
        named = f"/proc/{process.pid}/fd/{descriptor}"
        real_readlink = os.readlink

        def denied(path, *args, **kwargs):
            if str(path) == named:
                raise PermissionError(13, "fixture: descriptor not readable", str(path))
            return real_readlink(path, *args, **kwargs)

        self.assertEqual(distribution._release_held_by_live_process(held),
                         (True, f"live-process:{process.pid}"))
        with mock.patch.object(distribution.os, "readlink", side_effect=denied):
            self.assertEqual(distribution._release_held_by_live_process(held),
                             (True, f"live-process:{process.pid}:descriptor-unreadable"))
        process.communicate(b"\n", timeout=5)
        with mock.patch.object(distribution.os, "readlink", side_effect=denied):
            self.assertEqual(distribution._release_held_by_live_process(held), (False, ""))

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

if __name__ == "__main__":
    unittest.main()
