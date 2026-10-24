#!/usr/bin/env python3
import os
from contextlib import chdir
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_activation as activation  # noqa: E402
import installer  # noqa: E402
import fixture_env  # noqa: E402
import codex_hook_trust  # noqa: E402


class ManagedCopyProjectionHealthTest(unittest.TestCase):
    def test_copy_health_requires_matching_regular_file_bytes_and_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sh"
            dest = root / "projected.sh"
            source.write_bytes(b"#!/bin/sh\necho managed\n")
            source.chmod(0o755)
            entry = {"source": str(source), "dest": str(dest), "kind": "copy"}

            dest.write_bytes(source.read_bytes())
            dest.chmod(0o755)
            self.assertEqual(activation._entries_healthy([entry]), (False, False))

            dest.write_bytes(b"changed bytes\n")
            dest.chmod(0o755)
            self.assertEqual(activation._entries_healthy([entry]), (False, True))

            dest.write_bytes(source.read_bytes())
            dest.chmod(0o644)
            self.assertEqual(activation._entries_healthy([entry]), (False, True))

            # destructive-ok: reason=exercise the missing projected-copy state; boundary=dest leaf under this test's TemporaryDirectory root
            dest.unlink()
            self.assertEqual(activation._entries_healthy([entry]), (True, False))

            # Even a symlink to the expected source is the wrong managed kind.
            dest.symlink_to(source)
            self.assertEqual(activation._entries_healthy([entry]), (False, True))
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(), source.resolve())

            foreign = root / "foreign.sh"
            foreign.write_bytes(source.read_bytes())
            foreign.chmod(0o755)
            # destructive-ok: reason=replace this test's expected-source symlink with a foreign symlink; boundary=dest leaf under this test's TemporaryDirectory root
            dest.unlink()
            dest.symlink_to(foreign)
            self.assertEqual(activation._entries_healthy([entry]), (False, True))
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(), foreign.resolve())

            # destructive-ok: reason=replace this test's foreign symlink with a directory; boundary=dest leaf under this test's TemporaryDirectory root
            dest.unlink()
            dest.mkdir()
            self.assertEqual(activation._entries_healthy([entry]), (False, True))


class CodexHookTrustTest(unittest.TestCase):
    def test_codex_projection_binds_the_herdr_home_consumer_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex"
            with mock.patch.object(activation.paths, "runtime_home", return_value=home):
                entries = activation._linked_entries("codex", REPO_ROOT, "global")
        integration = [entry for entry in entries
                       if Path(entry["dest"]).name == "herdr-agent-state.sh"]
        self.assertEqual(len(integration), 1, integration)
        self.assertEqual(Path(integration[0]["source"]),
                         REPO_ROOT / "adapters/codex/hooks/herdr-agent-state.sh")
        self.assertEqual(Path(integration[0]["dest"]), home / "herdr-agent-state.sh")

    def test_activation_publishes_the_receipt_and_deactivation_restores_its_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            calls = []

            def record(home, hooks_file, previous, **_kwargs):
                calls.append((home, hooks_file, previous))
                self.assertTrue((home / "hooks.json").is_symlink())
                config = home / "config.toml"
                with config.open("a", encoding="utf-8") as handle:
                    handle.write('\n[hooks.state."fixture-key"]\ntrusted_hash = "sha256:fixture"\n')
                return {"record": {"schema": 1, "hooks": {"fixture-key": {
                    "source": str(hooks_file), "written_hash": "sha256:fixture",
                    "baseline_present": False, "baseline_value": None}}},
                    "status": "recorded", "report_lines": []}

            with fixture_env.patched_environment(root / "fixture", source), \
                    mock.patch.object(codex_hook_trust, "record_managed_trust", side_effect=record):
                activation.activate("codex", "linked", str(source))
                state = activation._load_json(activation._state_path("codex"))
                self.assertEqual(state["managed_config"]["codex_hook_trust"]["hooks"]["fixture-key"][
                    "written_hash"], "sha256:fixture")
                self.assertEqual(len(calls), 1)
                activation.deactivate("codex")
                config = activation.paths.runtime_home("codex") / "config.toml"
                self.assertFalse(config.exists())

    def test_state_publication_failure_rolls_back_the_native_trust_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            original_atomic_json = activation._atomic_json

            def record(home, hooks_file, previous, **_kwargs):
                with (home / "config.toml").open("a", encoding="utf-8") as handle:
                    handle.write('\n[hooks.state."fixture-key"]\ntrusted_hash = "sha256:fixture"\n')
                return {"record": {"schema": 1, "hooks": {"fixture-key": {
                    "source": str(hooks_file), "written_hash": "sha256:fixture",
                    "baseline_present": False, "baseline_value": None}}},
                    "status": "recorded", "report_lines": []}

            def fail_state(path, payload):
                if Path(path) == activation._state_path("codex"):
                    raise OSError("fixture-state-publication-failed")
                return original_atomic_json(path, payload)

            with fixture_env.patched_environment(root / "fixture", source), \
                    mock.patch.object(codex_hook_trust, "record_managed_trust", side_effect=record), \
                    mock.patch.object(activation, "_atomic_json", side_effect=fail_state):
                with self.assertRaisesRegex(OSError, "fixture-state-publication-failed"):
                    activation.activate("codex", "linked", str(source))
                home = activation.paths.runtime_home("codex")
                self.assertFalse((home / "config.toml").exists())
                self.assertFalse((home / "hooks.json").exists())
                self.assertFalse(activation._state_path("codex").exists())

    def test_native_hash_upsert_uses_resolved_config_and_preserves_disabled_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            active = root / "active"
            active.mkdir()
            hooks = active / "hooks.json"
            hooks.write_text('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}')
            (home / "hooks.json").symlink_to(hooks)
            target = root / "external-config.toml"
            target.write_text('[hooks.state."stable-key"]\nenabled = false\n')
            target.chmod(0o640)
            config = home / "config.toml"
            config.symlink_to(os.path.relpath(target, home))
            observed = {}

            class Server:
                def __init__(self, command, cwd, *, env=None):
                    observed.update(command=command, cwd=cwd, env=env)
                def request(self, method, params, *, timeout=5):
                    observed.setdefault("calls", []).append((method, params, timeout))
                    if method == "hooks/list":
                        return {"data": [{"cwd": str(Path(os.environ.get("TMPDIR") or "/tmp").resolve()),
                                          "hooks": [{"sourcePath": str(hooks), "key": "stable-key",
                                                     "currentHash": "sha256:new", "trustStatus": "untrusted",
                                                     "enabled": False}]}]}
                    if method == "config/batchWrite":
                        self.params = params
                        target.write_text('[hooks.state."stable-key"]\nenabled = false\ntrusted_hash = "sha256:new"\n')
                        return {}
                    return {}
                def notify(self, method):
                    observed.setdefault("notifications", []).append(method)
                def close(self):
                    observed["closed"] = True

            fake_client = type("Client", (), {
                "AppServer": Server,
                "same_source": staticmethod(lambda raw, expected: Path(raw).samefile(expected)),
            })
            with mock.patch.object(codex_hook_trust, "_status_client", return_value=fake_client):
                result = codex_hook_trust.record_managed_trust(home, hooks, None, command=["fixture"])
            self.assertEqual(result["status"], "recorded")
            self.assertEqual(result["record"]["hooks"]["stable-key"]["written_hash"], "sha256:new")
            self.assertTrue(config.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertIn("enabled = false", target.read_text())
            write = next(params for method, params, _ in observed["calls"] if method == "config/batchWrite")
            self.assertEqual(write["filePath"], str(target))
            self.assertTrue(write["reloadUserConfig"])
            self.assertNotIn("enabled", json.dumps(write))
            self.assertEqual(write["edits"][0]["keyPath"], "hooks.state")
            self.assertEqual(observed["env"]["CODEX_HOME"], str(home))

    def test_restorer_only_changes_its_owned_trusted_hash_leaf(self):
        original = ('[hooks.state."key"]\nenabled = false\ntrusted_hash = "sha256:owned"\n'
                    '\n[other]\nvalue = 1\n')
        record = {"hooks": {"key": {"written_hash": "sha256:owned", "baseline_present": False}}}
        restored, reports = codex_hook_trust.restore_trust_text(original, record)
        self.assertEqual(reports, [])
        self.assertIn("enabled = false", restored)
        self.assertNotIn("trusted_hash", restored)
        self.assertIn("[other]", restored)
        import tomllib
        self.assertEqual(tomllib.loads(restored)["hooks"]["state"]["key"]["enabled"], False)

    def test_restorer_removes_an_empty_owned_table_before_an_unrelated_table(self):
        original = ('[hooks.state."key"]\ntrusted_hash = "sha256:owned"\n'
                    '\n[projects."/mine"]\ntrust_level = "trusted"\n')
        record = {"hooks": {"key": {"written_hash": "sha256:owned", "baseline_present": False}}}
        restored, reports = codex_hook_trust.restore_trust_text(original, record)
        self.assertEqual(reports, [])
        self.assertNotIn("[hooks.state.", restored)
        import tomllib
        self.assertEqual(tomllib.loads(restored)["projects"]["/mine"]["trust_level"], "trusted")

    def test_restorer_removes_one_separator_with_an_empty_owned_table(self):
        original = ('[hooks.state."key"]\ntrusted_hash = "sha256:owned"\n'
                    '\n[projects."/mine"]\ntrust_level = "trusted"\n')
        record = {"hooks": {"key": {"written_hash": "sha256:owned", "baseline_present": False}}}
        restored, reports = codex_hook_trust.restore_trust_text(original, record)
        self.assertEqual(reports, [])
        self.assertEqual(restored, '[projects."/mine"]\ntrust_level = "trusted"\n')
        import tomllib
        self.assertEqual(tomllib.loads(restored)["projects"]["/mine"]["trust_level"], "trusted")


class RuntimeSnapshotTest(unittest.TestCase):
    # destructive-ok: reason=exercise added and deleted source fingerprints; boundary=entry.py and new_entry.py under this test TemporaryDirectory
    def test_launch_revision_tracks_source_but_not_unversioned_work_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "utilities").mkdir()
            source = root / "utilities" / "entry.py"
            source.write_text("original\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "base")
            initial = activation.source_revision(root, runtime_launch=True)
            (root / "final_report.md").write_text("work output\n")
            self.assertEqual(activation.source_revision(root, runtime_launch=True), initial)
            # Both modes now share one root-level filter (`_release_content_skip`):
            # an untracked top-level work output is not release content in either
            # mode, so the default call agrees with the launch call here too.
            self.assertEqual(activation.source_revision(root), initial)
            addition = root / "utilities" / "new_entry.py"
            addition.write_text("new runtime code\n")
            self.assertNotEqual(activation.source_revision(root, runtime_launch=True), initial)
            addition.unlink()
            source.write_text("changed\n")
            self.assertNotEqual(activation.source_revision(root, runtime_launch=True), initial)
            source.unlink()
            self.assertNotEqual(activation.source_revision(root, runtime_launch=True), initial)
    def test_release_revision_ignores_runtime_grounding_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "RELEASE_VERSION").write_text("v1.2.3\n", encoding="utf-8")
            (root / "core").mkdir()
            (root / "core" / "CORE.md").write_text("stable\n", encoding="utf-8")
            expected = activation.source_revision(root)

            for marker in (
                ".capability-grounding",
                ".route-grounding",
                ".core-grounding",
                ".spec-grounding",
            ):
                marker_root = root / marker
                marker_root.mkdir()
                (marker_root / "session.json").write_text("{}\n", encoding="utf-8")
                self.assertEqual(activation.source_revision(root), expected, marker)

            (root / "core" / "CORE.md").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(activation.source_revision(root), expected)

    def test_runtime_activate_defaults_to_packaged_snapshot(self):
        args = installer.build_parser().parse_args(
            ["runtime", "activate", "--runtime", "codex"]
        )
        self.assertEqual(args.mode, "packaged")

    def test_snapshot_and_restore_preserve_live_managed_session_sockets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".harness"
            managed = state / "managed-sessions" / "live"
            managed.mkdir(parents=True)
            current = state / "activation.json"
            current.write_text("before\n", encoding="utf-8")
            socket_path = managed / "app-server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Bind the same real socket without spending AF_UNIX's path budget
            # on the runner's potentially long temporary-directory prefix.
            with chdir(root):
                listener.bind(str(socket_path.relative_to(root)))
            try:
                record = activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )
                self.assertFalse(
                    (Path(record["backup"]) / "managed-sessions").exists())
                current.write_text("after\n", encoding="utf-8")
                (state / "new-projection").write_text("remove me\n", encoding="utf-8")
                record["postimage"] = activation.safe_fs.capture_state(
                    state, exclude_names=("managed-sessions",)
                ).public()
                activation._restore([record])
                self.assertEqual(current.read_text(encoding="utf-8"), "before\n")
                self.assertFalse((state / "new-projection").exists())
                self.assertTrue(socket_path.is_socket())
            finally:
                listener.close()

    def test_preserved_runtime_directory_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".harness"
            state.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (state / "managed-sessions").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(activation.ActivationError):
                activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )


