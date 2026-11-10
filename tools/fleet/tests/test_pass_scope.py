"""One collection pass shares its fresh observations; settled files are reused by generation."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import pass_scope


class PassScopeTest(unittest.TestCase):
    def test_one_answer_per_pass_and_fresh_outside(self):
        calls = []
        produce = lambda: calls.append(1) or len(calls)
        self.assertEqual(pass_scope.cached("k", produce), 1)
        self.assertEqual(pass_scope.cached("k", produce), 2)
        with pass_scope.collection_pass():
            self.assertEqual(pass_scope.cached("k", produce), 3)
            with pass_scope.collection_pass():
                self.assertEqual(pass_scope.cached("k", produce), 3)
            self.assertEqual(pass_scope.cached("k", produce), 3)
        with pass_scope.collection_pass():
            self.assertEqual(pass_scope.cached("k", produce), 4)

    def test_realpath_is_memoized_only_inside_a_pass(self):
        pass_scope.install()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a"
            target.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(target)
            with pass_scope.collection_pass():
                first = os.path.realpath(str(link))
                link.unlink()
                link.symlink_to(Path(tmp))
                self.assertEqual(os.path.realpath(str(link)), first)      # one pass, one answer
                self.assertEqual(Path(link).resolve(), Path(first))
            self.assertEqual(os.path.realpath(str(link)), os.path.realpath(tmp))  # next read is fresh

    def test_socket_owner_walk_is_shared_within_a_pass(self):
        from fleet.collectors import codex
        with mock.patch.object(codex, "_socket_owners_by_inode_walk", return_value={"1": {2}}) as walk:
            with pass_scope.collection_pass():
                codex._socket_owners_by_inode()
                codex._socket_owners_by_inode()
            codex._socket_owners_by_inode()
        self.assertEqual(walk.call_count, 2)


class GenerationReuseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_registry_source_is_reread_only_after_its_generation_changes(self):
        import resource_run_registry as registry
        path = self.root / "registry.json"
        path.write_text(json.dumps({"runs": {}}))
        first = registry.read_registry_source(path)
        with mock.patch.object(registry.os, "open", wraps=registry.os.open) as opened:
            second = registry.read_registry_source(path)
        self.assertEqual(opened.call_count, 0)
        self.assertEqual(first, second)
        time.sleep(0.01)
        path.write_text(json.dumps({"runs": {"r": {}}}))
        third_source, third = registry.read_registry_source(path)
        self.assertEqual(json.loads(third), {"runs": {"r": {}}})
        self.assertNotEqual(third_source["sha256"], first[0]["sha256"])

    def test_route_header_follows_the_route_file_generation(self):
        from fleet import route
        path = self.root / "rt.json"
        path.write_text(json.dumps({"route_id": "rt-a", "artifact_root": "/x"}))
        self.assertEqual(route._route_header(path), ("rt-a", "/x", "rt-a"))
        time.sleep(0.01)
        path.write_text(json.dumps({"route_id": "rt-b"}))
        self.assertEqual(route._route_header(path), ("rt-b", "", "rt-b"))

    def test_journal_day_reuse_matches_a_fresh_read(self):
        import artifact_journal as journal
        root = self.root / ".agent_reports"
        campaign = root / "campaigns/2026-11-06_example"
        campaign.mkdir(parents=True)
        binding = {"path": "campaigns/2026-11-06_example/journals/2026-11-06.md",
                   "campaign_id": "camp_" + "1" * 32, "campaign_key": "example", "route_id": "rt-abcd",
                   "route_hash": "hash", "at": "2026-11-06T23:59:58+09:00"}
        (campaign / "campaign.json").write_text(json.dumps({"campaign_id": binding["campaign_id"]}))
        journal.append(root, binding, origin={"turn": "a"}, summary="Work a",
                       body="Observed check exit 1; task verdict unknown\n")
        day = root / binding["path"]
        fresh = journal.read_day(day, limit=journal.READ_ROWS, newest=True)
        self.assertEqual(fresh["coverage"], "complete")
        self.assertEqual(journal._read_day_generation(day), fresh)
        with mock.patch.object(journal, "read_day") as read:
            journal._read_day_generation(day)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
