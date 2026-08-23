#!/usr/bin/env python3
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_activation as activation  # noqa: E402
import installer  # noqa: E402


class RuntimeSnapshotTest(unittest.TestCase):
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
            listener.bind(str(socket_path))
            try:
                record = activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )
                self.assertFalse(
                    (Path(record["backup"]) / "managed-sessions").exists())
                current.write_text("after\n", encoding="utf-8")
                (state / "new-projection").write_text("remove me\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