class ReleaseContentSkipTest(unittest.TestCase):
    """`_release_content_skip` is the one filter cleanliness, copy, and the
    bundle key share: a root-level untracked item (e.g. an unversioned
    `dist/` build output) is not release content anywhere."""

    def _git_checkout(self, root: Path) -> Path:
        checkout = root / "checkout"
        checkout.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)

        git("init", "-q")
        (checkout / "utilities").mkdir()
        (checkout / "utilities" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        git("add", ".")
        git(
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
            "commit", "-qm", "base",
        )
        return checkout

    def test_packaged_ignores_root_untracked_release_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = self._git_checkout(root)
            dist = checkout / "dist"
            dist.mkdir()
            (dist / "a.tgz").write_bytes(b"binary")

            revision = activation.source_revision(checkout)
            self.assertNotIn("+dirty:", revision)

            state_home = root / "codex-home"
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(state_home)
            try:
                bundle_source = activation._build_bundle("codex", checkout, revision, "global")
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

            self.assertFalse((bundle_source / "dist").exists())
            self.assertTrue((bundle_source / "utilities" / "tool.py").is_file())

    def test_source_dir_untracked_file_stays_dirty_in_both_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = self._git_checkout(root)
            baseline_launch = activation.source_revision(checkout, runtime_launch=True)
            baseline_default = activation.source_revision(checkout)
            (checkout / "utilities" / "new.py").write_text("print(2)\n", encoding="utf-8")
            self.assertNotEqual(
                activation.source_revision(checkout, runtime_launch=True), baseline_launch
            )
            self.assertNotEqual(activation.source_revision(checkout), baseline_default)


class SurfaceSkewTest(unittest.TestCase):
    """The four install surfaces are updated by two different commands and drift apart.

    Regression bar for 2026-08-19: a runtime activation advanced ~/.claude while the
    managed release tree stayed behind, so `fleet` — which resolves through
    ~/.local/share/hearting/current — kept running the old code while every existing
    check reported success. The release surface appeared in no diagnostic at all.
    """

    def _surface(self, root: Path, subtree_body: str, marker: str = None) -> Path:
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "fleet.py").write_text(subtree_body, encoding="utf-8")
        for name in ("adapters", "capabilities", "core", "hooks", "roles", "utilities"):
            (root / name).mkdir()
            (root / name / "shared.md").write_text("same everywhere\n", encoding="utf-8")
        if marker is not None:
            (root / ".hearting-release.json").write_text(
                '{"version": "%s"}\n' % marker, encoding="utf-8"
            )
        return root

    def test_identical_content_across_surface_kinds_is_not_skew(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Same code, different surface kind: only the release carries the release
            # marker, exactly as on disk. A whole-tree digest calls that a difference and
            # would report permanent false skew — which is why subtrees are compared.
            release = self._surface(Path(temporary) / "release", "same\n", marker="v1.0.0")
            bundle = self._surface(Path(temporary) / "bundle", "same\n")
            self.assertNotEqual(
                activation._tree_digest(release), activation._tree_digest(bundle),
                "whole-tree digests differ across kinds — the reason subtrees are used",
            )
            self.assertEqual(
                activation._surface_digests(release), activation._surface_digests(bundle)
            )

    def test_release_behind_a_runtime_is_reported_and_names_the_subtree(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = self._surface(Path(temporary) / "release", "old\n", marker="v1.0.0")
            current = self._surface(Path(temporary) / "runtime", "new\n")
            original = activation._load_json

            def fake_load(path):
                if path.name == "activation.json":
                    return {"active_root": str(current), "active_revision": "abc123"}
                return original(path)

            activation._load_json = fake_load
            try:
                report = activation.surface_skew(release)
            finally:
                activation._load_json = original

            self.assertFalse(report["ok"])
            self.assertEqual([entry["subtree"] for entry in report["skewed"]], ["tools"])
            groups = report["skewed"][0]["groups"]
            self.assertIn(["release"], groups)
            self.assertIn(sorted(activation.RUNTIMES), groups)
            self.assertIn("release", report["compared"])

    def test_loop_runtime_logs_do_not_read_as_skew(self):
        """A bundle copied from a checkout carries git-ignored loop logs; a release never
        does. Digesting them reported permanent `adapters` skew with identical code."""
        with tempfile.TemporaryDirectory() as temporary:
            release = self._surface(Path(temporary) / "release", "same\n", marker="v1.0.0")
            bundle = self._surface(Path(temporary) / "bundle", "same\n")
            # `loops/` itself is tracked (README, .gitignore, drill cases), so it exists in
            # BOTH trees; only the run output inside it is ephemeral and bundle-only.
            for root in (release, bundle):
                (root / "adapters" / "loops").mkdir(parents=True)
                (root / "adapters" / "loops" / "README.md").write_text(
                    "tracked\n", encoding="utf-8"
                )
            (bundle / "adapters" / "loops" / "oncall.log").write_text(
                "run output\n", encoding="utf-8"
            )
            self.assertEqual(
                activation._surface_digests(release)["adapters"],
                activation._surface_digests(bundle)["adapters"],
            )
            # A tracked `.log` fixture is source and must still count.
            fixture = bundle / "adapters" / "tests" / "fixtures"
            fixture.mkdir(parents=True)
            (fixture / "jobs_route.log").write_text("fixture\n", encoding="utf-8")
            self.assertNotEqual(
                activation._surface_digests(release)["adapters"],
                activation._surface_digests(bundle)["adapters"],
            )

    def test_default_tree_digest_is_unchanged_by_the_skip_hook(self):
        """`_bundle_checksum` persists this value in bundle metadata; adding the optional
        filter must not move it for callers that pass no filter."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self._surface(Path(temporary) / "tree", "body\n")
            loops = root / "adapters" / "loops"
            loops.mkdir(parents=True)
            (loops / "oncall.log").write_text("run output\n", encoding="utf-8")
            with_log = activation._tree_digest(root)
            (loops / "oncall.log").write_text("different\n", encoding="utf-8")
            self.assertNotEqual(with_log, activation._tree_digest(root))

    def test_absent_surface_is_not_skew(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "never-installed"
            original = activation._load_json
            activation._load_json = lambda path: None
            try:
                report = activation.surface_skew(missing)
            finally:
                activation._load_json = original
            self.assertTrue(report["ok"])
            self.assertEqual(report["compared"], [])
            self.assertTrue(all(not s["present"] for s in report["surfaces"]))


class BundleRuntimeStateTest(unittest.TestCase):
    """A release bundle is immutable; runtime state written inside it is a finding."""

    def _bundle(self, root: Path) -> Path:
        source = root / "runtime-home" / ".harness" / "bundles" / "release-v1-aaa" / "source"
        (source / "utilities").mkdir(parents=True)
        return source

    def test_reports_state_written_inside_the_active_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._bundle(Path(temporary))
            governor = ".runtime/model-worker-governor"
            (source / ".agent_reports" / governor).mkdir(parents=True)
            (source / "utilities" / ".agent_reports" / governor).mkdir(parents=True)
            found = activation.bundle_runtime_state(source)
            self.assertEqual(
                found,
                sorted(
                    [
                        str(source / ".agent_reports"),
                        str(source / "utilities" / ".agent_reports"),
                    ]
                ),
            )

    def test_clean_bundle_reports_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._bundle(Path(temporary))
            self.assertEqual(activation.bundle_runtime_state(source), [])

    def test_a_linked_checkout_is_never_scanned(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / ".agent_reports").mkdir(parents=True)
            self.assertEqual(activation.bundle_runtime_state(checkout), [])

    def test_nested_state_is_not_double_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._bundle(Path(temporary))
            nested = source / ".agent_reports" / "inner" / ".claude_reports"
            nested.mkdir(parents=True)
            self.assertEqual(
                activation.bundle_runtime_state(source),
                [str(source / ".agent_reports")],
            )


class LinkedReleaseBundleTest(unittest.TestCase):
    """Defect Q: a packaged bundle copied from an immutable managed release gave
    one content two paths, and `resolve_agent_home()` ranks the runtime's own
    bundle pointer above the managed `current` pointer -- so codex/opencode
    resolved AGENT_HOME to the bundle while the registry sealed the release, and
    every launch without an explicit AGENT_HOME failed launch-runtime-root-mismatch."""

    def _release(self, root: Path, version: str = "v9.9.9") -> Path:
        release = root / "releases" / version
        (release / "core").mkdir(parents=True)
        (release / "core" / "CORE.md").write_text("release core\n", encoding="utf-8")
        (release / "utilities").mkdir()
        (release / "utilities" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        (release / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        return release

    def _checkout(self, root: Path) -> Path:
        checkout = root / "checkout"
        (checkout / "core").mkdir(parents=True)
        (checkout / "core" / "CORE.md").write_text("dev core\n", encoding="utf-8")
        return checkout

    def _build(self, source: Path, state_home: Path, runtime: str = "codex"):
        import os
        previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(state_home)
        try:
            revision = activation.source_revision(source)
            return activation._build_bundle(runtime, source, revision, "global"), revision
        finally:
            if previous is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous

    def test_a_managed_release_is_linked_not_copied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, revision = self._build(release, root / "codex-home")
            self.assertTrue(revision.startswith("release:"), revision)
            self.assertTrue(bundle_source.is_symlink())
            self.assertEqual(
                Path(os.path.realpath(bundle_source)), Path(os.path.realpath(release))
            )
            # The link is the whole bundle payload: nothing was duplicated.
            self.assertFalse((bundle_source.parent / "source" / "utilities").is_symlink())
            self.assertTrue((bundle_source / "utilities" / "tool.py").is_file())

    def test_the_bundle_pointer_and_the_release_are_one_object(self):
        # The exact condition defect Q needed: two paths, one content. After the
        # fix the two candidate AGENT_HOME values name one filesystem object, so
        # every consumer that resolves a path (launch tuple, runtime-root guard)
        # reaches the same identity and the mismatch cannot be constructed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            self.assertNotEqual(str(bundle_source), str(release))
            self.assertTrue(activation._same_tree(bundle_source, release))
            self.assertEqual(
                (bundle_source / "core" / "CORE.md").read_text(encoding="utf-8"),
                (release / "core" / "CORE.md").read_text(encoding="utf-8"),
            )

    def test_a_dev_checkout_is_still_copied(self):
        # A checkout is mutable, so the bundle must still freeze it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = self._checkout(root)
            bundle_source, revision = self._build(checkout, root / "codex-home")
            self.assertFalse(revision.startswith("release:"), revision)
            self.assertFalse(bundle_source.is_symlink())
            self.assertTrue((bundle_source / "core" / "CORE.md").is_file())
            checkout_core = checkout / "core" / "CORE.md"
            checkout_core.write_text("mutated\n", encoding="utf-8")
            self.assertEqual(
                (bundle_source / "core" / "CORE.md").read_text(encoding="utf-8"),
                "dev core\n",
            )

    def test_rebuilding_reuses_the_link_and_repairs_a_repointed_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            again, _ = self._build(release, root / "codex-home")
            self.assertEqual(bundle_source, again)
            elsewhere = self._release(root, "v8.8.8")
            # destructive-ok: reason=repoint the bundle link so the repair path has something stale to fix; boundary=the one symlink this test just created under its own temporary codex home
            bundle_source.unlink()
            os.symlink(elsewhere, bundle_source, target_is_directory=True)
            repaired, _ = self._build(release, root / "codex-home")
            self.assertTrue(activation._same_tree(repaired, release))

    def test_rebuilding_retires_only_linked_bundles_whose_release_is_gone(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            old = self._release(root, "v1.0.0")
            active = self._release(root, "v1.0.1")
            live = self._release(root, "v1.0.2")
            old_bundle, _ = self._build(old, home)
            active_bundle, _ = self._build(active, home)
            live_bundle, _ = self._build(live, home)
            copy_bundle, _ = self._build(self._checkout(root), home)
            (home / ".harness").mkdir(exist_ok=True)
            (home / ".harness" / "activation.json").write_text(
                json.dumps({"active_root": str(active_bundle)}), encoding="utf-8"
            )
            bundles = old_bundle.parent.parent
            dangling = root / "gone"
            staging = bundles / ".staging-left"
            staging.mkdir()
            os.symlink(dangling, staging / "source", target_is_directory=True)
            (staging / "bundle.json").write_text('{"source_link": true}', encoding="utf-8")
            unmarked = bundles / "unmarked"
            unmarked.mkdir()
            os.symlink(dangling, unmarked / "source", target_is_directory=True)
            foreign = bundles / "foreign"
            os.symlink(live_bundle.parent, foreign, target_is_directory=True)
            # destructive-ok: reason=make two linked bundles dangle by removing release trees this test created; boundary=v1.0.0 release dir inside this test's own temporary directory
            shutil.rmtree(old)
            # destructive-ok: reason=make the recorded active bundle dangle too; boundary=v1.0.1 release dir inside this test's own temporary directory
            shutil.rmtree(active)

            self._build(self._release(root, "v1.1.0"), home)

            self.assertFalse(os.path.lexists(old_bundle.parent))
            for survivor in (
                active_bundle.parent, live_bundle.parent, copy_bundle.parent,
                staging, unmarked, foreign,
            ):
                self.assertTrue(os.path.lexists(survivor), survivor)
            self.assertTrue(live.is_dir())


    def test_prune_preserves_unexpected_content_in_dangling_marked_bundle(self):
        import json
        import os
        import shutil
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            release = self._release(root, "v1.0.0")
            source, _ = self._build(release, home)
            bundle = source.parent
            # Retire the release, then add content that activation did not create.
            # destructive-ok: reason=make the linked source dangling for the ownership fixture; boundary=the release created inside this test's temporary directory
            shutil.rmtree(release)
            foreign = bundle / "operator-data"
            foreign.write_text("keep\n", encoding="utf-8")
            self._build(self._release(root, "v1.1.0"), home)
            self.assertTrue(foreign.exists())

    def test_prune_does_not_treat_stat_error_as_dangling(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            release = self._release(root, "v2.0.0")
            source, _ = self._build(release, home)
            bundle = source.parent
            real_stat = os.stat
            target_stat_denials = []
            def denied(path, *args, **kwargs):
                if Path(path) == source and kwargs.get("follow_symlinks", True):
                    target_stat_denials.append(Path(path))
                    raise PermissionError("fixture stat denied")
                return real_stat(path, *args, **kwargs)
            with mock.patch.object(activation.os, "stat", side_effect=denied):
                self._build(self._release(root, "v2.1.0"), home)
            self.assertTrue(target_stat_denials, "fixture must reach the followed source target")
            self.assertTrue(bundle.exists())

    def test_discarding_a_linked_bundle_never_removes_the_release(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            bundles = bundle_source.parent.parent
            # destructive-ok: reason=prove the linked release survives losing its bundle store; boundary=the bundle root inside this test's own temporary directory
            shutil.rmtree(bundles)
            self.assertFalse(bundles.exists())
            self.assertTrue(release.is_dir())
            self.assertTrue((release / "utilities" / "tool.py").is_file())

    def test_a_linked_bundle_is_not_scanned_as_bundle_residue(self):
        # bundle_runtime_state walks the active bundle to find runtime state
        # written inside an immutable tree. A linked bundle owns no tree, so
        # walking it would descend into the release and report the release's own
        # contents as this bundle's residue.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            (release / ".agent_reports").mkdir()
            bundle_source, _ = self._build(release, root / "codex-home")
            self.assertEqual(activation.bundle_runtime_state(bundle_source), [])
            # A copied bundle keeps reporting exactly as before. The residue has
            # to be written after the build: `_bundle_ignore` never copies it in.
            checkout = self._checkout(root)
            copied, _ = self._build(checkout, root / "codex-home-2")
            (copied / ".agent_reports").mkdir()
            self.assertEqual(
                activation.bundle_runtime_state(copied), [str(copied / ".agent_reports")]
            )

    def test_linked_bundle_checksum_still_asserts_content(self):
        # Review S1: verifying only the link made `bundle_stale` unfalsifiable —
        # status compares this value against the one it came from — so a release
        # replaced in place under the same version tag would have read as fresh.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            self.assertIsInstance(activation._bundle_checksum(bundle_source), str)
            (release / "utilities" / "tool.py").write_text("print(2)\n", encoding="utf-8")
            self.assertIsNone(activation._bundle_checksum(bundle_source))

    def test_linked_bundle_checksum_tracks_the_link_not_a_tree_walk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            checksum = activation._bundle_checksum(bundle_source)
            self.assertIsInstance(checksum, str)
            (release / "utilities" / "__pycache__").mkdir()
            (release / "utilities" / "__pycache__" / "tool.pyc").write_bytes(b"\x00")
            self.assertEqual(activation._bundle_checksum(bundle_source), checksum)
            elsewhere = self._release(root, "v7.7.7")
            # destructive-ok: reason=repoint the bundle link to make the checksum stale; boundary=the one symlink this test just created under its own temporary codex home
            bundle_source.unlink()
            os.symlink(elsewhere, bundle_source, target_is_directory=True)
            self.assertIsNone(activation._bundle_checksum(bundle_source))


class RuntimeActivationOwnershipTest(unittest.TestCase):
    def test_status_reports_tampered_owned_destination_as_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                activation.activate("codex", "linked", str(source))
                state_path = activation._state_path("codex")
                state = activation._load_json(state_path)
                state["owned_paths"][0]["dest"] = str(root / "outside-runtime-home")
                activation._atomic_json(state_path, state)

                report = activation.status("codex")

                self.assertEqual(report["freshness"], "missing")
                self.assertEqual(report["user_changed"], [])

    def test_status_excludes_destinations_refresh_would_adopt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "runtime-home"
            source_root = root / "harness"
            source_root.mkdir()

            herdr_source = source_root / "integration.js"
            herdr_source.write_text(
                "// installed by herdr\n// HERDR_INTEGRATION_ID=opencode\n"
                "// HERDR_INTEGRATION_VERSION=13\n",
                encoding="utf-8",
            )
            herdr_dest = home / "plugins/integration.js"
            herdr_dest.parent.mkdir(parents=True)
            herdr_dest.write_text(
                "// installed by herdr\n// HERDR_INTEGRATION_ID=opencode\n"
                "// HERDR_INTEGRATION_VERSION=9\n",
                encoding="utf-8",
            )

            link_dest = home / "skills/adopted-link"
            link_dest.parent.mkdir(parents=True)
            link_target = source_root / "alternate-target"
            link_target.write_text("harness target\n", encoding="utf-8")
            link_dest.symlink_to(link_target)

            plugin_source = source_root / "plugin-source"
            plugin_source.mkdir()
            plugin_dest = home / "plugins/plugin-dir"
            (plugin_dest / ".codex-plugin").mkdir(parents=True)

            state = {"source_root": str(source_root), "active_root": str(source_root)}
            cases = (
                (
                    {"dest": str(herdr_dest), "kind": "copy", "source": str(herdr_source)},
                    {"dest": str(herdr_dest), "kind": "copy", "source": str(herdr_source)},
                ),
                (
                    {"dest": str(link_dest), "kind": "symlink", "source": str(source_root / "expected")},
                    {"dest": str(link_dest), "kind": "symlink", "source": str(source_root / "expected")},
                ),
                (
                    {"dest": str(plugin_dest), "kind": "copytree", "source": str(plugin_source)},
                    {"dest": str(plugin_dest), "kind": "copytree", "source": str(plugin_source)},
                ),
            )
            for prior, desired in cases:
                with self.subTest(kind=prior["kind"]):
                    self.assertEqual(
                        activation._preservable_user_changes(
                            state, [desired], {prior["dest"]: prior}
                        ),
                        {},
                    )

    def test_status_does_not_mark_carried_user_changed_stale_link_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                activation.activate("codex", "linked", str(source))
                state_path = activation._state_path("codex")
                state = activation._load_json(state_path)
                dest = activation.paths.runtime_home("codex") / "skills/obsolete-skill"
                dest.parent.mkdir(parents=True, exist_ok=True)
                outside = root / "user-owned-target"
                outside.write_text("user target\n", encoding="utf-8")
                dest.symlink_to(outside)
                state["owned_paths"].append({
                    "dest": str(dest),
                    "kind": "symlink",
                    "source": str(Path(state["active_root"]) / "obsolete-skill"),
                })
                activation._atomic_json(state_path, state)

                report = activation.status("codex")

                self.assertEqual(report["freshness"], "duplicate")
                self.assertIn(str(dest), report["user_changed"])
                self.assertTrue(dest.is_symlink())
                self.assertEqual(os.readlink(dest), str(outside))

    def test_refresh_carries_user_replaced_projection_through_deactivate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                activation.activate("codex", "linked", str(source))
                state_path = activation._state_path("codex")
                state = activation._load_json(state_path)
                owned = next(item for item in state["owned_paths"]
                             if item.get("kind") == "symlink")
                dest = Path(owned["dest"])
                original_source = owned["source"]
                # destructive-ok: reason=simulate a user replacing an activated link; boundary=dest leaf in this TemporaryDirectory's fixture HOME
                dest.unlink()
                user_bytes = b"user-owned replacement\n"
                dest.write_bytes(user_bytes)

                for _ in range(2):
                    refreshed = activation.refresh("codex")
                    self.assertEqual(dest.read_bytes(), user_bytes)
                    self.assertIn(str(dest), refreshed["preserved"])
                    self.assertIn(f"preserved user-changed: {dest}", refreshed["report_lines"])
                    self.assertIn(f"user-changed:{dest.relative_to(activation.paths.runtime_home('codex'))}",
                                  refreshed["duplicate_sources"])
                    carried = next(item for item in activation._load_json(state_path)["owned_paths"]
                                   if item["dest"] == str(dest))
                    self.assertEqual(carried["source"], original_source)
                    self.assertEqual(activation.status("codex")["user_changed"], [str(dest)])

                result = activation.deactivate("codex")
                self.assertIn(str(dest), result["preserved"])
                self.assertEqual(dest.read_bytes(), user_bytes)

    def test_refresh_preserves_repointed_and_dangling_owned_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                activation.activate("claude", "linked", str(source))
                state = activation._load_json(activation._state_path("claude"))
                owned = next(item for item in state["owned_paths"]
                             if item.get("kind") == "symlink")
                dest = Path(owned["dest"])
                external = root / "outside-user-target"
                external.write_text("user target\n", encoding="utf-8")
                # destructive-ok: reason=repoint this test's activated link; boundary=dest leaf in this TemporaryDirectory's fixture HOME
                dest.unlink()
                dest.symlink_to(external)

                for target in (external, root / "dangling-user-target"):
                    if dest.is_symlink():
                        # destructive-ok: reason=change this test's simulated user link target; boundary=dest leaf in this TemporaryDirectory's fixture HOME
                        dest.unlink()
                    dest.symlink_to(target)
                    raw_target = os.readlink(dest)
                    refreshed = activation.refresh("claude")
                    self.assertTrue(dest.is_symlink())
                    self.assertEqual(os.readlink(dest), raw_target)
                    self.assertIn(str(dest), refreshed["preserved"])

                result = activation.deactivate("claude")
                self.assertIn(str(dest), result["preserved"])
                self.assertTrue(dest.is_symlink())
                self.assertEqual(os.readlink(dest), str(root / "dangling-user-target"))

    def test_modified_copy_is_preserved_with_legacy_record_without_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                activation.activate("codex", "linked", str(source))
                state_path = activation._state_path("codex")
                state = activation._load_json(state_path)
                owned = next(item for item in state["owned_paths"]
                             if item.get("kind") == "copy")
                dest = Path(owned["dest"])
                legacy_item = dict(owned)
                legacy_item.pop("digest", None)
                state["owned_paths"] = [
                    legacy_item if item["dest"] == owned["dest"] else item
                    for item in state["owned_paths"]
                ]
                activation._atomic_json(state_path, state)
                user_bytes = b"modified copied integration\n"
                dest.write_bytes(user_bytes)

                refreshed = activation.refresh("codex")
                self.assertEqual(dest.read_bytes(), user_bytes)
                self.assertIn(str(dest), refreshed["preserved"])
                carried = next(item for item in activation._load_json(state_path)["owned_paths"]
                               if item["dest"] == str(dest))
                self.assertEqual(carried, legacy_item)
                result = activation.deactivate("codex")
                self.assertIn(str(dest), result["preserved"])
                self.assertEqual(dest.read_bytes(), user_bytes)

    def test_clean_refresh_replaces_all_owned_paths_without_preserving(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                first = activation.activate("codex", "linked", str(source))
                before = activation._load_json(activation._state_path("codex"))["owned_paths"]
                refreshed = activation.refresh("codex")
                after = activation._load_json(activation._state_path("codex"))["owned_paths"]
                self.assertEqual(first["preserved"], [])
                self.assertEqual(refreshed["preserved"], [])
                self.assertEqual(len(after), len(before))
                self.assertTrue(all("digest" in item for item in after if item["kind"] == "copy"))
                self.assertTrue(all(Path(item["dest"]).is_symlink()
                                    for item in after if item["kind"] == "symlink"))

    def test_capture_excludes_runtime_local_harness_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with fixture_env.patched_environment(root / "fixture", source):
                state_dir = activation.paths.harness_state_dir("codex")
                local_dispatch = state_dir / "dispatch" / "jobs.log"
                managed = state_dir / "managed-sessions" / "live.json"
                sibling = state_dir / "unknown-sibling" / "value"
                local_dispatch.parent.mkdir(parents=True)
                managed.parent.mkdir(parents=True)
                sibling.parent.mkdir(parents=True)
                local_dispatch.write_text("local row\n", encoding="utf-8")
                managed.write_text("session\n", encoding="utf-8")
                sibling.write_text("unknown\n", encoding="utf-8")
                snapshot = activation.capture_runtime_state("codex")
                self.addCleanup(activation.discard_runtime_state, snapshot)
                destinations = {Path(item["dest"]) for item in snapshot["records"]}
                self.assertNotIn(state_dir, destinations)
                self.assertNotIn(local_dispatch, destinations)
                self.assertNotIn(managed, destinations)
                self.assertNotIn(sibling, destinations)
                self.assertIn(state_dir / "transactions", destinations)

    def test_codex_projection_failure_before_state_commit_restores_native_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                runtime_home = activation.paths.runtime_home("codex")
                runtime_home.mkdir(parents=True, exist_ok=True)
                target = root / "fixture" / "shared" / "config.toml"
                target.parent.mkdir()
                original = ('[tui]\nstatus_line = ["project-name", "thread-id", "custom"]\n'
                            'status_line_use_colors = false\n').encode()
                target.write_bytes(original)
                config = runtime_home / "config.toml"
                link_target = os.path.relpath(target, runtime_home)
                config.symlink_to(link_target)
                original_prepare = activation._prepare_runtime_config

                def project_then_fail(*args, **kwargs):
                    original_prepare(*args, **kwargs)
                    self.assertTrue(config.is_symlink())
                    self.assertEqual(os.readlink(config), link_target)
                    projected = target.read_text(encoding="utf-8")
                    self.assertIn('status_line = ["project-name", "custom", "thread-id"]', projected)
                    self.assertIn(activation.CODEX_IDENTITY_BEGIN, projected)
                    self.assertIn('status_line_use_colors = false', projected)
                    self.assertEqual(backup.read_bytes(), original)
                    raise activation.ActivationError("injected after native projection")

                backup = config.with_name(activation.CODEX_IDENTITY_BACKUP)
                activation._prepare_runtime_config = project_then_fail
                try:
                    with self.assertRaisesRegex(activation.ActivationError, "injected"):
                        activation.activate("codex", "linked", str(source))
                finally:
                    activation._prepare_runtime_config = original_prepare
                self.assertTrue(config.is_symlink())
                self.assertEqual(os.readlink(config), link_target)
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse(backup.exists())
                self.assertFalse(activation._state_path("codex").exists())

    def test_runtime_snapshot_restores_codex_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                runtime_home = activation.paths.runtime_home("codex")
                runtime_home.mkdir(parents=True, exist_ok=True)
                target = root / "fixture" / "shared" / "config.toml"
                target.parent.mkdir()
                original = b'[tui]\nstatus_line = ["custom"]\n'
                target.write_bytes(original)
                config = runtime_home / "config.toml"
                link_target = os.path.relpath(target, runtime_home)
                config.symlink_to(link_target)

                snapshot = activation.capture_runtime_state("codex", str(source))
                self.addCleanup(activation.discard_runtime_state, snapshot)
                destinations = {Path(record["dest"]) for record in snapshot["records"]}
                self.assertIn(target.resolve(), destinations)
                target.write_bytes(b'[tui]\nstatus_line = ["activation-postimage"]\n')
                activation.seal_runtime_state(snapshot)

                activation.restore_runtime_state(snapshot)
                self.assertTrue(config.is_symlink())
                self.assertEqual(os.readlink(config), link_target)
                self.assertEqual(target.read_bytes(), original)


class NativeContextLifecycleTest(unittest.TestCase):
    """Activate, refresh, then deactivate: what activation added leaves, what the user had stays."""

    def _lifecycle(self, runtime, seed=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                home = activation.paths.runtime_home(runtime)
                name = "settings.json" if runtime == "claude" else "config.toml"
                config = home / name
                if seed is not None:
                    config.parent.mkdir(parents=True, exist_ok=True)
                    config.write_bytes(seed)
                activation.activate(runtime, "linked", str(source))
                activation.activate(runtime, "linked", str(source))
                activation.deactivate(runtime)
                backup = home / activation.CODEX_IDENTITY_BACKUP
                return (config.read_bytes() if config.exists() else None,
                        backup.read_bytes() if backup.exists() else None)

    def test_codex_clean_home_leaves_no_config_or_backup(self):
        config, backup = self._lifecycle("codex")
        self.assertIsNone(config)
        self.assertIsNone(backup)

    def test_codex_manual_window_survives_with_its_bytes(self):
        seed = b"# mine\nmodel_context_window = 800000\nmodel = \"x\"\n"
        config, backup = self._lifecycle("codex", seed)
        self.assertEqual(config, seed)
        self.assertEqual(backup, seed)

    def test_claude_clean_home_drops_the_added_window(self):
        config, _ = self._lifecycle("claude")
        env = json.loads(config).get("env", {})
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)

    def test_codex_refresh_and_uninstall_keep_user_edits_and_symlink(self):
        import tomllib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[2]
            with fixture_env.patched_environment(root / "fixture", source):
                home = activation.paths.runtime_home("codex")
                home.mkdir(parents=True, exist_ok=True)
                target = root / "fixture" / "shared.toml"
                target.write_text('model = "mine"\n[tui]\nstatus_line = ["custom"]\n', encoding="utf-8")
                target.chmod(0o640)
                config = home / "config.toml"
                link_target = os.path.relpath(target, home)
                config.symlink_to(link_target)
                activation.activate("codex", "linked", str(source))
                edited = target.read_text(encoding="utf-8").replace(
                    '["custom", "thread-id"]', '["custom", "later", "thread-id"]')
                edited = edited.replace('status_line_use_colors = true', 'status_line_use_colors = false')
                target.write_text(edited + '\n[projects."/mine"]\ntrust_level = "trusted"\n', encoding="utf-8")
                activation.activate("codex", "linked", str(source))
                before = target.read_bytes()
                args = installer.build_parser().parse_args(["uninstall", "codex", "--dry-run"])
                self.assertEqual(installer.cmd_uninstall(args)["exit"], 0)
                self.assertEqual(target.read_bytes(), before)
                args.dry_run = False
                self.assertEqual(installer.cmd_uninstall(args)["exit"], 0)
                self.assertTrue(config.is_symlink())
                self.assertEqual(os.readlink(config), link_target)
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                self.assertEqual(tomllib.loads(target.read_text(encoding="utf-8")), {
                    "model": "mine", "tui": {"status_line": ["custom", "later"],
                                               "status_line_use_colors": False},
                    "projects": {"/mine": {"trust_level": "trusted"}},
                })

    def test_claude_manual_window_survives(self):
        seed = json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                                   "USER_FLAG": "keep"}}).encode()
        config, _ = self._lifecycle("claude", seed)
        self.assertEqual(json.loads(config)["env"],
                         {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000", "USER_FLAG": "keep"})


class RuntimeActivationConflictPreflightTest(unittest.TestCase):
    def test_codex_conflict_refuses_before_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with fixture_env.patched_environment(root / "fixture", source):
                config = activation.paths.runtime_home("codex") / "config.toml"
                config.parent.mkdir(parents=True)
                config.write_text(
                    '[plugins."hearting-codex@fixture"]\nenabled = true\n',
                    encoding="utf-8",
                )
                before = config.read_bytes()
                with self.assertRaisesRegex(activation.ActivationError, "hearting-codex"):
                    activation.validate_request(
                        "codex", "activate", mode="linked", source=str(source)
                    )
                self.assertEqual(config.read_bytes(), before)

    def test_refresh_conflict_refuses_before_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with fixture_env.patched_environment(root / "fixture", source):
                state_path = activation._state_path("codex")
                state_path.parent.mkdir(parents=True)
                state_path.write_text(
                    json.dumps({"mode": "linked", "source_root": str(source)}),
                    encoding="utf-8",
                )
                config = activation.paths.runtime_home("codex") / "config.toml"
                config.write_text(
                    '[plugins."hearting-codex"]\n', encoding="utf-8"
                )
                with self.assertRaisesRegex(activation.ActivationError, "hearting-codex"):
                    activation.validate_request("codex", "refresh")

class ClaudeHookGroupIdentityTest(unittest.TestCase):
    """A hook group the user tuned is the same group, not a foreign one."""

    PROJECTION = {"type": "command", "command": "python3 \"$HOME/.claude/hooks/projection.py\"", "timeout": 5}
    NUDGE = {"type": "command", "command": "bash \"$HOME/.claude/hooks/nudge.sh\"", "timeout": 10}

    def _release(self, root: Path, name: str, prompt_groups: list) -> Path:
        release = root / name
        (release / "adapters/claude").mkdir(parents=True)
        (release / "adapters/claude/settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": prompt_groups},
            "statusLine": {"type": "command", "command": activation.CLAUDE_STATUSLINE_COMMAND},
            "autoMemoryEnabled": False,
            "env": {key: "1" for key in activation.CLAUDE_MANAGED_ENV_KEYS},
        }), encoding="utf-8")
        return release

    def _merge(self, config: Path, release: Path, previous_release: Path) -> dict:
        previous = {"managed_config": {"claude_hooks": json.loads(
            (previous_release / "adapters/claude/settings.json").read_text(encoding="utf-8")
        )["hooks"]}}
        original = activation._config_path
        activation._config_path = lambda *_args, **_kwargs: config
        try:
            return activation._merge_claude_settings(release, previous)
        finally:
            activation._config_path = original

    def test_a_tuned_group_is_kept_once_and_a_retired_one_leaves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._release(root, "old", [{"hooks": [self.PROJECTION]}, {"hooks": [self.NUDGE]}])
            new = self._release(root, "new", [{"hooks": [self.PROJECTION]}])
            tuned_projection = {"hooks": [dict(self.PROJECTION, timeout=30, **{"async": True})]}
            tuned_nudge = {"hooks": [dict(self.NUDGE, timeout=30)]}
            user_group = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                tuned_projection, tuned_nudge, user_group,
            ]}}), encoding="utf-8")

            result = self._merge(config, new, old)

            groups = json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            # The tuned projection group stays as the user left it and the
            # release copy is not appended beside it.
            self.assertEqual(groups, [tuned_projection, user_group])
            self.assertEqual(result["added"], 0)

            # A second install of the same release changes nothing.
            self._merge(config, new, new)
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"],
                [tuned_projection, user_group],
            )

    def test_an_untuned_group_still_takes_the_new_release_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._release(root, "old", [{"hooks": [self.PROJECTION]}])
            changed = {"hooks": [dict(self.PROJECTION, timeout=12)]}
            new = self._release(root, "new", [changed])
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                {"hooks": [self.PROJECTION]},
            ]}}), encoding="utf-8")

            self._merge(config, new, old)

            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"],
                [changed],
            )

    def test_a_group_the_user_trimmed_does_not_run_its_remaining_hook_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = {"matcher": "*", "hooks": [self.PROJECTION, self.NUDGE]}
            old = self._release(root, "old", [pair])
            new = self._release(root, "new", [pair])
            trimmed = {"matcher": "*", "hooks": [dict(self.PROJECTION, timeout=30)]}
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [trimmed]}}), encoding="utf-8")

            self._merge(config, new, old)

            groups = json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            # The kept hook stays tuned and only the missing one comes back.
            self.assertEqual(groups, [trimmed, {"matcher": "*", "hooks": [self.NUDGE]}])

    def test_uninstall_removes_tuned_and_trimmed_copies_but_not_user_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = [{"matcher": "*", "hooks": [self.PROJECTION, self.NUDGE]}]
            user_group = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
            mixed = {"matcher": "*", "hooks": [dict(self.NUDGE, timeout=1), {"type": "command", "command": "mine-too"}]}
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                {"matcher": "*", "hooks": [dict(self.PROJECTION, timeout=30)]}, mixed, user_group,
            ]}}), encoding="utf-8")
            original = activation._config_path
            activation._config_path = lambda *_args, **_kwargs: config
            try:
                activation._unmerge_claude_settings(
                    {"managed_config": {"claude_hooks": {"UserPromptSubmit": managed}}}, "global"
                )
            finally:
                activation._config_path = original

            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"],
                [{"matcher": "*", "hooks": [{"type": "command", "command": "mine-too"}]}, user_group],
            )


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_tmp(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class ClaudeManagedEnvTest(unittest.TestCase):
    """Claude's settings `env` blanks inherited caller names and foreign ids; activation installs it without clobbering."""

    KEYS = ("AGENT_DISPATCH_CALLER_HARNESS", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")

    def _release(self, root: Path) -> Path:
        release = root / "release"
        (release / "adapters/claude").mkdir(parents=True)
        shipped = json.loads((REPO_ROOT / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        (release / "adapters/claude/settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]},
            "statusLine": {"type": "command", "command": activation.CLAUDE_STATUSLINE_COMMAND},
            "autoMemoryEnabled": False,
            "env": shipped.get("env", {}),
        }), encoding="utf-8")
        return release

    def _merge(self, config: Path, release: Path, previous=None) -> dict:
        original = activation._config_path
        activation._config_path = lambda *_args, **_kwargs: config
        try:
            return activation._merge_claude_settings(release, previous)
        finally:
            activation._config_path = original

    def test_the_shipped_settings_manage_the_four_identity_keys(self):
        self.assertEqual(set(activation.CLAUDE_MANAGED_ENV_KEYS), set(self.KEYS))

    def test_merge_adds_the_keys_and_keeps_an_unrelated_user_env_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "settings.json"
            config.write_text(json.dumps({"env": {"MY_OWN": "keep"}}), encoding="utf-8")
            result = self._merge(config, release)
            env = json.loads(config.read_text(encoding="utf-8"))["env"]
            for key in self.KEYS:
                self.assertEqual(env[key], "", key)
            self.assertEqual(env["MY_OWN"], "keep")
            self.assertEqual(result["conflicts"], [])

    def test_a_user_changed_value_is_a_reported_conflict_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "settings.json"
            config.write_text(json.dumps({"env": {"AGENT_DISPATCH_CALLER_HARNESS": "mine"}}),
                              encoding="utf-8")
            result = self._merge(config, release)
            self.assertIn("env.AGENT_DISPATCH_CALLER_HARNESS", result["conflicts"])
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["env"]["AGENT_DISPATCH_CALLER_HARNESS"],
                "mine")


class CodexIdentityConfigTest(unittest.TestCase):
    """Codex clears inherited identity through one delimited block in `$CODEX_HOME/config.toml`."""

    BEGIN = "# >>> hearting harness identity (managed by runtime activation; edit outside this block) >>>"
    END = "# <<< hearting harness identity <<<"
    EXCLUDED = ("AGENT_DISPATCH_CALLER_HARNESS", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                "OPENCODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")

    def _release(self, root: Path) -> Path:
        release = root / "release"
        fragment = release / "adapters/codex/config"
        fragment.mkdir(parents=True)
        (fragment / "harness-identity.toml").write_bytes(
            (REPO_ROOT / "adapters/codex/config/harness-identity.toml").read_bytes())
        (fragment / "tui-statusline.toml").write_bytes(
            (REPO_ROOT / "adapters/codex/config/tui-statusline.toml").read_bytes())
        return release

    def _call(self, name, config: Path, *args):
        original = activation._config_path
        activation._config_path = lambda *_a, **_k: config
        try:
            return getattr(activation, name)(*args)
        finally:
            activation._config_path = original

    def _merge(self, config, release, previous=None):
        return self._call("_merge_codex_identity_config", config, release, previous, "global")

    def _unmerge(self, config, state, dry_run=False):
        return self._call("_unmerge_codex_identity_config", config, state, "global", dry_run)

    def _effective(self, config: Path):
        import tomllib
        policy = tomllib.loads(config.read_text(encoding="utf-8")).get("shell_environment_policy", {})
        return policy.get("set", {}), policy.get("filters", {})

    def _assert_clears_inherited(self, config: Path):
        values, filters = self._effective(config)
        self.assertNotIn("AGENT_DISPATCH_CALLER_HARNESS", values)
        self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", filters)
        for key in self.EXCLUDED:
            self.assertEqual(filters.get(key), "exclude", key)

    def test_an_absent_config_gets_the_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "home" / "config.toml"
            result = self._merge(config, self._release(root))
            self.assertEqual(result["kind"], "codex-config-merged")
            self.assertEqual(result["conflicts"], [])
            self.assertIn(self.BEGIN, config.read_text(encoding="utf-8"))
            self._assert_clears_inherited(config)

    def test_runtime_config_preparation_moves_thread_id_last_and_deactivation_restores_it(self):
        import tomllib
        import native_context
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "home" / "config.toml"
            original = ('[tui]\nstatus_line = ["project-name", "thread-id", "custom"]\n'
                        'status_line_use_colors = false\n')
            config.parent.mkdir(parents=True)
            config.write_text(original, encoding="utf-8")
            projection = {"native_context": {"codex": {}}, "report_lines": [],
                          "pre_projection_exists": True,
                          "pre_projection_bytes": original.encode("utf-8")}
            with mock.patch.object(activation, "_config_path", return_value=config), \
                 mock.patch.object(native_context, "project_context_window", return_value=projection):
                changes = activation._prepare_runtime_config("codex", release, None)
            self.assertTrue(changes[0]["tui_status_line_changed"])
            parsed_root = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertIs(parsed_root["check_for_update_on_startup"], False)
            parsed = parsed_root["tui"]
            self.assertEqual(parsed["status_line"], ["project-name", "custom", "thread-id"])
            self.assertIs(parsed["status_line_use_colors"], False)
            self.assertIn(self.BEGIN, config.read_text(encoding="utf-8"))

            state = {"managed_config": {"codex_identity": changes[0]["managed_config"],
                                        "codex_tui": changes[0]["tui_record"]}}
            with mock.patch.object(activation, "_config_path", return_value=config):
                activation._unmerge_codex_identity_config(state, "global")
            remaining = config.read_text(encoding="utf-8")
            parsed = tomllib.loads(remaining)["tui"]
            self.assertEqual(parsed["status_line"], ["project-name", "thread-id", "custom"])
            self.assertIs(parsed["status_line_use_colors"], False)
            self.assertNotIn(self.BEGIN, remaining)

    def test_runtime_activation_startup_absent_true_false_dry_run_and_receipt_carry(self):
        import native_context
        import stat
        import tomllib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            tui = ('[tui]\nstatus_line = ["thread-title", "git-branch", "context-used", '
                   '"model-with-reasoning", "five-hour-limit", "weekly-limit", "thread-id"]\n'
                   'status_line_use_colors = true\n')
            for label, explicit, expected in (("absent", "", False),
                                              ("true", "check_for_update_on_startup = true\n", True),
                                              ("false", "check_for_update_on_startup = false\n", False)):
                with self.subTest(label=label):
                    config = root / label / "config.toml"
                    config.parent.mkdir()
                    original = explicit + tui
                    config.write_text(original, encoding="utf-8")
                    projection = {"native_context": {"codex": {}}, "report_lines": [],
                                  "pre_projection_exists": True,
                                  "pre_projection_bytes": original.encode("utf-8")}
                    with mock.patch.object(activation, "_config_path", return_value=config), \
                         mock.patch.object(native_context, "project_context_window", return_value=projection):
                        first = activation._prepare_runtime_config("codex", release, None)
                    first_state = {"managed_config": {
                        "codex_identity": first[0].get("managed_config"),
                        "codex_tui": first[0].get("tui_record")}}
                    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
                    self.assertIs(parsed["check_for_update_on_startup"], expected)
                    first_receipt = (first[0].get("managed_config") or {}).get("startup_default")
                    self.assertEqual(bool(first_receipt), label == "absent")
                    once = config.read_bytes()

                    with mock.patch.object(activation, "_config_path", return_value=config), \
                         mock.patch.object(native_context, "project_context_window", return_value=projection):
                        second = activation._prepare_runtime_config("codex", release, first_state)
                    second_receipt = (second[0].get("managed_config") or {}).get("startup_default")
                    self.assertEqual(bool(second_receipt), label == "absent")
                    self.assertEqual(config.read_bytes(), once)

                    state = {"managed_config": {
                        "codex_identity": second[0].get("managed_config"),
                        "codex_tui": second[0].get("tui_record")}}
                    before_dry_run = (config.read_bytes(), stat.S_IMODE(config.stat().st_mode),
                                      config.stat().st_mtime_ns)
                    with mock.patch.object(activation, "_config_path", return_value=config):
                        activation._unmerge_codex_identity_config(state, "global", dry_run=True)
                    self.assertEqual((config.read_bytes(), stat.S_IMODE(config.stat().st_mode),
                                      config.stat().st_mtime_ns), before_dry_run)

                    with mock.patch.object(activation, "_config_path", return_value=config):
                        activation._unmerge_codex_identity_config(state, "global")
                    restored = config.read_text(encoding="utf-8")
                    self.assertEqual(restored, original)
                    self.assertEqual((config.parent / activation.CODEX_IDENTITY_BACKUP).read_bytes(),
                                     original.encode("utf-8"))

    def test_runtime_config_preparation_and_deactivation_preserve_symlink(self):
        import native_context
        import tomllib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            target = root / "shared" / "codex.toml"
            target.parent.mkdir()
            original = ('[tui]\nstatus_line = ["project-name", "thread-id", "custom"]\n'
                        'status_line_use_colors = false\n').encode()
            target.write_bytes(original)
            config = root / "home" / "config.toml"
            config.parent.mkdir()
            link_target = os.path.relpath(target, config.parent)
            config.symlink_to(link_target)
            projection = {"native_context": {"codex": {}}, "report_lines": [],
                          "pre_projection_exists": True,
                          "pre_projection_bytes": original}
            with mock.patch.object(activation, "_config_path", return_value=config), \
                 mock.patch.object(native_context, "project_context_window", return_value=projection):
                changes = activation._prepare_runtime_config("codex", release, None)
            self.assertTrue(changes[0]["tui_status_line_changed"])
            self.assertTrue(config.is_symlink())
            self.assertEqual(os.readlink(config), link_target)
            applied = target.read_text(encoding="utf-8")
            parsed_root = tomllib.loads(applied)
            self.assertIs(parsed_root["check_for_update_on_startup"], False)
            parsed = parsed_root["tui"]
            self.assertEqual(parsed["status_line"], ["project-name", "custom", "thread-id"])
            self.assertIs(parsed["status_line_use_colors"], False)
            self.assertIn(self.BEGIN, applied)

            state = {"managed_config": {"codex_identity": changes[0]["managed_config"],
                                        "codex_tui": changes[0]["tui_record"]}}
            with mock.patch.object(activation, "_config_path", return_value=config):
                activation._unmerge_codex_identity_config(state, "global")
            remaining = target.read_text(encoding="utf-8")
            parsed = tomllib.loads(remaining)["tui"]
            self.assertEqual(parsed["status_line"], ["project-name", "thread-id", "custom"])
            self.assertIs(parsed["status_line_use_colors"], False)
            self.assertNotIn(self.BEGIN, remaining)
            self.assertTrue(config.is_symlink())
            self.assertEqual(os.readlink(config), link_target)

    def test_native_window_restores_without_identity_record_and_deletes_created_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("model_context_window = 800000\n", encoding="utf-8")
            state = {"managed_config": {"native_context": {"codex": {
                "file_created": True, "keys": {"model_context_window": {
                    "baseline": {"present": False}, "projected": 800000}}}}}}
            self.assertEqual(self._unmerge(config, state), [str(config)])
            self.assertFalse(config.exists())

    def test_native_window_restores_independently_when_identity_block_was_edited(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            edited = self.BEGIN + "\n[shell_environment_policy.filters]\n# user edit\n" + self.END + "\n"
            config.write_text("model_context_window = 800000\n\n" + edited, encoding="utf-8")
            state = {"managed_config": {
                "codex_identity": {"inserted": self.BEGIN + "\nold block\n" + self.END + "\n"},
                "native_context": {"codex": {"file_created": False, "keys": {
                    "model_context_window": {"baseline": {"present": True, "value": 640000},
                                              "projected": 800000}}}}}}
            self._unmerge(config, state)
            text = config.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 640000", text)
            self.assertIn("# user edit", text)

    def test_empty_original_config_is_retained_after_window_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("model_context_window = 800000\n", encoding="utf-8")
            state = {"managed_config": {"native_context": {"codex": {
                "file_created": False, "keys": {"model_context_window": {
                    "baseline": {"present": False}, "projected": 800000}}}}}}
            self._unmerge(config, state)
            self.assertTrue(config.is_file())
            self.assertEqual(config.read_text(encoding="utf-8"), "")

    def test_legacy_record_without_identity_block_keeps_user_empty_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("", encoding="utf-8")
            self.assertEqual(self._unmerge(config, {"managed_config": {}}), [])
            self.assertTrue(config.is_file())

    def test_dry_run_leaves_native_config_bytes_mode_and_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_bytes(b'model_context_window = 800000 # keep\r\nmodel = "x"\r\n')
            config.chmod(0o640)
            before = (config.read_bytes(), config.stat().st_mode, config.stat().st_mtime_ns)
            state = {"managed_config": {"native_context": {"codex": {
                "file_created": False, "keys": {"model_context_window": {
                    "baseline": {"present": False}, "projected": 800000}}}}}}
            self._unmerge(config, state, dry_run=True)
            self.assertEqual((config.read_bytes(), config.stat().st_mode,
                              config.stat().st_mtime_ns), before)

    def test_the_block_is_appended_after_user_content_and_the_rest_stays_runtime_owned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n'
            config.write_text(original, encoding="utf-8")
            self._merge(config, self._release(root))
            text = config.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original))
            self._assert_clears_inherited(config)
            self.assertTrue((root / "config.toml.pre-harness-identity").is_file())
            self.assertEqual((root / "config.toml.pre-harness-identity").read_text(encoding="utf-8"), original)
            import tomllib
            self.assertIs(tomllib.loads(text)["features"]["default_mode_request_user_input"], True)

    def test_a_second_merge_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('model = "x"\n', encoding="utf-8")
            release = self._release(root)
            first = self._merge(config, release)
            once = config.read_bytes()
            self._merge(config, release, {"managed_config": {"codex_identity": first["managed_config"]}})
            self.assertEqual(config.read_bytes(), once)

    def test_a_user_policy_with_legacy_arrays_or_a_partial_filters_table_is_a_conflict_and_unchanged(self):
        for label, body in (
            ("exclude", '[shell_environment_policy]\nexclude = ["AWS_*"]\n'),
            ("include_only", '[shell_environment_policy]\ninclude_only = ["PATH"]\n'),
            ("inline filters", '[shell_environment_policy]\nfilters = { "FOO" = "exclude" }\n'),
            ("partial filters", '[shell_environment_policy.filters]\n"FOO" = "exclude"\n'),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text(body, encoding="utf-8")
                result = self._merge(config, self._release(root))
                self.assertTrue(result["conflicts"], label)
                self.assertEqual(config.read_text(encoding="utf-8"), body)

    def test_a_user_table_that_already_has_the_effective_values_is_satisfied_without_a_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            body = ('[features]\ndefault_mode_request_user_input = true\n\n'
                    '[shell_environment_policy]\ninherit = "core"\n'
                    '[shell_environment_policy.set]\nFOO = "1"\n'
                    '[shell_environment_policy.filters]\n'
                    + "".join('"%s" = "exclude"\n' % key for key in self.EXCLUDED))
            config.write_text(body, encoding="utf-8")
            result = self._merge(config, self._release(root))
            self.assertEqual(result["conflicts"], [])
            self.assertIsNone(result["managed_config"])
            self.assertEqual(config.read_text(encoding="utf-8"), body)

    def test_a_user_set_table_is_not_a_conflict_and_stays_untouched(self):
        # The block defines only `filters`; a user `set` (even one naming a caller on purpose)
        # lives beside it and Codex applies `set` after the exclusions.
        for body in ('[shell_environment_policy]\nset = { FOO = "1" }\n',
                     '[shell_environment_policy.set]\nAGENT_DISPATCH_CALLER_HARNESS = "claude"\n'):
            with self.subTest(body), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text(body, encoding="utf-8")
                result = self._merge(config, self._release(root))
                self.assertEqual(result["conflicts"], [])
                self.assertTrue(config.read_text(encoding="utf-8").startswith(body))
                self.assertEqual(self._effective(config)[0], self._effective(
                    _write_tmp(root / "user-only.toml", body))[0])
                _, filters = self._effective(config)
                for key in self.EXCLUDED:
                    self.assertEqual(filters.get(key), "exclude", key)

    def test_a_block_from_the_previous_declaration_is_replaced_not_stacked(self):
        # An earlier build of this block also set the caller name; refresh swaps in the current text.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            release = self._release(root)
            old_inserted = (
                "\n" + self.BEGIN + '\n[shell_environment_policy.set]\nAGENT_DISPATCH_CALLER_HARNESS = "codex"\n'
                '[shell_environment_policy.filters]\n"CLAUDE_CODE_SESSION_ID" = "exclude"\n' + self.END + "\n")
            config.write_text('model = "x"\n' + old_inserted, encoding="utf-8")
            previous = {"managed_config": {"codex_identity": {"inserted": old_inserted}}}
            result = self._merge(config, release, previous)
            self.assertEqual(result["conflicts"], [])
            text = config.read_text(encoding="utf-8")
            self.assertEqual(text.count(self.BEGIN), 1)
            self.assertNotIn("AGENT_DISPATCH_CALLER_HARNESS = ", text)
            self._assert_clears_inherited(config)

    def test_a_user_policy_table_without_set_or_filters_gets_the_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('[shell_environment_policy]\ninherit = "all"\n', encoding="utf-8")
            result = self._merge(config, self._release(root))
            self.assertEqual(result["conflicts"], [])
            self._assert_clears_inherited(config)

    def test_deactivate_removes_only_the_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n'
            config.write_text(original, encoding="utf-8")
            result = self._merge(config, self._release(root))
            changed = self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(changed, [str(config)])
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_deactivate_removes_a_config_activation_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "home" / "config.toml"
            result = self._merge(config, self._release(root))
            changed = self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(changed, [str(config)])
            self.assertFalse(config.exists())

    def test_deactivate_keeps_an_empty_config_the_user_had(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text("", encoding="utf-8")
            result = self._merge(config, self._release(root))
            self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertTrue(config.is_file())
            self.assertEqual(config.read_text(encoding="utf-8"), "")

    def test_deactivate_leaves_a_block_the_user_edited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            result = self._merge(config, self._release(root))
            edited = config.read_text(encoding="utf-8").replace('"CLAUDECODE"', '"MINE"')
            config.write_text(edited, encoding="utf-8")
            # Each insertion is unmerged on its own: the edited block stays exactly as the
            # user left it, while the untouched question feature line still goes.
            expected = activation._codex_feature_remove(edited, result["managed_config"]["feature"])
            self.assertNotEqual(expected, edited)
            self.assertIn('"MINE"', expected)
            self.assertEqual(
                self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}}),
                [str(config)])
            self.assertEqual(config.read_text(encoding="utf-8"), expected)

    def test_invalid_toml_takes_the_existing_activation_error_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text("not = [valid", encoding="utf-8")
            with self.assertRaisesRegex(activation.ActivationError, "invalid Codex config"):
                self._merge(config, self._release(root))
            self.assertEqual(config.read_text(encoding="utf-8"), "not = [valid")

    def test_a_release_without_the_fragment_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            empty_release = root / "old-release"
            empty_release.mkdir()
            result = self._merge(config, empty_release)
            self.assertIsNone(result["managed_config"])
            self.assertFalse(config.exists())

    def test_health_reports_missing_then_clean_and_names_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "config.toml"
            config.write_text('model = "x"\n', encoding="utf-8")
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"), (True, []))
            self._merge(config, release)
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"), (False, []))
            config.write_text('[shell_environment_policy]\nexclude = ["A"]\n', encoding="utf-8")
            missing, conflicts = self._call("_codex_identity_health", config, release, "global")
            self.assertTrue(conflicts)

    def test_explicit_feature_true_is_respected_and_false_or_non_boolean_is_preserved(self):
        for value, expected_conflict in (("true", False), ("false", True), ('"yes"', True)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                original = f'[features]\nother = "kept"\ndefault_mode_request_user_input = {value}\n'
                config.write_text(original, encoding="utf-8")
                result = self._merge(config, self._release(root))
                self.assertEqual(bool(result["conflicts"]), expected_conflict)
                if expected_conflict:
                    self.assertEqual(result["conflicts"], ["features.default_mode_request_user_input"])
                    content = config.read_bytes()
                    self.assertTrue(content.startswith(original.encode("utf-8")))
                    self.assertIn(self.BEGIN.encode("utf-8"), content)
                    import tomllib
                    parsed = tomllib.loads(content.decode("utf-8"))
                    self.assertEqual(parsed["features"]["default_mode_request_user_input"],
                                     tomllib.loads(original)["features"]["default_mode_request_user_input"])
                    self.assertIsNone(result["managed_config"]["feature"])
                else:
                    self.assertIsNone(result["managed_config"]["feature"])
                    import tomllib
                    self.assertIs(tomllib.loads(config.read_text(encoding="utf-8"))
                                  ["features"]["default_mode_request_user_input"], True)

    def test_deactivate_removes_only_the_inserted_feature_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = '[features]\napps = false\n\n[model]\nname = "x"\n'
            config.write_text(original, encoding="utf-8")
            result = self._merge(config, self._release(root))
            self.assertEqual(result["conflicts"], [])
            self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_inline_or_dotted_feature_forms_are_respected_when_true_and_conflicts_when_unsafe(self):
        for original, conflict in (
                ('features = { default_mode_request_user_input = true }\n', False),
                ('features.default_mode_request_user_input = true\n', False),
                ('features = { apps = false }\n', True),
                ('features.apps = false\n', True)):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text(original, encoding="utf-8")
                result = self._merge(config, self._release(root))
                if conflict:
                    self.assertEqual(result["conflicts"], ["features.default_mode_request_user_input"])
                    content = config.read_bytes()
                    self.assertTrue(content.startswith(original.encode("utf-8")))
                    self.assertIn(self.BEGIN.encode("utf-8"), content)
                    parsed = self._feature_parsed(config)
                    self.assertIs(parsed["features"]["apps"], False)
                    self.assertIsNone(result["managed_config"]["feature"])
                else:
                    self.assertEqual(result["conflicts"], [])
                    self.assertTrue(config.read_text(encoding="utf-8").startswith(original))
                    self.assertIsNone(result["managed_config"]["feature"])

    def _feature_parsed(self, config):
        import tomllib
        return tomllib.loads(config.read_text(encoding="utf-8"))

    def test_identity_satisfied_but_feature_missing_inserts_and_records_only_the_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            body = ('[shell_environment_policy]\ninherit = "core"\n'
                    '[shell_environment_policy.set]\nFOO = "1"\n'
                    '[shell_environment_policy.filters]\n'
                    + "".join('"%s" = "exclude"\n' % key for key in self.EXCLUDED))
            config.write_text(body, encoding="utf-8")
            result = self._merge(config, self._release(root))
            self.assertEqual(result["conflicts"], [])
            self.assertIsNone(result["managed_config"]["inserted"])
            self.assertTrue(result["managed_config"]["feature"])
            self.assertNotIn(self.BEGIN, config.read_text(encoding="utf-8"))
            self.assertIs(self._feature_parsed(config)["features"]["default_mode_request_user_input"], True)
            self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(config.read_text(encoding="utf-8"), body)

    def test_remerge_never_moves_or_deletes_a_user_key_added_under_the_feature_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            release = self._release(root)
            config.write_text('model = "x"\n', encoding="utf-8")
            first = self._merge(config, release)
            self.assertTrue(first["managed_config"]["feature"]["header"])
            edited = config.read_text(encoding="utf-8").replace(
                "default_mode_request_user_input = true\n",
                "default_mode_request_user_input = true\nmine = 1\n", 1)
            config.write_text(edited, encoding="utf-8")
            previous = {"managed_config": {"codex_identity": first["managed_config"]}}
            second = self._merge(config, release, previous)
            self.assertEqual(config.read_text(encoding="utf-8"), edited)
            parsed = self._feature_parsed(config)
            self.assertEqual(parsed["features"], {"default_mode_request_user_input": True, "mine": 1})
            self._unmerge(config, {"managed_config": {"codex_identity": second["managed_config"]}})
            parsed = self._feature_parsed(config)
            self.assertEqual(parsed["features"], {"mine": 1})
            self.assertEqual(parsed["model"], "x")
            self.assertNotIn(self.BEGIN, config.read_text(encoding="utf-8"))

    def test_deactivate_removes_an_activation_created_header_once_it_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'model = "x"'
            config.write_text(original, encoding="utf-8")
            result = self._merge(config, self._release(root))
            self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_feature_header_spellings_lookalikes_and_other_tables_stay_valid(self):
        cases = {
            "spaced header": '[ features ]\napps = false\n',
            "lookalike in multiline string": 'note = """\n[features]\n"""\n',
            "same key under another table": '[profiles.x]\ndefault_mode_request_user_input = false\n',
            "lookalike string then real header": 'note = """\n[features]\n"""\n\n[features]\napps = false\n',
            "quoted header": '["features"]\napps = false\n',
            "subtable only": '[features.sub]\na = 1\n',
        }
        for label, original in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                import tomllib
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text(original, encoding="utf-8")
                result = self._merge(config, self._release(root))
                self.assertEqual(result["conflicts"], [], label)
                before = tomllib.loads(original)
                after = self._feature_parsed(config)
                self.assertIs(after["features"]["default_mode_request_user_input"], True)
                for key, value in before.items():
                    if key == "features":
                        for sub, sub_value in value.items():
                            self.assertEqual(after["features"][sub], sub_value)
                    else:
                        self.assertEqual(after[key], value, label)
                self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
                self.assertEqual(config.read_text(encoding="utf-8"), original, label)

    def test_deactivate_leaves_a_feature_the_user_changed_to_false_or_removed(self):
        for label, edit in (("false", lambda text: text.replace(
                "default_mode_request_user_input = true", "default_mode_request_user_input = false")),
                ("removed", lambda text: text.replace(
                    "default_mode_request_user_input = true\n", ""))):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text('[features]\napps = false\n', encoding="utf-8")
                result = self._merge(config, self._release(root))
                edited = edit(config.read_text(encoding="utf-8"))
                config.write_text(edited, encoding="utf-8")
                record = {"managed_config": {"codex_identity": {**result["managed_config"], "inserted": None}}}
                self.assertEqual(self._unmerge(config, record), [])
                self.assertEqual(config.read_text(encoding="utf-8"), edited)

    def test_a_feature_conflict_after_activation_drops_the_feature_receipt_and_keeps_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            release = self._release(root)
            config.write_text('[features]\napps = false\n', encoding="utf-8")
            first = self._merge(config, release)
            edited = config.read_text(encoding="utf-8").replace(
                "default_mode_request_user_input = true", "default_mode_request_user_input = false")
            config.write_text(edited, encoding="utf-8")
            second = self._merge(config, release, {"managed_config": {"codex_identity": first["managed_config"]}})
            self.assertEqual(second["conflicts"], ["features.default_mode_request_user_input"])
            self.assertEqual(config.read_text(encoding="utf-8"), edited)
            self.assertEqual(second["managed_config"]["inserted"], first["managed_config"]["inserted"])
            self.assertIsNone(second["managed_config"]["feature"])
            self._unmerge(config, {"managed_config": {"codex_identity": second["managed_config"]}})
            self.assertIn("default_mode_request_user_input = false", config.read_text(encoding="utf-8"))
            self.assertNotIn(self.BEGIN, config.read_text(encoding="utf-8"))

    def test_health_names_a_feature_conflict_and_reports_a_missing_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "config.toml"
            config.write_text('model = "x"\n', encoding="utf-8")
            self._merge(config, release)
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"), (False, []))
            text = config.read_text(encoding="utf-8")
            config.write_text(text.replace("default_mode_request_user_input = true\n", ""), encoding="utf-8")
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"), (True, []))
            config.write_text(text.replace("= true", "= false"), encoding="utf-8")
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"),
                             (False, ["features.default_mode_request_user_input"]))


