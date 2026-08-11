from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_identity as idm
import artifact_index as ix
import artifact_manifest as m


def _sha(n):
    return "sha256:" + (str(n) * 64)[:64]


def _document(root_id, alloc=None):
    alloc = alloc or idm.IdAllocator()
    camp_id = alloc.allocate("campaign")
    cyc_id = alloc.allocate("cycle")
    art_id = alloc.allocate("artifact")
    arev_id = alloc.allocate("artifact_revision")
    man_id = alloc.allocate("manifest")
    mrev_id = alloc.allocate("manifest_revision")
    prod_id = alloc.allocate("producer")
    evt_id = alloc.allocate("event")
    strm_id = alloc.allocate("stream")
    provenance = {
        "source_manifest_id": man_id,
        "source_revision_id": mrev_id,
        "producer_route_id": "r",
        "algorithm_version": "v1",
        "schema_version": 1,
        "source_digest": _sha(2),
    }
    doc = {
        "schema_version": 2,
        "manifest_kind": "artifact.cycle",
        "manifest_id": man_id,
        "manifest_revision_id": mrev_id,
        "repository_id": "repo_" + "0" * 32,
        "artifact_root_id": root_id,
        "campaign": {"campaign_id": camp_id, "goal": "g", "completion_criterion": {"statement": "s"}, "title": "t", "state": "active"},
        "cycle": {
            "cycle_id": cyc_id,
            "campaign_id": camp_id,
            "parent_cycle_id": None,
            "started_on": "2026-08-11T00:00:00Z",
            "input_digest": _sha(0),
            "outcome_criterion": {"required_artifact_roles": [], "decision_required": False},
            "state": "active",
        },
        "artifacts": [{"artifact_id": art_id, "cycle_id": cyc_id, "role": "primary", "type": "doc", "capability": "c", "title": "t"}],
        "artifact_revisions": [
            {
                "artifact_revision_id": arev_id,
                "artifact_id": art_id,
                "revision_sequence": 1,
                "content_digest": _sha(1),
                "byte_size": 1,
                "media_type": "text/plain",
                "locator": {"kind": "cycle-relative", "path": "plan.md"},
                "provenance": provenance,
            }
        ],
        "shared_references": [],
        "shared_reference_revisions": [],
        "routes": [{"artifact_root_id": root_id, "route_id": "rt-" + cyc_id, "route_hash": _sha(5), "terminal_marker": "m", "terminal_evidence_id": evt_id}],
        "events": [
            {
                "event_id": evt_id,
                "stream_id": strm_id,
                "stream_sequence": 1,
                "event_type": "artifact.revision.recorded",
                "target_id": art_id,
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:00:00Z",
                "provenance": provenance,
                "evidence_ids": [],
                "payload": {},
            }
        ],
        "producer": {"producer_id": prod_id, "contract_version": "artifact-cycle-manifest/v2", "source_revision": "abc"},
    }
    return doc


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.root_id = "root_" + "1" * 32
        self.alloc = idm.IdAllocator()

    def _apply(self, index, doc, key=None):
        digest = m.manifest_digest(doc)
        key = key or doc["manifest_id"]
        return ix.apply(index, doc, cycle_path="p/" + doc["cycle"]["cycle_id"], manifest_digest=digest, idempotency_key=key), digest, key

    def test_rejects_duplicate_stable_id_across_manifests(self):
        doc1 = _document(self.root_id, self.alloc)
        index, digest1, key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["artifacts"][0]["artifact_id"] = doc1["artifacts"][0]["artifact_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2)
        self.assertFalse(report.ok)
        self.assertIn("index-stable-id-duplicate", {v.code for v in report.violations})

    def test_rejects_reused_revision_id_across_manifests(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _, _ = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["artifact_revisions"][0]["artifact_revision_id"] = doc1["artifact_revisions"][0]["artifact_revision_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2)
        self.assertIn("index-stable-id-duplicate", {v.code for v in report.violations})

    def test_rejects_reused_event_id_across_manifests(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _, _ = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["events"][0]["event_id"] = doc1["events"][0]["event_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2)
        self.assertIn("index-event-id-reused", {v.code for v in report.violations})

    def test_rejects_duplicate_route_composite_across_manifests(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _, _ = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["routes"][0]["route_id"] = doc1["routes"][0]["route_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2)
        self.assertIn("index-route-composite-duplicate", {v.code for v in report.violations})

    def test_rejects_stream_sequence_not_continuing_from_cursor(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _, _ = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["events"][0]["stream_id"] = doc1["events"][0]["stream_id"]
        doc2["events"][0]["stream_sequence"] = 5
        digest2 = m.manifest_digest(doc2)
        report = ix.check(index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2)
        self.assertIn("index-stream-sequence-discontinuous", {v.code for v in report.violations})

    def test_rejects_manifest_revision_append_out_of_scope(self):
        doc1 = _document(self.root_id, self.alloc)
        index, digest1, key1 = self._apply(ix.empty(self.root_id), doc1)
        doc1_mutated = dict(doc1)
        doc1_mutated["manifest_revision_id"] = self.alloc.allocate("manifest_revision")
        digest2 = m.manifest_digest(doc1_mutated)
        report = ix.check(index, doc1_mutated, idempotency_key=key1, manifest_digest=digest2)
        self.assertIn("manifest-revision-append-out-of-scope", {v.code for v in report.violations})

    def test_rejects_cycle_id_duplicate(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _, _ = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["cycle"]["cycle_id"] = doc1["cycle"]["cycle_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2)
        self.assertIn("index-cycle-id-duplicate", {v.code for v in report.violations})

    def test_rejects_root_identity_mismatch(self):
        doc = _document(self.root_id, self.alloc)
        index = ix.empty("root_" + "9" * 32)
        digest = m.manifest_digest(doc)
        report = ix.check(index, doc, idempotency_key=doc["manifest_id"], manifest_digest=digest)
        self.assertIn("index-root-identity-mismatch", {v.code for v in report.violations})

    def test_apply_is_pure_and_returns_new_document(self):
        doc = _document(self.root_id, self.alloc)
        empty = ix.empty(self.root_id)
        applied, _, _ = self._apply(empty, doc)
        self.assertEqual(empty.stable_ids, {})
        self.assertNotEqual(applied.stable_ids, {})

    def test_build_from_manifests_equals_incremental_apply(self):
        docs = [_document(self.root_id, self.alloc) for _ in range(3)]
        incremental = ix.empty(self.root_id)
        items = []
        for doc in docs:
            digest = m.manifest_digest(doc)
            cycle_path = "p/" + doc["cycle"]["cycle_id"]
            incremental = ix.apply(incremental, doc, cycle_path=cycle_path, manifest_digest=digest, idempotency_key=doc["manifest_id"])
            items.append((doc, cycle_path, digest, doc["manifest_id"]))
        built = ix.build(items)
        self.assertEqual(ix.canonical_bytes(incremental), ix.canonical_bytes(built))

    def test_canonical_bytes_stable_under_insertion_order(self):
        docs = [_document(self.root_id, self.alloc) for _ in range(2)]
        items = [(d, "p/" + d["cycle"]["cycle_id"], m.manifest_digest(d), d["manifest_id"]) for d in docs]
        forward = ix.build(items)
        backward = ix.build(list(reversed(items)))
        self.assertEqual(ix.canonical_bytes(forward), ix.canonical_bytes(backward))

    def test_idempotent_match_requires_key_and_digest(self):
        doc = _document(self.root_id, self.alloc)
        index, digest, key = self._apply(ix.empty(self.root_id), doc)
        self.assertTrue(ix.idempotent_match(index, doc, idempotency_key=key, manifest_digest=digest))
        self.assertFalse(ix.idempotent_match(index, doc, idempotency_key=key, manifest_digest=_sha(9)))
        self.assertFalse(ix.idempotent_match(index, doc, idempotency_key="other-key", manifest_digest=digest))

    def test_parse_rejects_unknown_key(self):
        payload = ix.to_payload(ix.empty(self.root_id))
        payload["bogus"] = 1
        with self.assertRaises(ValueError):
            ix.parse(payload)

    # -- F1b: the index is closed at every depth, not only the top level -----

    def test_parse_rejects_forged_nested_manifest_row(self):
        doc = _document(self.root_id, self.alloc)
        index, _digest, _key = self._apply(ix.empty(self.root_id), doc)
        payload = ix.to_payload(index)
        payload["manifests"]["forged-key"] = {
            "manifest_digest": _sha(7),
            "cycle_id": "cyc_forged",
            "extra": True,
        }
        with self.assertRaises(ValueError):
            ix.parse(payload)

    def test_parse_rejects_nested_row_of_wrong_type(self):
        doc = _document(self.root_id, self.alloc)
        index, _digest, key = self._apply(ix.empty(self.root_id), doc)
        payload = ix.to_payload(index)
        payload["manifests"][key] = "not-an-object"
        with self.assertRaises(ValueError):
            ix.parse(payload)

    # -- F2b: one campaign owns many cycles (D-1); shared references recur ---

    def test_check_allows_second_cycle_in_same_campaign(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["campaign"] = dict(doc1["campaign"])
        doc2["cycle"]["campaign_id"] = doc1["campaign"]["campaign_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertTrue(report.ok, [v.to_payload() for v in report.violations])

    def test_check_rejects_same_id_reused_with_different_kind(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        reused = doc1["campaign"]["campaign_id"]
        doc2["artifacts"][0]["artifact_id"] = reused
        doc2["artifact_revisions"][0]["artifact_id"] = reused
        doc2["events"][0]["target_id"] = reused
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertFalse(report.ok)
        self.assertIn(
            "index-stable-id-kind-conflict", {v.code for v in report.violations}
        )

    def test_check_allows_shared_reference_reuse_across_cycles(self):
        ref_id = self.alloc.allocate("shared_reference")
        rrev_id = self.alloc.allocate("shared_reference_revision")

        def add_ref(doc):
            doc["shared_references"] = [
                {"shared_reference_id": ref_id, "kind": "shared-spec", "title": "t"}
            ]
            doc["shared_reference_revisions"] = [
                {
                    "shared_reference_revision_id": rrev_id,
                    "shared_reference_id": ref_id,
                    "revision_sequence": 1,
                    "content_digest": _sha(3),
                    "updated_at": "2026-08-11T00:00:00Z",
                    "provenance": doc["artifact_revisions"][0]["provenance"],
                }
            ]

        doc1 = _document(self.root_id, self.alloc)
        add_ref(doc1)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        add_ref(doc2)
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertTrue(report.ok, [v.to_payload() for v in report.violations])

    # -- F2a: orphan parent cycles are refused (A-1) -------------------------

    def test_check_rejects_orphan_parent_cycle(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["cycle"]["parent_cycle_id"] = self.alloc.allocate("cycle")
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertFalse(report.ok)
        self.assertIn("index-orphan-parent-cycle", {v.code for v in report.violations})

    def test_check_allows_existing_parent_cycle(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["campaign"] = dict(doc1["campaign"])
        doc2["cycle"]["campaign_id"] = doc1["campaign"]["campaign_id"]
        doc2["cycle"]["parent_cycle_id"] = doc1["cycle"]["cycle_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertTrue(report.ok, [v.to_payload() for v in report.violations])

    # -- F3a: repository identity is part of admission tenancy (D-4) ---------

    def test_check_rejects_foreign_repository_id(self):
        doc = _document(self.root_id, self.alloc)
        index = ix.empty(self.root_id)
        digest = m.manifest_digest(doc)
        report = ix.check(
            index,
            doc,
            idempotency_key=doc["manifest_id"],
            manifest_digest=digest,
            repository_id="repo_" + "f" * 32,
        )
        self.assertFalse(report.ok)
        self.assertIn(
            "index-repository-identity-mismatch", {v.code for v in report.violations}
        )

    # -- F3b: manifest revision ids are immutable and single-use -------------

    def test_check_rejects_manifest_revision_id_reuse(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["manifest_revision_id"] = doc1["manifest_revision_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertFalse(report.ok)
        self.assertIn("index-stable-id-duplicate", {v.code for v in report.violations})

    # -- F8ii: rebuild refuses cross-manifest conflicts ----------------------

    def test_build_refuses_cross_manifest_duplicate_stable_id(self):
        doc1 = _document(self.root_id, self.alloc)
        doc2 = _document(self.root_id, self.alloc)
        doc2["artifacts"][0]["artifact_id"] = doc1["artifacts"][0]["artifact_id"]
        doc2["artifact_revisions"][0]["artifact_id"] = doc1["artifacts"][0]["artifact_id"]
        doc2["events"][0]["target_id"] = doc1["artifacts"][0]["artifact_id"]
        items = [
            (doc1, "p/c1", m.manifest_digest(doc1), doc1["manifest_id"]),
            (doc2, "p/c2", m.manifest_digest(doc2), doc2["manifest_id"]),
        ]
        with self.assertRaises(ValueError):
            ix.build(items)

    def test_reusable_rows_are_order_independent(self):
        # Rebuild input order is lexical while incremental order is admission
        # order; shared-entity rows must not depend on either (D-7).
        doc1 = _document(self.root_id, self.alloc)
        doc2 = _document(self.root_id, self.alloc)
        doc2["campaign"] = dict(doc1["campaign"])
        doc2["cycle"]["campaign_id"] = doc1["campaign"]["campaign_id"]
        items_fwd = [
            (doc1, "p/c1", m.manifest_digest(doc1), doc1["manifest_id"]),
            (doc2, "p/c2", m.manifest_digest(doc2), doc2["manifest_id"]),
        ]
        items_rev = list(reversed(items_fwd))
        self.assertEqual(
            ix.canonical_bytes(ix.build(items_fwd)),
            ix.canonical_bytes(ix.build(items_rev)),
        )

    def test_check_rejects_self_parent_cycle(self):
        doc = _document(self.root_id, self.alloc)
        doc["cycle"]["parent_cycle_id"] = doc["cycle"]["cycle_id"]
        digest = m.manifest_digest(doc)
        report = ix.check(
            ix.empty(self.root_id),
            doc,
            idempotency_key=doc["manifest_id"],
            manifest_digest=digest,
        )
        self.assertFalse(report.ok)
        self.assertIn("index-self-parent-cycle", {v.code for v in report.violations})

    def test_check_rejects_cross_campaign_parent_cycle(self):
        doc1 = _document(self.root_id, self.alloc)
        index, _digest1, _key1 = self._apply(ix.empty(self.root_id), doc1)
        doc2 = _document(self.root_id, self.alloc)
        doc2["cycle"]["parent_cycle_id"] = doc1["cycle"]["cycle_id"]
        digest2 = m.manifest_digest(doc2)
        report = ix.check(
            index, doc2, idempotency_key=doc2["manifest_id"], manifest_digest=digest2
        )
        self.assertFalse(report.ok)
        self.assertIn(
            "index-parent-cycle-campaign-mismatch", {v.code for v in report.violations}
        )

    def test_build_refuses_circular_parent_chain(self):
        doc1 = _document(self.root_id, self.alloc)
        doc2 = _document(self.root_id, self.alloc)
        doc2["campaign"] = dict(doc1["campaign"])
        doc2["cycle"]["campaign_id"] = doc1["campaign"]["campaign_id"]
        doc1["cycle"]["parent_cycle_id"] = doc2["cycle"]["cycle_id"]
        doc2["cycle"]["parent_cycle_id"] = doc1["cycle"]["cycle_id"]
        items = [
            (doc1, "p/c1", m.manifest_digest(doc1), doc1["manifest_id"]),
            (doc2, "p/c2", m.manifest_digest(doc2), doc2["manifest_id"]),
        ]
        with self.assertRaises(ValueError):
            ix.build(items)

    def test_parse_rejects_traversal_cycle_path(self):
        doc = _document(self.root_id, self.alloc)
        index, _digest, _key = self._apply(ix.empty(self.root_id), doc)
        payload = ix.to_payload(index)
        row = payload["cycles"][doc["cycle"]["cycle_id"]]
        row["cycle_path"] = "../outside"
        with self.assertRaises(ValueError):
            ix.parse(payload)

    def test_build_accepts_multi_cycle_campaign(self):
        doc1 = _document(self.root_id, self.alloc)
        doc2 = _document(self.root_id, self.alloc)
        doc2["campaign"] = dict(doc1["campaign"])
        doc2["cycle"]["campaign_id"] = doc1["campaign"]["campaign_id"]
        items = [
            (doc1, "p/c1", m.manifest_digest(doc1), doc1["manifest_id"]),
            (doc2, "p/c2", m.manifest_digest(doc2), doc2["manifest_id"]),
        ]
        index = ix.build(items)
        self.assertEqual(len(index.cycles), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
