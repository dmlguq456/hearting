#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import unittest

import route_identity as MODULE


class RouteIdentityTest(unittest.TestCase):
    def test_excluded_keys_are_a_single_shared_set(self):
        self.assertEqual(
            {"route_hash", "route_id", "owner_attempt_id", "route_family_key",
             "route_file", "_route_file"},
            set(MODULE.ROUTE_HASH_EXCLUDED_KEYS),
        )

    def test_hash_unaffected_by_post_hash_lineage_fields(self):
        payload = {"schema_version": 2, "nodes": [{"id": "n"}]}
        before = MODULE.route_hash(payload)
        payload["owner_attempt_id"] = "att-example"
        payload["route_family_key"] = "sha256:" + "a" * 64
        payload["route_file"] = "/invocation/route.json"
        payload["_route_file"] = "/other/invocation/route.json"
        after = MODULE.route_hash(payload)
        self.assertEqual(before, after)

    def test_route_id_from_hash_derives_prefix(self):
        digest = "sha256:" + "b" * 64
        self.assertEqual("rt-" + "b" * 16, MODULE.route_id_from_hash(digest))

    def test_route_id_from_hash_refuses_non_sha256_prefix(self):
        with self.assertRaises(ValueError):
            MODULE.route_id_from_hash("md5:" + "c" * 32)


class RegisteredNodeIdentityTest(unittest.TestCase):
    def setUp(self):
        self.node = {"id": "terminal", "kind": "capability-owner", "unit": "_kernel/owner",
                     "dispatch_depth": 1, "terminal": True}
        self.row = {"dispatch_depth": "1", "worker_type": "owner", "unit": "_kernel/owner",
                    "owner_route_id": "rt-owner", "owner_route_hash": "sha256:owner"}

    def test_owner_and_identical_legacy_alias_resolve_without_mutation(self):
        for aliases in ({}, {"route_id": "rt-owner", "route_hash": "sha256:owner",
                            "route_node": "terminal"}):
            row = dict(self.row, **aliases)
            before = dict(row)
            self.assertEqual(MODULE.registered_node_identity(row, self.node),
                             ("rt-owner", "sha256:owner", "terminal"))
            self.assertEqual(row, before)

    def test_owner_refuses_partial_conflicting_and_nonowner_authority(self):
        for changes in ({"owner_route_hash": ""}, {"route_id": "foreign"},
                        {"route_hash": "foreign"}, {"route_node": "foreign"},
                        {"worker_type": "stage"}, {"unit": "dev/backend"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MODULE.registered_node_identity(dict(self.row, **changes), self.node)
        for changes in ({"dispatch_depth": 2}, {"kind": "review-worker"},
                        {"unit": "dev/backend"}, {"terminal": False}):
            with self.subTest(node=changes), self.assertRaises(ValueError):
                MODULE.registered_node_identity(self.row, dict(self.node, **changes))

    def test_depth_two_must_use_its_own_node_identity(self):
        row = dict(self.row, dispatch_depth="2", worker_type="stage", unit="dev/backend")
        self.assertEqual(MODULE.registered_node_identity(row, self.node), ("", "", ""))
        row.update(route_id="rt-child", route_hash="sha256:child", route_node="execute")
        self.assertEqual(MODULE.registered_node_identity(row, self.node),
                         ("rt-child", "sha256:child", "execute"))


if __name__ == "__main__":
    unittest.main()
