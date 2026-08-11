from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_identity as m


class TestAllocator(unittest.TestCase):
    def test_allocated_id_has_typed_prefix_and_128_bit_body(self):
        alloc = m.IdAllocator()
        value = alloc.allocate("cycle")
        self.assertTrue(value.startswith("cyc_"))
        body = value[len("cyc_") :]
        self.assertEqual(len(body), 32)
        int(body, 16)  # must be valid hex
        self.assertEqual(len(bytes.fromhex(body)), 16)

    def test_prefixes_are_pairwise_unambiguous(self):
        prefixes = list(m.ID_KINDS.values())
        for a in prefixes:
            for b in prefixes:
                if a == b:
                    continue
                self.assertFalse(b.startswith(a), "{0!r} is a prefix of {1!r}".format(a, b))

    def test_kind_of_round_trips_all_kinds(self):
        alloc = m.IdAllocator()
        for kind in m.ID_KINDS:
            value = alloc.allocate(kind)
            self.assertEqual(m.kind_of(value), kind)
            self.assertTrue(m.is_well_formed(value, kind))
            self.assertTrue(m.is_well_formed(value))

    def test_rejects_unknown_kind(self):
        alloc = m.IdAllocator()
        with self.assertRaises(m.IdentityError):
            alloc.allocate("not-a-real-kind")

    def test_allocator_uses_injected_entropy(self):
        fe = m.FixedEntropy(b"\xab\xcd")
        alloc = m.IdAllocator(entropy=fe)
        value = alloc.allocate("event")
        self.assertEqual(value, "evt_" + ("abcd" * 8))

    def test_same_entropy_yields_same_id_regardless_of_cwd_and_clock(self):
        alloc1 = m.IdAllocator(entropy=m.FixedEntropy(b"\x01\x02\x03"))
        alloc2 = m.IdAllocator(entropy=m.FixedEntropy(b"\x01\x02\x03"))
        self.assertEqual(alloc1.allocate("artifact"), alloc2.allocate("artifact"))

    def test_migration_namespace_seat_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            m.migration_namespace("ns", "key")

    def test_root_identity_round_trip_and_rejects_extra_key(self):
        payload = {
            "schema_version": 1,
            "artifact_root_id": "root_" + "0" * 32,
            "repository_id": "repo_" + "1" * 32,
            "issued_at": "2026-08-11T00:00:00Z",
            "producer_contract_version": "artifact-cycle-manifest/v2",
        }
        identity = m.RootIdentity.parse(payload)
        self.assertEqual(identity.to_payload(), payload)

        bad = dict(payload)
        bad["extra_key"] = "nope"
        with self.assertRaises(m.IdentityError):
            m.RootIdentity.parse(bad)

        missing = dict(payload)
        del missing["issued_at"]
        with self.assertRaises(m.IdentityError):
            m.RootIdentity.parse(missing)

    def test_allocate_many_returns_distinct_well_formed_ids(self):
        alloc = m.IdAllocator()
        values = alloc.allocate_many("artifact", 5)
        self.assertEqual(len(values), 5)
        self.assertEqual(len(set(values)), 5)
        for v in values:
            self.assertTrue(m.is_well_formed(v, "artifact"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
