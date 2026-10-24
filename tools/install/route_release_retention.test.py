#!/usr/bin/env python3
"""Exercise route retention through one isolated managed-release update."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "tools" / "install"))
import fixture_env  # noqa: E402


class RouteReleaseRetentionConsumerTest(unittest.TestCase):
    _source_tar_cache: dict[str, bytes] = {}

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source_root = Path(os.environ.get("HEARTING_TEST_SOURCE_ROOT", ROOT)).resolve()
        self.env = fixture_env.build_environment(
            self.base / "fixture", ROOT,
            base={"PATH": os.environ.get("PATH", "")},
        )
        self.env.update({
            "HARNESS_ALLOW_FILE_RELEASES": "1",
            "HARNESS_SCHEDULER_NO_ACTIVATE": "1",
            "HARNESS_TEST_PLATFORM": "linux",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.env.pop("HEARTING_TEST_SOURCE_ROOT", None)
        vendor = self.base / "vendor-bin"
        vendor.mkdir()
        codex = vendor / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o755)
        self.env["PATH"] = str(vendor) + os.pathsep + self.env["PATH"]
        fixture_env.prepare_environment(self.env)

    def _git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], check=True, capture_output=True,
        ).stdout

    def _add_marker(self, bundle: tarfile.TarFile, version: str) -> None:
        payload = (version + "\n").encode()
        info = tarfile.TarInfo("hearting/RELEASE_VERSION")
        info.mode = 0o644
        info.size = len(payload)
        bundle.addfile(info, BytesIO(payload))

    def _archive(self, version: str, *, ref: str | None = None) -> tuple[Path, str]:
        assets = self.base / "assets"
        assets.mkdir(exist_ok=True)
        archive = assets / f"{version}.tar.gz"
        source_ref = ref or "HEAD"
        overlays = (
            () if ref is not None else (
                "core/OPERATIONS.md",
                "tools/install/distribution.py",
                "tools/install/test_distribution_safe.py",
                "tools/install/deletion_safety.test.py",
                "tools/install/route_release_retention.test.py",
            )
        )
        overlay_names = {"hearting/" + name for name in overlays}
        with tarfile.open(archive, mode="w:gz") as bundle:
            if self.source_root != ROOT:
                excluded = {".git", ".dispatch", ".runtime", ".agent_reports",
                            ".claude_reports", ".core-grounding", ".spec-grounding",
                            ".capability-grounding", ".route-grounding", "__pycache__"}
                for source_file in self.source_root.rglob("*"):
                    relative = source_file.relative_to(self.source_root)
                    if excluded.intersection(relative.parts) or relative.as_posix() in {
                        "RELEASE_VERSION", ".hearting-release.json",
                    } or any(part.endswith(".pyc") for part in relative.parts):
                        continue
                    if source_file.is_file() or source_file.is_symlink():
                        bundle.add(source_file, arcname="hearting/" + relative.as_posix(), recursive=False)
            else:
                if source_ref not in self._source_tar_cache:
                    self._source_tar_cache[source_ref] = self._git(
                        "archive", "--format=tar", "--prefix=hearting/", source_ref,
                    )
                with tarfile.open(fileobj=BytesIO(self._source_tar_cache[source_ref]), mode="r:") as source:
                    for member in source.getmembers():
                        if member.name == "hearting/RELEASE_VERSION" or member.name in overlay_names:
                            continue
                        stream = source.extractfile(member) if member.isfile() else None
                        bundle.addfile(member, stream)
            for name in overlays:
                source_file = ROOT / name
                if source_file.is_file() or source_file.is_symlink():
                    bundle.add(source_file, arcname="hearting/" + name, recursive=False)
            self._add_marker(bundle, version)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (assets / f"{version}.tar.gz.sha256").write_text(
            f"{digest}  hearting.tar.gz\n", encoding="utf-8",
        )
        return archive, digest

    def test_release_archive_leaves_shared_spec_marker_state_outside_the_bundle(self):
        marker_dir = Path(self.env["HARNESS_STATE_ROOT"]) / "spec-grounding"
        marker_dir.mkdir(parents=True)
        marker = marker_dir / "session__fixture"
        marker.write_text("123456789\n", encoding="utf-8")
        archive, _digest = self._archive("state-root-fixture")
        with tarfile.open(archive, "r:gz") as bundle:
            names = bundle.getnames()
        self.assertFalse(any(
            ".spec-grounding/" in name or "/spec-grounding/" in name
            for name in names
        ))
        self.assertEqual(marker.read_text(encoding="utf-8"), "123456789\n")

    def _publish_index(self, version: str, archive: Path) -> None:
        checksum = archive.with_name(archive.name + ".sha256")
        index = archive.parent / "release.json"
        index.write_text(json.dumps({
            "tag_name": version,
            "assets": [
                {"name": "hearting.tar.gz", "browser_download_url": archive.as_uri()},
                {"name": "hearting.tar.gz.sha256", "browser_download_url": checksum.as_uri()},
            ],
        }), encoding="utf-8")
        self.env["HARNESS_RELEASE_INDEX_URL"] = index.as_uri()

    def _release_root(self, version: str) -> Path:
        return Path(self.env["XDG_DATA_HOME"]) / "hearting" / "releases" / version

    def _pycache_root(self, release: Path) -> Path:
        data_releases = Path(self.env["XDG_DATA_HOME"]) / "hearting" / "releases"
        relative = data_releases.relative_to(Path(data_releases.anchor))
        return Path(self.env["HARNESS_STATE_ROOT"]) / "pycache" / relative / release.name

    def _minimal_release(self, version: str, timestamp: int) -> Path:
        release = self._release_root(version)
        dispatch = release / ".dispatch"
        dispatch.mkdir(parents=True)
        (dispatch / "jobs.log").write_bytes(b"")
        (release / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        (release / ".hearting-release.json").write_text(json.dumps({
            "schema": 1, "version": version, "archive_sha256": "0" * 64,
        }), encoding="utf-8")
        os.utime(release, (timestamp, timestamp))
        return release

    def _install_current_fixture(
        self, version: str, *, updater_ref: str | None = None,
    ) -> tuple[Path, str]:
        archive, digest = self._archive(version)
        releases = self._release_root(version).parent
        releases.mkdir(parents=True, exist_ok=True)
        extraction = self.base / f"extract-{version}"
        extraction.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(extraction)
        release = self._release_root(version)
        shutil.move(str(extraction / "hearting"), release)
        if updater_ref is not None:
            old_source = self._git("show", f"{updater_ref}:tools/install/distribution.py")
            (release / "tools/install/distribution.py").write_bytes(old_source)
        (release / ".hearting-release.json").write_text(json.dumps({
            "schema": 1, "version": version, "archive_sha256": digest,
        }), encoding="utf-8")
        (release / ".dispatch" / "jobs.log").parent.mkdir(parents=True, exist_ok=True)
        (release / ".dispatch" / "jobs.log").touch(exist_ok=True)

        data = Path(self.env["XDG_DATA_HOME"]) / "hearting"
        state = Path(self.env["HARNESS_STATE_ROOT"])
        data.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        current = data / "current"
        current.symlink_to(release)
        (state / "distribution.json").write_text(json.dumps({
            "schema": 1, "repository": "example/hearting", "version": version,
            "archive_sha256": digest, "release_root": str(release),
            "runtimes": ["codex"], "channel": "stable",
        }), encoding="utf-8")
        bin_dir = Path(self.env["HARNESS_BIN_DIR"])
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ("hearting", "harness"):
            (bin_dir / name).symlink_to(current / "tools/install/harness.sh")
        return release, digest

    def _instrument_cleanup(self, release: Path, record_path: Path) -> str:
        module = release / "tools" / "install" / "distribution.py"
        source = module.read_text(encoding="utf-8")
        start = source.find("def _cleanup_releases(")
        self.assertGreaterEqual(start, 0, f"cleanup function missing from {module}")
        signature_end = source.find("\n", start)
        self.assertGreater(signature_end, start, f"cleanup signature is incomplete in {module}")
        probe = (
            "    _fixture_probe = os.environ.get('HARNESS_TEST_UPDATE_EXECUTION_RECORD')\n"
            "    if _fixture_probe:\n"
            "        Path(_fixture_probe).write_text(json.dumps({\n"
            "            'module_file': __file__,\n"
            "            'module_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),\n"
            "            'module_version': Path(__file__).resolve().parents[2].joinpath('RELEASE_VERSION').read_text().strip(),\n"
            "        }), encoding='utf-8')\n"
        )
        module.write_text(source[:signature_end + 1] + probe + source[signature_end + 1:], encoding="utf-8")
        return hashlib.sha256(module.read_bytes()).hexdigest()

    def _cache(self, version: str) -> Path:
        cache = self._pycache_root(self._release_root(version)) / "tools/install/__pycache__"
        cache.mkdir(parents=True)
        (cache / "distribution.cpython-312.pyc").write_bytes(b"fixture cache\n")
        return cache.parent

    def _route(
        self, route_id: str, release: Path, *, shape: str = "direct",
        rows: list[str] | None = None,
    ) -> tuple[Path, Path, bytes]:
        routes = Path(self.env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes"
        routes.mkdir(parents=True, exist_ok=True)
        jobs = release / ".dispatch" / "jobs.log"
        if rows is not None:
            jobs.write_text("\n".join(
                "\t".join(["2026-10-18T00:00:00Z", *row.split("\t")]) for row in rows
            ) + "\n", encoding="utf-8")
        launch = {
            "contract_version": 1,
            "launch_home": {"kind": "launch_home", "path": str(release.resolve())},
            "jobs_path": {"kind": "jobs_path", "path": str(jobs)},
        }
        route: dict = {
            "route_id": route_id, "schema_version": 2,
            "effective_intensity": "direct" if shape == "direct" else "standard",
            "execution_topology": "inline" if shape == "direct" else "staged",
            "owner_dispatch_depth": 0 if shape == "direct" else 1,
            "max_dispatch_depth": 0 if shape == "direct" else 2,
            "launch_compatibility_tuple": launch,
            "nodes": [{
                "id": "inline" if shape == "direct" else "plan",
                "kind": "capability-owner" if shape == "direct" else "pipeline-stage",
                "dispatch_depth": 0 if shape == "direct" else 1,
                "execution_surface": "inline" if shape == "direct" else "registered-headless",
                "registered_worker": False if shape == "direct" else True,
                "terminal": True,
                "completion_gate": "inline-complete" if shape == "direct" else "code-plan",
                "terminal_gate": "inline-complete" if shape == "direct" else "code-plan",
            }],
        }
        route_file = routes / f"{route_id}.json"
        route_file.write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
        return route_file, jobs, route_file.read_bytes()

    def _run_update(self, target: str, record_path: Path) -> dict:
        self.env["HARNESS_TEST_UPDATE_EXECUTION_RECORD"] = str(record_path)
        result = subprocess.run(
            [str(Path(self.env["HARNESS_BIN_DIR"]) / "hearting"), "update",
             "--version", target, "--yes", "--json"],
            env=self.env, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload.get("exit"), 0, payload)
        return payload

    def test_one_update_reclaims_only_the_unneeded_route_and_its_pycache(self) -> None:
        names = ["v0.0.1-old", "v0.0.2-staged", "v0.0.3-live",
                 "v0.0.4-correction", "v0.0.5-resumable",
                 "v0.0.6-unknown", "v0.0.7-current"]
        for offset, name in enumerate(names[:-1]):
            self._minimal_release(name, 1_700_000_000 + offset)
        current, _digest = self._install_current_fixture(names[-1])
        for name in names:
            self._cache(name)

        old = self._release_root(names[0])
        current_observation = current / ".dispatch/logs/dispatch-session-sweep.log"
        old_observation = old / ".dispatch/logs/dispatch-session-sweep.log"
        current_observation.parent.mkdir(parents=True, exist_ok=True)
        old_observation.parent.mkdir(parents=True, exist_ok=True)
        current_observation.write_bytes(b"current observation\n")
        old_observation.write_bytes(b"legacy observation\n")

        stable_jobs = Path(self.env["AGENT_DISPATCH_JOBS"])
        stable_jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stable_jobs.parent.chmod(0o700)
        discovery = ("2026-10-18T00:00:00Z\tdone\tfixture\t/tmp/wt\tdiscovery\t"
                     f"artifact_root={self.env['AGENT_ARTIFACT_ROOT']}\n")
        stable_jobs.write_text(discovery, encoding="utf-8")
        routes = [
            self._route("rt-1111111111111111", old),
            self._route("rt-2222222222222222", self._release_root(names[1]), shape="staged"),
            self._route("rt-3333333333333333", self._release_root(names[2]), rows=[
                "open\tfixture\t/tmp/wt\tlive\troute_id=rt-3333333333333333,attempt_id=live",
            ]),
            self._route("rt-4444444444444444", self._release_root(names[3]), rows=[
                "done\tfixture\t/tmp/wt\towner\towner_route_id=rt-4444444444444444,worker_type=owner,attempt_id=blocked,note=dead-worker-blocked,failure_class=blocked",
            ]),
            self._route("rt-6666666666666666", self._release_root(names[4])),
            self._route("rt-7777777777777777", self._release_root(names[5]), rows=[
                "future-state\tfixture\t/tmp/wt\tfuture\troute_id=rt-7777777777777777,attempt_id=future",
            ]),
        ]
        old_contract_bytes = (ROOT / "capabilities" / "code-execute.md").read_bytes()
        old_contract_root = self._release_root(names[3]) / "capabilities"
        old_contract_root.mkdir(parents=True)
        (old_contract_root / "code-execute.md").write_bytes(old_contract_bytes)
        route_chain = (Path(self.env["XDG_STATE_HOME"]) / "agent-fleet" / "route-chains"
                       / "codex" / "fixture-session.jsonl")
        route_chain.parent.mkdir(parents=True)
        route_chain.write_text(json.dumps({
            "v": 1, "ts": 1_797_000_000.0, "event": "start",
            "harness": "codex", "session_id": "fixture-session",
            "route_id": "rt-6666666666666666", "route_file": str(routes[4][0]),
        }) + "\n", encoding="utf-8")
        route_bytes = {path: before for path, _jobs, before in routes}
        registry_bytes = {jobs: jobs.read_bytes() for _path, jobs, _before in routes}
        ack = Path(self.env["AGENT_ARTIFACT_ROOT"]) / ".runtime/acks/fixture.json"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_bytes(b"fixture ack\n")
        session = self.base / "fixture-session.json"
        session.write_bytes(b"fixture session\n")
        immutable = {**route_bytes,
                     **{path: before for path, before in registry_bytes.items() if path != routes[0][1]},
                     route_chain: route_chain.read_bytes(),
                     ack: ack.read_bytes(),
                     stable_jobs: stable_jobs.read_bytes(), session: session.read_bytes()}

        patched_sha = hashlib.sha256((current / "tools/install/distribution.py").read_bytes()).hexdigest()
        execution_record = self.base / "execution.json"
        executing_sha = self._instrument_cleanup(current, execution_record)
        target = "v0.0.7-target"
        target_archive, _ = self._archive(target)
        self._publish_index(target, target_archive)
        update = self._run_update(target, execution_record)

        record = json.loads(execution_record.read_text(encoding="utf-8"))
        target_root = self._release_root(target)
        self.assertEqual(Path(record["module_file"]).resolve(),
                         (current / "tools/install/distribution.py").resolve())
        self.assertEqual(record["module_sha256"], executing_sha)
        self.assertEqual(record["module_version"], names[-1])
        self.assertEqual(hashlib.sha256((target_root / "tools/install/distribution.py").read_bytes()).hexdigest(), patched_sha)
        self.assertEqual((Path(self.env["XDG_DATA_HOME"]) / "hearting/current").resolve(), target_root.resolve())
        self.assertEqual(update["release"]["version"], target)
        sys.path.insert(0, str(ROOT / "utilities"))
        import route_authority
        import parent_next_directive
        correction_route = json.loads(routes[3][0].read_text(encoding="utf-8"))
        contract_roots = route_authority.contract_read_roots(target_root, correction_route)
        sealed_contract_root = next(
            root for root in contract_roots if root.resolve() == old_contract_root.resolve()
        )
        loaded_contract = (sealed_contract_root / "code-execute.md").read_bytes()
        self.assertEqual(loaded_contract, old_contract_bytes)
        self.assertIn(target_root / "capabilities", contract_roots)
        correction_row = next(
            line.split("\t") for line in routes[3][1].read_text(encoding="utf-8").splitlines()
            if "owner_route_id=rt-4444444444444444" in line
        )
        correction_metadata = dict(
            item.split("=", 1) for item in correction_row[5].split(",") if "=" in item
        )
        self.assertEqual(
            route_authority.answerable_owner_end(correction_row[1], correction_metadata), "BLOCKED"
        )
        continuation = parent_next_directive.correction_command(
            correction_metadata["attempt_id"], routes[3][1], agent_home=target_root,
        )
        continuation_args = shlex.split(continuation)
        self.assertEqual(continuation_args[1], str(
            Path(self.env["XDG_DATA_HOME"]) / "hearting" / "current"
            / "utilities" / "capability-route.py"
        ))
        self.assertEqual(continuation_args[2:], [
            "correct", "--jobs", str(routes[3][1]), "--attempt-id", "blocked",
        ])

        self.assertFalse(old.exists(), "the historical direct route alone must not pin its release")
        self.assertFalse(self._pycache_root(old).exists(), "deleted release pycache must be reclaimed")
        for name in names[1:-1]:
            self.assertTrue(self._release_root(name).exists(), f"protected release {name} was removed")
            self.assertTrue(self._pycache_root(self._release_root(name)).exists(),
                            f"protected release cache {name} was removed")
        self.assertTrue(self._release_root(names[-1]).exists(), "previous release remains inside retention floor")
        self.assertTrue(self._pycache_root(self._release_root(names[-1])).exists())
        self.assertTrue(self._release_root(names[5]).exists(), "unknown route state must retain its release")
        self.assertTrue(self._pycache_root(self._release_root(names[5])).exists(),
                        "unknown route state must retain its release cache")
        for path, before in immutable.items():
            self.assertEqual(path.read_bytes(), before, f"update changed protected state: {path}")

        root_digest = hashlib.sha256(
            os.fsencode(str((old / ".dispatch").resolve(strict=False)))
        ).hexdigest()
        observation_digest = hashlib.sha256(b"legacy observation\n").hexdigest()
        archive = (Path(self.env["HARNESS_STATE_ROOT"]) / "dispatch-observation-archive"
                   / root_digest / "logs/dispatch-session-sweep.log" / observation_digest)
        self.assertEqual(archive.read_bytes(), b"legacy observation\n")
        self.assertEqual(current_observation.read_bytes(), b"current observation\n")
        print("update_execution=" + json.dumps({
            "module_file": record["module_file"], "module_version": record["module_version"],
            "module_sha256": record["module_sha256"], "installed_target": target,
            "current_after_update": str(target_root), "update_calls": 1,
            "old_contract_read_root": str(sealed_contract_root),
            "old_contract_sha256": hashlib.sha256(loaded_contract).hexdigest(),
            "correction_continuation": continuation,
            "unknown_route_release": str(self._release_root(names[5])),
        }, sort_keys=True))

    def test_v33552_updater_first_installs_patch_but_keeps_its_own_cleanup_identity(self) -> None:
        old_version, candidate_version, target = "v3.35.52", "v3.35.50", "v3.35.54"
        candidate = self._minimal_release(candidate_version, 1_700_000_000)
        current, _digest = self._install_current_fixture(old_version, updater_ref=old_version)
        self._cache(candidate_version)
        self._cache(old_version)
        stable_jobs = Path(self.env["AGENT_DISPATCH_JOBS"])
        stable_jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stable_jobs.parent.chmod(0o700)
        stable_jobs.write_text(
            "2026-10-18T00:00:00Z\tdone\tfixture\t/tmp/wt\tdiscovery\t"
            f"artifact_root={self.env['AGENT_ARTIFACT_ROOT']}\n", encoding="utf-8",
        )
        route_file, _jobs, before = self._route("rt-5555555555555555", candidate)
        execution_record = self.base / "old-execution.json"
        executing_sha = self._instrument_cleanup(current, execution_record)
        target_archive, _ = self._archive(target)
        self._publish_index(target, target_archive)
        self._run_update(target, execution_record)

        record = json.loads(execution_record.read_text(encoding="utf-8"))
        target_root = self._release_root(target)
        self.assertEqual(Path(record["module_file"]).resolve(),
                         (current / "tools/install/distribution.py").resolve())
        self.assertEqual(record["module_version"], old_version)
        self.assertEqual(record["module_sha256"], executing_sha)
        self.assertEqual((Path(self.env["XDG_DATA_HOME"]) / "hearting/current").resolve(), target_root.resolve())
        self.assertTrue(candidate.exists(), "the previous updater must keep its unresolved route pin")
        self.assertTrue(self._pycache_root(candidate).exists())
        self.assertEqual(route_file.read_bytes(), before)
        print("update_execution=" + json.dumps({
            "source_revision": old_version,
            "source_distribution_sha256": hashlib.sha256(
                self._git("show", f"{old_version}:tools/install/distribution.py")
            ).hexdigest(),
            "module_file": record["module_file"], "module_version": record["module_version"],
            "module_sha256": record["module_sha256"], "installed_target": target,
            "current_after_update": str(target_root), "old_pin_remains": True,
            "update_calls": 1,
        }, sort_keys=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
