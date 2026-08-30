#!/usr/bin/env python3

import unittest

import route_identity as MODULE


class RouteIdentityTest(unittest.TestCase):
    def test_excluded_keys_are_a_single_shared_set(self):
        self.assertEqual(
            {"route_hash", "route_id", "owner_attempt_id", "route_family_key"},
            set(MODULE.ROUTE_HASH_EXCLUDED_KEYS),
        )

    def test_hash_unaffected_by_post_hash_lineage_fields(self):
        payload = {"schema_version": 2, "nodes": [{"id": "n"}]}
        before = MODULE.route_hash(payload)
        payload["owner_attempt_id"] = "att-example"
        payload["route_family_key"] = "sha256:" + "a" * 64
        after = MODULE.route_hash(payload)
        self.assertEqual(before, after)

    def test_route_id_from_hash_derives_prefix(self):
        digest = "sha256:" + "b" * 64
        self.assertEqual("rt-" + "b" * 16, MODULE.route_id_from_hash(digest))

    def test_route_id_from_hash_refuses_non_sha256_prefix(self):
        with self.assertRaises(ValueError):
            MODULE.route_id_from_hash("md5:" + "c" * 32)


if __name__ == "__main__":
    unittest.main()
