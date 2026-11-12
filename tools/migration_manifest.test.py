#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "migration-manifest.py"
SPEC = importlib.util.spec_from_file_location("migration_manifest", TOOL)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def _make_fixture(base: Path) -> Path:
    root = base / "fixture-root"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    (root / "sub" / "b.txt").write_text(
        "old ref .claude_reports and /home/someone/project\n", encoding="utf-8"
    )
    (root / "sub" / "link.txt").symlink_to(root / "a.txt")
    try:
        os.chmod(root / "sub", 0o755)
    except OSError:
        pass
    return root


class MigrationManifestTest(unittest.TestCase):
    # regression ⑤: fixed fixture, two runs, byte-identical JSONL.
    def test_determinism_across_two_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            out_a = base / "out-a"
            out_b = base / "out-b"
            for out_dir in (out_a, out_b):
                result = subprocess.run(
                    [sys.executable, str(TOOL), "scan", "--root", str(root), "--out", str(out_dir)],
                    text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            jsonl_a = (out_a / f"{root.name}.jsonl").read_bytes()
            jsonl_b = (out_b / f"{root.name}.jsonl").read_bytes()
            self.assertEqual(jsonl_a, jsonl_b)
            # run_meta carries the only timestamp -- excluding it from the
            # byte-identity check above is deliberate, not an oversight.
            meta = json.loads((out_a / "run_meta.json").read_text())
            self.assertIn("generated_at_unix", meta)

    def test_out_inside_investigated_root_is_rejected_before_scanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            out_dir = root / "manifest-out"
            with self.assertRaises(M.ManifestError):
                M._reject_out_inside_investigated(out_dir, root)

    def test_symlink_loop_completes_without_following(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            (root / "sub" / "loop").symlink_to(root)  # symlink back to root itself
            records, summary = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key=root.name,
            )
            self.assertTrue(summary["complete"])
            kinds = {r["path"]: r["record_type"] for r in records}
            self.assertEqual(kinds["sub/loop"], "symlink")

    def test_unreadable_directory_becomes_error_record_and_root_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                self.skipTest("root bypasses directory permission checks")
            blocked = root / "sub" / "blocked-dir"
            blocked.mkdir()
            (blocked / "hidden.txt").write_text("secret", encoding="utf-8")
            os.chmod(blocked, 0o000)
            try:
                records, summary = M.scan_root(
                    root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                    mirror_map={}, root_key=root.name,
                )
            finally:
                os.chmod(blocked, 0o755)
            error_rows = [r for r in records if r["record_type"] == "error"]
            self.assertTrue(error_rows, records)
            self.assertFalse(summary["complete"])

    def test_unreadable_file_is_recorded_but_scan_still_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                self.skipTest("root bypasses file permission checks")
            blocked = root / "sub" / "blocked.txt"
            blocked.write_text("secret", encoding="utf-8")
            os.chmod(blocked, 0o000)
            try:
                records, summary = M.scan_root(
                    root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                    mirror_map={}, root_key=root.name,
                )
            finally:
                os.chmod(blocked, 0o644)
            blocked_row = next(r for r in records if r["path"] == "sub/blocked.txt")
            self.assertTrue(blocked_row.get("ref_scan_skipped"))

    def test_scan_never_writes_inside_the_investigated_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            before_mtimes = {
                str(p.relative_to(root)): os.lstat(p).st_mtime_ns for p in root.rglob("*")
            }
            out_dir = base / "out"
            result = subprocess.run(
                [sys.executable, str(TOOL), "scan", "--root", str(root), "--out", str(out_dir), "--hash"],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            after_mtimes = {
                str(p.relative_to(root)): os.lstat(p).st_mtime_ns for p in root.rglob("*")
            }
            self.assertEqual(before, after)
            self.assertEqual(before_mtimes, after_mtimes)

    def test_sweep_produces_one_jsonl_per_root_plus_summary_and_run_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            roots_yaml = base / "roots.yaml"
            roots_yaml.write_text(
                "roots:\n"
                f"  - key: fixture-root\n    path: {root}\n"
                f"  - key: fixture-sub\n    path: {root / 'sub'}\n",
                encoding="utf-8",
            )
            out_dir = base / "sweep-out"
            result = subprocess.run(
                [sys.executable, str(TOOL), "sweep", "--roots", str(roots_yaml), "--out", str(out_dir)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out_dir / "fixture-root.jsonl").is_file())
            self.assertTrue((out_dir / "fixture-sub.jsonl").is_file())
            self.assertTrue((out_dir / "summary.md").is_file())
            self.assertTrue((out_dir / "run_meta.json").is_file())

    def test_strict_yaml_rejects_anchors_and_tags(self):
        with self.assertRaises(M.ManifestError):
            M._strict_yaml_load("roots:\n  - key: &anchor a\n    path: /tmp\n")
        with self.assertRaises(M.ManifestError):
            M._strict_yaml_load("roots: !!python/object:foo\n")

    def test_strict_yaml_parses_nested_mapping_and_sequence(self):
        text = (
            "a:\n"
            "  b: 1\n"
            "  c: two\n"
            "roots:\n"
            "  - key: x\n    path: /tmp/x\n"
            "  - key: y\n    path: /tmp/y\n"
        )
        data = M._strict_yaml_load(text)
        self.assertEqual(data["a"]["b"], "1")
        self.assertEqual(data["a"]["c"], "two")
        self.assertEqual(data["roots"], [
            {"key": "x", "path": "/tmp/x"},
            {"key": "y", "path": "/tmp/y"},
        ])

    def test_jsonl_serialization_is_compact_and_key_sorted(self):
        records = [{"record_type": "dir", "path": "a"}, {"record_type": "file", "path": "z.txt", "size": 1}]
        body = M._dump_jsonl(records)
        lines = body.splitlines()
        self.assertEqual(lines[0], '{"path":"a","record_type":"dir"}')
        self.assertEqual(lines[1], '{"path":"z.txt","record_type":"file","size":1}')

    def test_record_sort_key_orders_by_type_then_path(self):
        records = [
            {"record_type": "file", "path": "z.txt", "size": 1},
            {"record_type": "dir", "path": "a"},
            {"record_type": "symlink", "path": "m"},
        ]
        records.sort(key=lambda r: (M.RECORD_RANK[r["record_type"]], os.fsencode(r["path"])))
        self.assertEqual([r["record_type"] for r in records], ["dir", "file", "symlink"])


    # F5: previously-stuck constants/misclassifications, real fixtures.
    def test_git_detects_owning_ancestor_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / ".git").mkdir()
            root = base / "sub" / "physical-root"
            root.mkdir(parents=True)
            _, summary = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture",
            )
            self.assertTrue(summary["git"])

    def test_directory_form_lock_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / ".pipeline-lock").mkdir(parents=True)
            _, summary = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture",
            )
            self.assertTrue(summary["lock_present"])

    def test_open_route_present_covers_canonical_and_legacy_routes_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / ".runtime" / "routes").mkdir(parents=True)
            (root / ".runtime" / "routes" / "canonical.json").write_text("{}", encoding="utf-8")
            _, summary = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture",
            )
            self.assertTrue(summary["open_route_present"])
            with tempfile.TemporaryDirectory() as tmp2:
                root2 = Path(tmp2) / "root"
                (root2 / "routes").mkdir(parents=True)
                (root2 / "routes" / "legacy.json").write_text("{}", encoding="utf-8")
                _, summary2 = M.scan_root(
                    root2, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                    mirror_map={}, root_key="fixture",
                )
                self.assertTrue(summary2["open_route_present"])

    def test_nested_artifact_root_detects_legacy_claude_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "nested" / ".claude_reports").mkdir(parents=True)
            _, summary = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture",
            )
            self.assertTrue(summary["nested_artifact_root"])

    def test_live_job_reads_bounded_path_metadata_from_jobs_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            agent_home = base / "agent-home"
            (agent_home / ".dispatch").mkdir(parents=True)
            root = base / "physical-root"
            root.mkdir()
            (agent_home / ".dispatch" / "jobs.log").write_text(
                f"2026-08-05T00:00:00Z\topen\t/x\t{root}\tslug\tmeta\n", encoding="utf-8",
            )
            _, summary = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture", agent_home=agent_home,
            )
            self.assertEqual(summary["live_job"], "live")
            # a closed job for the same path is not live.
            (agent_home / ".dispatch" / "jobs.log").write_text(
                f"2026-08-05T00:00:00Z\tdone\t/x\t{root}\tslug\tmeta\n", encoding="utf-8",
            )
            _, summary2 = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture", agent_home=agent_home,
            )
            self.assertEqual(summary2["live_job"], "none")
            # missing registry -> unknown, never a false negative read as "none".
            _, summary3 = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture", agent_home=None,
            )
            self.assertEqual(summary3["live_job"], "unknown")

    def test_bisync_impact_reflects_mirror_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            _, mirrored = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={"fixture": "/nas/target"}, root_key="fixture",
            )
            self.assertEqual(mirrored["bisync_impact"], "mirrored")
            _, not_mirrored = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={"other": "/nas/x"}, root_key="fixture",
            )
            self.assertEqual(not_mirrored["bisync_impact"], "not-mirrored")
            _, unknown = M.scan_root(
                root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture",
            )
            self.assertEqual(unknown["bisync_impact"], "unknown")

    def test_hash_read_failure_marks_root_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"; root.mkdir()
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                self.skipTest("root bypasses file permission checks")
            blocked = root / "blocked.txt"
            blocked.write_text("secret", encoding="utf-8")
            os.chmod(blocked, 0o000)
            try:
                records, summary = M.scan_root(
                    root, dest=None, do_hash=True, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                    mirror_map={}, root_key="fixture",
                )
            finally:
                os.chmod(blocked, 0o644)
            self.assertFalse(summary["complete"])
            blocked_row = next(r for r in records if r["path"] == "blocked.txt")
            self.assertTrue(blocked_row.get("hash_error"))

    def test_destination_as_existing_file_is_a_typed_collision_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"; root.mkdir()
            dest = base / "dest-is-a-file"; dest.write_text("x", encoding="utf-8")
            _, summary = M.scan_root(
                root, dest=dest, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                mirror_map={}, root_key="fixture",
            )
            self.assertEqual(summary["destination_conflict_state"], "file")
            self.assertTrue(summary["destination_conflict"])

    # F6: sweep-key path traversal / duplicate rejection, dest inside --out,
    # and a determinism check that actually covers the summary record.
    def test_sweep_rejects_a_key_that_escapes_out_via_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            roots_yaml = base / "roots.yaml"
            roots_yaml.write_text(
                f"roots:\n  - key: ../escaped\n    path: {root}\n", encoding="utf-8",
            )
            out_dir = base / "sweep-out"
            result = subprocess.run(
                [sys.executable, str(TOOL), "sweep", "--roots", str(roots_yaml), "--out", str(out_dir)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 64, result.stdout)
            self.assertFalse((base / "escaped.jsonl").exists())

    def test_sweep_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            roots_yaml = base / "roots.yaml"
            roots_yaml.write_text(
                f"roots:\n  - key: dup\n    path: {root}\n  - key: dup\n    path: {root}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(TOOL), "sweep", "--roots", str(roots_yaml), "--out", str(base / "out")],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 64, result.stdout)

    def test_sweep_rejects_an_out_dir_nested_inside_a_declared_dest(self):
        # F6: sweep only fed `root` paths to the I1 containment check, not
        # `dest` -- a `dest` naming a large tree that `--out` happens to sit
        # inside used to pass clean.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            dest = base / "dest-tree"; dest.mkdir()
            out_dir = dest / "sweep-out"
            roots_yaml = base / "roots.yaml"
            roots_yaml.write_text(
                f"roots:\n  - key: fixture-root\n    path: {root}\n    dest: {dest}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(TOOL), "sweep", "--roots", str(roots_yaml), "--out", str(out_dir)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 64, result.stdout)

    def test_determinism_check_catches_a_summary_only_mutation(self):
        # F6: the old two-pass compare only looked at `records_a`/`records_b`,
        # so a second-pass-only change to `root_summary` (the thing that is
        # actually written into the JSONL) went undetected.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _make_fixture(base)
            calls = {"n": 0}
            original_root_summary = M._root_summary

            def flaky_summary(*args, **kwargs):
                calls["n"] += 1
                summary = original_root_summary(*args, **kwargs)
                if calls["n"] == 2:
                    summary = dict(summary); summary["file_count"] += 1
                return summary

            M._root_summary = flaky_summary
            try:
                _, _, determinism_ok = M._scan_twice(
                    root, dest=None, do_hash=False, ref_scan_max_bytes=M.DEFAULT_REF_SCAN_MAX_BYTES,
                    mirror_map={}, root_key="fixture",
                )
            finally:
                M._root_summary = original_root_summary
            self.assertFalse(determinism_ok)


if __name__ == "__main__":
    unittest.main()
