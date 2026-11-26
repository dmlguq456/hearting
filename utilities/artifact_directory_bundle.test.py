import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import artifact_directory_bundle as B


class DirectoryBundleTests(unittest.TestCase):
    def test_descriptor_closed_canonical_vector(self):
        body = {"schema": B.SCHEMA, "directory": {"kind": "cycle-relative", "path": "artifacts/raw-results"},
                "file_count": 1, "total_bytes": 0, "tree_digest": "sha256:" + "0" * 64,
                "digest_algorithm": B.ALGORITHM}
        data = B.canonical_descriptor_bytes(body)
        self.assertEqual(data, json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(B.parse_descriptor(data), body)
        for bad in (data[:-1] + b",\"extra\":1}", data[:-2] + b"}}",
                    data.replace(b'"file_count":1', b'"file_count":true')):
            with self.assertRaises(B.DirectoryBundleError):
                B.parse_descriptor(bad)
        with self.assertRaises(B.DirectoryBundleError):
            B.parse_descriptor(b'{"schema":"x","schema":"y"}')

    def test_descriptor_independent_wire_vector_and_closed_negative_cases(self):
        body = {"schema": B.SCHEMA, "directory": {"kind": "cycle-relative", "path": "artifacts/raw-results"},
                "file_count": 1, "total_bytes": 0, "tree_digest": "sha256:" + "0" * 64,
                "digest_algorithm": B.ALGORITHM}
        expected = (b'{"digest_algorithm":"sha256-canonical-leaf-list/v1","directory":{"kind":"cycle-relative",'
                    b'"path":"artifacts/raw-results"},"file_count":1,"schema":"hearting-directory-bundle/v1",'
                    b'"total_bytes":0,"tree_digest":"sha256:' + b'0' * 64 + b'"}')
        self.assertEqual(B.canonical_descriptor_bytes(body), expected)
        invalid = (
            expected.replace(b'"file_count":1', b'"file_count":1.0'),
            expected.replace(b'"file_count":1', b'"file_count":NaN'),
            expected.replace(b'"file_count":1', b'"file_count":true'),
            expected[:-1] + b',"unknown":0}',
            expected.replace(b'raw-results', b'outside'),
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(B.DirectoryBundleError):
                B.parse_descriptor(raw)

    def test_selector_outermost_hidden_empty_and_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "artifacts"
            (a / ".hidden" / "raw-results" / "nested" / "raw-results").mkdir(parents=True)
            (a / "raw-results").mkdir()
            (a / "raw-results-file").write_text("ordinary")
            got = B.select_outermost(a)
            self.assertEqual(got, ("artifacts/.hidden/raw-results", "artifacts/raw-results"))
            (a / "raw-results" / "x.txt").write_bytes(b"abc")
            tree = B.scan_tree(Path(tmp), "artifacts/raw-results")
            self.assertEqual(tree.body["file_count"], 1)
            self.assertEqual(tree.leaves[0].digest, "sha256:" + hashlib.sha256(b"abc").hexdigest())

    def test_selector_prunes_only_when_candidate_policy_is_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            excluded = artifacts / "cache"
            excluded.mkdir(parents=True)
            (excluded / "raw-results").symlink_to("missing-target", target_is_directory=True)
            sibling = artifacts / "run" / "raw-results"
            sibling.mkdir(parents=True)
            (sibling / "result.txt").write_text("kept")
            seen = []

            def prune(path):
                seen.append(path)
                return path == excluded

            self.assertEqual(B.select_outermost(artifacts, prune_directory=prune),
                             ("artifacts/run/raw-results",))
            self.assertNotIn(excluded / "raw-results", seen)
            # The descriptor verifier's default selection retains its historical behavior.
            with self.assertRaises(B.DirectoryBundleError):
                B.select_outermost(artifacts)
            self.assertEqual(B.select_outermost(artifacts, prune_directory=lambda _path: True), ())

    def test_candidate_bundle_is_not_selected_when_a_descendant_is_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            raw = artifacts / "raw-results"
            (raw / "cache").mkdir(parents=True)
            (raw / "cache" / "CACHEDIR.TAG").write_bytes(b"marker")
            (raw / "cache" / "raw-results").symlink_to("missing", target_is_directory=True)
            (raw / "result.csv").write_text("kept")
            cache = raw / "cache"
            self.assertEqual(B.select_outermost(artifacts,
                                                prune_directory=lambda path: path == cache), ())
            # Historical verification without a candidate callback is unchanged.
            self.assertEqual(B.select_outermost(artifacts), ("artifacts/raw-results",))

    def test_candidate_symlinks_fall_back_without_changing_historical_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = root / "artifacts"
            raw = artifacts / "raw-results"
            raw.mkdir(parents=True)
            (raw / "result.csv").write_bytes(b"kept\n")
            external = root / "external"
            external.mkdir()
            (raw / "cache").symlink_to(external, target_is_directory=True)
            (raw / "missing").symlink_to(root / "absent")
            self.assertEqual(B.select_outermost(artifacts, prune_directory=lambda _path: False), ())
            self.assertEqual(B.select_outermost(artifacts), ("artifacts/raw-results",))
            with self.assertRaises(B.DirectoryBundleError) as caught:
                B.scan_tree(root, "artifacts/raw-results")
            self.assertEqual(caught.exception.code, "tree-symlink-forbidden")

            (artifacts / "nested").mkdir()
            (artifacts / "nested" / "raw-results").symlink_to(external, target_is_directory=True)
            self.assertEqual(B.select_outermost(artifacts, prune_directory=lambda _path: False), ())
            with self.assertRaises(B.DirectoryBundleError) as caught:
                B.select_outermost(artifacts)
            self.assertEqual(caught.exception.code, "raw-results-symlink")

    def test_published_descriptor_still_rejects_a_later_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "artifacts" / "raw-results"
            raw.mkdir(parents=True)
            (raw / "result.csv").write_bytes(b"kept\n")
            tree = B.scan_tree(root, "artifacts/raw-results")
            descriptor = root / "artifacts" / "bundle.json"
            descriptor.write_bytes(tree.descriptor)
            row = {"media_type": B.MEDIA_TYPE, "byte_size": len(tree.descriptor),
                   "content_digest": tree.descriptor_digest}
            self.assertEqual(B.verify_coverage(root, "artifacts/bundle.json", row),
                             ("artifacts/raw-results/result.csv",))
            (raw / "cache").symlink_to(root / "absent")
            with self.assertRaises(B.DirectoryBundleError) as caught:
                B.verify_coverage(root, "artifacts/bundle.json", row)
            self.assertEqual(caught.exception.code, "tree-symlink-forbidden")
            self.assertEqual(descriptor.read_bytes(), tree.descriptor)

    def test_tree_digest_unicode_order_same_size_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "artifacts" / "raw-results"
            base.mkdir(parents=True)
            (base / "é").write_bytes(b"a")
            (base / "z").write_bytes(b"b")
            one = B.scan_tree(Path(tmp), "artifacts/raw-results")
            (base / "z").write_bytes(b"c")
            two = B.scan_tree(Path(tmp), "artifacts/raw-results")
            self.assertNotEqual(one.body["tree_digest"], two.body["tree_digest"])
            self.assertEqual([x.path for x in one.leaves], ["z", "é"])

    def test_tree_race_refuses_unsealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "artifacts" / "raw-results"
            base.mkdir(parents=True)
            (base / "empty").write_bytes(b"")
            with self.assertRaises(B.DirectoryBundleError):
                B.scan_tree(Path(tmp), "artifacts/raw-results/../raw-results")

    def test_scan_and_copy_reject_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            (real / "artifacts" / "raw-results").mkdir(parents=True)
            (real / "artifacts" / "raw-results" / "x").write_bytes(b"x")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(B.DirectoryBundleError):
                B.scan_tree(alias, "artifacts/raw-results")
            with self.assertRaises(B.DirectoryBundleError):
                B.copy_file_streaming(alias / "artifacts" / "raw-results" / "x", root / "copy")

    def test_tree_rejects_fifo_and_detects_same_size_read_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "artifacts" / "raw-results"
            raw.mkdir(parents=True)
            fifo = raw / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(B.DirectoryBundleError) as caught:
                B.scan_tree(root, "artifacts/raw-results")
            self.assertEqual(caught.exception.code, "tree-non-regular")
            fifo.unlink()
            target = raw / "mutable"
            target.write_bytes(b"first")
            original = B._hash_fd
            def mutate(fd, rel):
                facts = original(fd, rel)
                target.write_bytes(b"other")
                return facts
            B._hash_fd = mutate
            try:
                with self.assertRaises(B.DirectoryBundleError) as caught:
                    B.scan_tree(root, "artifacts/raw-results")
                self.assertIn(caught.exception.code, {"output-changed-while-reading"})
            finally:
                B._hash_fd = original

    def test_streaming_hash_and_copy_memory_bound(self):
        self.assertEqual(B.CHUNK_BYTES, 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source"
            dst = Path(tmp) / "destination"
            src.write_bytes(b"streamed payload")
            digest, size = B.copy_file_streaming(src, dst, expected_size=16)
            self.assertEqual(dst.read_bytes(), b"streamed payload")
            self.assertEqual(size, 16)
            self.assertEqual(digest, "sha256:" + hashlib.sha256(b"streamed payload").hexdigest())

    def test_selector_rejects_raw_symlink_and_tree_rejects_special(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            artifacts.mkdir()
            target = artifacts / "elsewhere"
            target.mkdir()
            (artifacts / "raw-results").symlink_to(target, target_is_directory=True)
            with self.assertRaises(B.DirectoryBundleError):
                B.select_outermost(artifacts)

    def test_bundle_cardinality_1_100_10000(self):
        # Descriptor has a constant artifact/revision/event cardinality independent of leaves.
        results = []
        for count in (1, 100, 10000):
            leaves = [[f"f{i:05d}", "sha256:" + "0" * 64, 1] for i in range(count)]
            body = {"schema": B.SCHEMA, "directory": {"kind": "cycle-relative", "path": "artifacts/raw-results"},
                    "file_count": count, "total_bytes": count,
                    "tree_digest": "sha256:" + hashlib.sha256(B._canonical(leaves)).hexdigest(),
                    "digest_algorithm": B.ALGORITHM}
            results.append((1, 1, 1, len(B.canonical_descriptor_bytes(body))))
        self.assertEqual([x[:3] for x in results], [(1, 1, 1)] * 3)


if __name__ == "__main__":
    unittest.main()