class OpencodeTuiSeedTest(unittest.TestCase):
    """The TUI identity entry registers through an absent-only tui.json seed."""

    def _release(self, root: Path) -> Path:
        release = root / "release"
        template_dir = release / "adapters" / "opencode" / "tui"
        template_dir.mkdir(parents=True)
        (template_dir / "tui.json").write_bytes(
            (REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())
        return release

    def _seed(self, home, release, previous=None):
        original = activation.paths.runtime_home
        activation.paths.runtime_home = lambda runtime, scope="global": home
        try:
            return activation._seed_opencode_tui_entry(release, previous, "global")
        finally:
            activation.paths.runtime_home = original

    def _unseed(self, home, state, dry_run=False):
        original = activation.paths.runtime_home
        activation.paths.runtime_home = lambda runtime, scope="global": home
        try:
            return activation._unseed_opencode_tui_entry(state, "global", dry_run=dry_run)
        finally:
            activation.paths.runtime_home = original

    def test_absent_config_gets_the_seed_and_a_second_seed_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = self._release(root)
            first = self._seed(home, release)
            self.assertEqual(first["status"], "seeded")
            target = home / "tui.json"
            self.assertEqual(target.read_bytes(),
                             (REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())
            second = self._seed(home, release, {"managed_config": {"opencode_tui": first["managed_config"]}})
            self.assertEqual(second["status"], "present")
            self.assertEqual(target.read_bytes(),
                             (REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())

    def test_an_existing_user_file_stays_byte_identical_and_unowned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            target = home / "tui.json"
            body = '{"plugin": ["user-entry.ts"], "theme": "user-theme"}\n'
            target.write_text(body, encoding="utf-8")
            release = self._release(root)
            result = self._seed(home, release)
            self.assertEqual(result["status"], "user-managed")
            self.assertIsNone(result["managed_config"])
            self.assertEqual(target.read_text(encoding="utf-8"), body)

    def test_same_bytes_without_a_prior_record_are_still_unowned(self):
        # F3: byte equality alone never creates ownership, so uninstall
        # must not select the file.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            target = home / "tui.json"
            target.write_bytes((REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())
            release = self._release(root)
            result = self._seed(home, release)
            self.assertEqual(result["status"], "user-managed")
            self.assertIsNone(result["managed_config"])
            self.assertEqual(self._unseed(home, {"managed_config": {}}, dry_run=True), [])
            self.assertTrue(target.is_file())

    def test_a_symlink_is_never_claimed_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            real = root / "real-tui.json"
            real.write_text('{"plugin": []}\n', encoding="utf-8")
            (home / "tui.json").symlink_to(real)
            release = self._release(root)
            result = self._seed(home, release)
            self.assertEqual(result["status"], "user-managed")
            self.assertIsNone(result["managed_config"])
            state = {"managed_config": {"opencode_tui": {"seeded": real.read_text(encoding="utf-8")}}}
            self.assertEqual(self._unseed(home, state), [])
            self.assertTrue((home / "tui.json").is_symlink())

    def test_unseed_removes_only_the_exact_bytes_it_wrote(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = self._release(root)
            first = self._seed(home, release)
            state = {"managed_config": {"opencode_tui": first["managed_config"]}}
            self.assertEqual(self._unseed(home, state, dry_run=True), [str(home / "tui.json")])
            self.assertTrue((home / "tui.json").is_file())
            self.assertEqual(self._unseed(home, state), [str(home / "tui.json")])
            self.assertFalse((home / "tui.json").exists())
            again = self._seed(home, release)
            edited = (home / "tui.json").read_text(encoding="utf-8") + "\n"
            (home / "tui.json").write_text(edited, encoding="utf-8")
            kept = {"managed_config": {"opencode_tui": again["managed_config"]}}
            self.assertEqual(self._unseed(home, kept), [])
            self.assertEqual((home / "tui.json").read_text(encoding="utf-8"), edited)



class HerdrIntegrationAdoptionTest(unittest.TestCase):
    def test_official_herdr_assets_are_copied_to_runtime_homes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "runtime-home"
            with mock.patch.object(activation.paths, "runtime_home", return_value=home):
                expected = {
                    "claude": {"hooks/herdr-agent-state.sh"},
                    "codex": {"herdr-agent-state.sh"},
                    "opencode": {"plugins/herdr-agent-state.js", "herdr-tui-session.js",
                                 "herdr-opencode/tui.js"},
                }
                for runtime, dests in expected.items():
                    entries = activation._linked_entries(runtime, REPO_ROOT, "global")
                    assets = [entry for entry in entries
                              if Path(entry["dest"]).relative_to(home).as_posix() in dests]
                    self.assertEqual({Path(entry["dest"]).relative_to(home).as_posix()
                                      for entry in assets}, dests)
                    self.assertEqual(len(assets), len(dests), assets)
                    self.assertTrue(all(entry["kind"] == "copy" for entry in assets), assets)

    def _write(self, path, *lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_only_herdrs_own_file_for_the_same_integration_is_adopted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._write(root / "src/herdr-agent-state.js", "// installed by herdr",
                                 "// HERDR_INTEGRATION_ID=opencode", "// HERDR_INTEGRATION_VERSION=13")
            dest = self._write(root / "home/plugins/herdr-agent-state.js", "// installed by herdr",
                               "// managed by herdr; reinstalling or updating the integration overwrites this file.",
                               "// HERDR_INTEGRATION_ID=opencode", "// HERDR_INTEGRATION_VERSION=9")
            activation._safe_existing_dest(dest, "copy", set(), [], source)
            refused = {
                "other integration": ("// installed by herdr", "// HERDR_INTEGRATION_ID=opencode-tui"),
                "user file": ("// my own plugin", "// HERDR_INTEGRATION_ID=opencode"),
            }
            for name, lines in refused.items():
                with self.subTest(name):
                    self._write(dest, *lines)
                    with self.assertRaisesRegex(activation.ActivationError, "destination collision"):
                        activation._safe_existing_dest(dest, "copy", set(), [], source)
            self._write(dest, "// installed by herdr", "// HERDR_INTEGRATION_ID=opencode")
            plain = self._write(root / "src/plain.js", "// HERDR_INTEGRATION_ID=opencode")
            for name, src in (("source without herdr header", plain), ("no source", None)):
                with self.subTest(name):
                    with self.assertRaisesRegex(activation.ActivationError, "destination collision"):
                        activation._safe_existing_dest(dest, "copy", set(), [], src)

_TEST_HOME = None
_TEST_ENVIRONMENT = None
_PREVIOUS_TEMP_DIR = None


def setUpModule():
    """Run the complete module against an isolated default runtime home.

    A few tests exercise public installer entry points without installing their
    own fixture environment. Module-level setup keeps those tests safe under
    both direct execution and unittest discovery.
    """
    global _TEST_HOME, _TEST_ENVIRONMENT, _PREVIOUS_TEMP_DIR
    _TEST_HOME = tempfile.TemporaryDirectory(prefix="ra-")
    _TEST_ENVIRONMENT = fixture_env.patched_environment(
        Path(_TEST_HOME.name) / "f", REPO_ROOT
    )
    _TEST_ENVIRONMENT.__enter__()
    temporary_root = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "t"
    temporary_root.mkdir(parents=True, exist_ok=True)
    _PREVIOUS_TEMP_DIR = tempfile.tempdir
    tempfile.tempdir = str(temporary_root)


def tearDownModule():
    global _TEST_HOME, _TEST_ENVIRONMENT, _PREVIOUS_TEMP_DIR
    tempfile.tempdir = _PREVIOUS_TEMP_DIR
    _PREVIOUS_TEMP_DIR = None
    if _TEST_ENVIRONMENT is not None:
        _TEST_ENVIRONMENT.__exit__(None, None, None)
        _TEST_ENVIRONMENT = None
    if _TEST_HOME is not None:
        _TEST_HOME.cleanup()
        _TEST_HOME = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
