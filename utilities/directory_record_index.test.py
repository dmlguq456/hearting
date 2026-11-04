#!/usr/bin/env python3
"""NAS lookup regressions: real lineage/admission with many unrelated records."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import directory_record_index as I
import artifact_producer as P
from split_module_fixture import patch_split

spec = importlib.util.spec_from_file_location("producer_index_fixtures", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
R = F.R


class DirectoryLookupTest(F.ProducerTestBase):
    _publish_root = F.RouteLineageBindingTest._publish_root
    _continuation = F.RouteLineageBindingTest._continuation
    _begin = F.RouteLineageBindingTest._begin

    def setUp(self):
        super().setUp()
        self.repo = Path(self._tmp.name) / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("fixture\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "-qm", "fixture"], check=True)
        self.activate()
        self.a = self._root_route("indexed-admission")
        self._publish_root(self.a)
        self.cycle = self._begin(self.a)
        self.record = P.read_cycle_record(self.root, self.cycle["cycle_id"])
        self.routes = P._routes_dir(self.root)
        self.cycles = P.producer_dir(self.root) / "cycles"

    def _root_route(self, slug):
        return R.compile_route("autopilot-code", "dev", "direct", self.repo, self.root,
                               predicates=F.ALL, inline_reason="atomic-direct", tracking="tracked",
                               tracked_gate_evidence=F.gate_evidence(), slug=slug)

    def noise(self, count=2000):
        for number in range(count):
            (self.routes / f"rt-{number:016x}.json").write_text(json.dumps({"route_id": f"noise-{number}", "nodes": []}))
            (self.cycles / f"cyc_{number:032x}.json").write_text(json.dumps({"cycle_id": f"cyc_{number:032x}",
                                                                         "route_id": f"noise-{number}", "state": "open"}))

    def children(self, route=None):
        route = route or self.a
        return P._lineage_children(self.root, route["route_id"], route["route_hash"])

    def test_finish_admission_and_fleet_cycle_lookup_read_only_related_records(self):
        self.noise()
        b = self._continuation(self.a)
        expected = P.cycle_route_admission(self.root, self.record, b, finalize=True)
        self.assertTrue(expected.allow, expected)
        self.assertEqual(P.route_cycle_for(self.root, b)["cycle_id"], self.cycle["cycle_id"])
        # Drop the old process-only cache. The disk index alone must suffice.
        P._ROUTE_EDGES.clear()
        reads, original = [], P._read_json
        def read(path):
            if Path(path).parent in (self.routes, self.cycles) and not Path(path).name.startswith("."):
                reads.append(Path(path).name)
            return original(path)
        with patch_split(P, "_read_json", side_effect=read):
            self.assertEqual(P.cycle_route_admission(self.root, self.record, b, finalize=True), expected)
            self.assertEqual(P.route_cycle_for(self.root, b)["cycle_id"], self.cycle["cycle_id"])
        self.assertLessEqual(len(reads), 4, reads)
        self.assertFalse(any(name.startswith("cyc_0000") or name == "rt-0000000000000000.json" for name in reads))

    def test_missing_stale_corrupt_and_foreign_indexes_match_the_scan(self):
        b = self._continuation(self.a)
        expected = self.children()
        cache = I._path(self.routes, "route-children")
        for damage in ("missing", "invalid-json", "checksum", "escape"):
            with self.subTest(damage=damage):
                if damage == "missing":
                    cache.unlink()
                elif damage == "invalid-json":
                    cache.write_text("{")
                else:
                    data = json.loads(cache.read_text())
                    data["groups"] = {I.route_key(self.a["route_id"], self.a["route_hash"]): ["../outside.json"]}
                    if damage == "escape":
                        data["digest"] = I._digest({key: value for key, value in data.items() if key != "digest"})
                    cache.write_text(json.dumps(data))
                self.assertEqual(self.children(), expected)
        # An out-of-band ordinary publication misses the writer update: rebuild.
        c = R.build_continuation_route(b, resume_from_node="inline", requested_boundary="inline", reason="out-of-band", artifact_root=self.root)
        (self.routes / f"{c['route_id']}.json").write_text(json.dumps(c))
        self.assertEqual([row["route_id"] for row in self.children(b)], [c["route_id"]])

    def test_write_once_updates_index_without_reading_unrelated_routes(self):
        self.noise(1000)
        self.children()
        c = R.build_continuation_route(self.a, resume_from_node="inline", requested_boundary="inline", reason="indexed-publish", artifact_root=self.root)
        reads, original = [], P._read_json
        def read(path):
            if Path(path).parent == self.routes:
                reads.append(Path(path).name)
            return original(path)
        with patch_split(P, "_read_json", side_effect=read):
            R.write_once(self.routes / f"{c['route_id']}.json", c)
            self.assertEqual([row["route_id"] for row in self.children()], [c["route_id"]])
        self.assertEqual(reads, [f"{c['route_id']}.json", f"{c['route_id']}.outcome.json"])

    def test_selected_tampered_edge_is_never_admission_authority(self):
        b = self._continuation(self.a)
        self.assertEqual(len(self.children()), 1)
        path = self.routes / f"{b['route_id']}.json"
        b["slug"] = "tampered-in-place"
        path.write_text(json.dumps(b))
        self.assertEqual(self.children(), [])
        self.assertTrue(P.cycle_route_admission(self.root, self.record, self.a, finalize=True).allow)

    def test_publication_during_a_cached_read_rebuilds_from_the_new_listing(self):
        b = self._continuation(self.a)
        self.children()
        c = R.build_continuation_route(self.a, resume_from_node="inline", requested_boundary="inline",
                                       reason="racing-publish", artifact_root=self.root)
        original, published = P._read_json, False
        def read(path):
            nonlocal published
            value = original(path)
            if Path(path).name == f"{b['route_id']}.json" and not published:
                published = True
                R.write_once(self.routes / f"{c['route_id']}.json", c)
            return value
        with patch_split(P, "_read_json", side_effect=read):
            actual = self.children()
        self.assertEqual({row["route_id"] for row in actual}, {b["route_id"], c["route_id"]})

    def test_cycle_state_is_fresh_and_atomic_replacement_updates_its_index(self):
        self.noise(1000)
        expected = P.list_cycle_records(self.root, route_ids={self.a["route_id"]})
        self.assertEqual(len(expected), 1)
        replacement = dict(self.record, state="sealed")
        P._write_cycle_record(self.root, replacement, exclusive=False)
        reads, original = [], P._read_json
        def read(path):
            if Path(path).parent == self.cycles:
                reads.append(Path(path).name)
            return original(path)
        with patch_split(P, "_read_json", side_effect=read):
            actual = P.list_cycle_records(self.root, route_ids={self.a["route_id"]})
        self.assertEqual(actual[0]["state"], "sealed")
        self.assertEqual(reads, [f"{self.cycle['cycle_id']}.json"])
        # Even an in-place state edit is read fresh; key identity is unchanged.
        replacement["state"] = "open"
        P.cycle_record_path(self.root, self.cycle["cycle_id"]).write_text(json.dumps(replacement))
        self.assertEqual(P.list_cycle_records(self.root, route_ids={self.a["route_id"]})[0]["state"], "open")

    def test_cache_write_failure_and_symlink_leave_lookup_and_publication_working(self):
        b = self._continuation(self.a)
        with mock.patch.object(I.os, "replace", side_effect=OSError("read-only cache")):
            self.assertEqual([row["route_id"] for row in self.children()], [b["route_id"]])
        cache = I._path(self.routes, "route-children")
        if cache.exists():
            cache.unlink()
        outside = Path(self._tmp.name) / "foreign.txt"
        outside.write_text("preserve\n")
        cache.symlink_to(outside)
        self.assertEqual([row["route_id"] for row in self.children()], [b["route_id"]])
        self.assertTrue(cache.is_symlink())
        self.assertEqual(outside.read_text(), "preserve\n")

    def test_open_only_status_does_not_read_closed_route_payloads(self):
        for number in range(1000):
            route_id = f"rt-{number:016x}"
            (self.routes / f"{route_id}.json").write_text(json.dumps({"route_id": route_id, "nodes": []}))
            (self.routes / f"{route_id}.outcome.json").write_text("{}")
        expected = [row for row in R.route_status(self.root) if not row["closed"]]
        reads, original = [], Path.read_text
        def read(path, *args, **kwargs):
            if path.parent == self.routes:
                reads.append(path.name)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read):
            self.assertEqual(R.route_status(self.root, open_only=True), expected)
        self.assertEqual(reads, [f"{self.a['route_id']}.json"])

    @unittest.skipUnless(os.name == "posix", "POSIX filenames may contain non-UTF-8 bytes")
    def test_foreign_non_utf8_filename_preserves_scan_and_cached_lookup(self):
        b = self._continuation(self.a)
        foreign = os.fsdecode(b"foreign-\xff.json")
        (self.routes / foreign).write_text("{}")
        expected = [b["route_id"]]
        self.assertEqual([row["route_id"] for row in self.children()], expected)
        cache = I._load(I._path(self.routes, "route-children"))
        self.assertIn(foreign, cache["listing"])
        P._ROUTE_EDGES.clear()
        self.assertEqual([row["route_id"] for row in self.children()], expected)

    def test_damaged_admission_route_projection_retains_removed_record_recovery(self):
        cycle_id, route_id = self.cycle["cycle_id"], self.a["route_id"]
        P.cycle_record_path(self.root, cycle_id).unlink()
        index_path = P.artifact_admission._index_path(self.root)
        damages = ([1], {"root": [1]}, {"root": {route_id: []}},
                   {"root": {route_id: {"cycle_id": []}}})
        for projection in damages:
            with self.subTest(projection=projection):
                index_path.write_text(json.dumps(
                    {"cycles": {cycle_id: {}}, "routes": projection}))
                with patch_split(P, "read_cycle_record", return_value=self.record) as recover:
                    self.assertEqual(P.list_cycle_records(self.root, route_ids={route_id}), [self.record])
                    recover.assert_called_once_with(self.root, cycle_id)

    def test_foreign_dot_json_cycle_filename_matches_original_scan_scope(self):
        (self.cycles / ".json").write_text(json.dumps({"cycle_id": "foreign", "route_id": self.a["route_id"]}))
        expected = P.list_cycle_records(self.root)
        self.assertEqual(P.list_cycle_records(self.root, route_ids={self.a["route_id"]}), expected)


if __name__ == "__main__":
    unittest.main()
