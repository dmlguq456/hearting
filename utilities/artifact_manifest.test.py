from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_identity as idm
import artifact_manifest as m


def _sha(n):
    return "sha256:" + (str(n) * 64)[:64]


def _valid_document():
    alloc = idm.IdAllocator()
    root_id = alloc.allocate("artifact_root")
    repo_id = alloc.allocate("repository")
    camp_id = alloc.allocate("campaign")
    cyc_id = alloc.allocate("cycle")
    art_id = alloc.allocate("artifact")
    art_id2 = alloc.allocate("artifact")
    arev_id = alloc.allocate("artifact_revision")
    arev_id2 = alloc.allocate("artifact_revision")
    man_id = alloc.allocate("manifest")
    mrev_id = alloc.allocate("manifest_revision")
    prod_id = alloc.allocate("producer")
    evt_id = alloc.allocate("event")
    evt_id2 = alloc.allocate("event")
    strm_id = alloc.allocate("stream")
    ref_id = alloc.allocate("shared_reference")
    rrev_id = alloc.allocate("shared_reference_revision")

    provenance = {
        "source_manifest_id": man_id,
        "source_revision_id": mrev_id,
        "producer_route_id": "r-1",
        "algorithm_version": "v1",
        "schema_version": 1,
        "source_digest": _sha(2),
    }

    return {
        "schema_version": 2,
        "manifest_kind": "artifact.cycle",
        "manifest_id": man_id,
        "manifest_revision_id": mrev_id,
        "repository_id": repo_id,
        "artifact_root_id": root_id,
        "campaign": {
            "campaign_id": camp_id,
            "goal": "g",
            "completion_criterion": {"statement": "s"},
            "title": "t",
            "state": "active",
        },
        "cycle": {
            "cycle_id": cyc_id,
            "campaign_id": camp_id,
            "parent_cycle_id": None,
            "started_on": "2026-08-11T00:00:00Z",
            "input_digest": _sha(0),
            "outcome_criterion": {"required_artifact_roles": ["primary"], "decision_required": False},
            "state": "active",
        },
        "artifacts": [
            {"artifact_id": art_id, "cycle_id": cyc_id, "role": "primary", "type": "doc", "capability": "autopilot-code", "title": "t"},
            {"artifact_id": art_id2, "cycle_id": cyc_id, "role": "secondary", "type": "doc", "capability": "autopilot-code", "title": "t2"},
        ],
        "artifact_revisions": [
            {
                "artifact_revision_id": arev_id,
                "artifact_id": art_id,
                "revision_sequence": 1,
                "content_digest": _sha(1),
                "byte_size": 10,
                "media_type": "text/plain",
                "locator": {"kind": "cycle-relative", "path": "plan.md"},
                "provenance": provenance,
            },
            {
                "artifact_revision_id": arev_id2,
                "artifact_id": art_id2,
                "revision_sequence": 1,
                "content_digest": _sha(3),
                "byte_size": 20,
                "media_type": "text/plain",
                "locator": {"kind": "cycle-relative", "path": "sub/checklist.md"},
                "provenance": provenance,
            },
        ],
        "shared_references": [
            {"shared_reference_id": ref_id, "kind": "shared-spec", "title": "spec"},
        ],
        "shared_reference_revisions": [
            {
                "shared_reference_revision_id": rrev_id,
                "shared_reference_id": ref_id,
                "content_digest": _sha(4),
                "updated_at": "2026-08-11T00:00:00Z",
                "provenance": provenance,
            },
        ],
        "routes": [
            {
                "artifact_root_id": root_id,
                "route_id": "rt-1",
                "route_hash": _sha(5),
                "terminal_marker": "m",
                "terminal_evidence_id": evt_id,
            },
        ],
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
            },
            {
                "event_id": evt_id2,
                "stream_id": strm_id,
                "stream_sequence": 2,
                "event_type": "artifact.revision.recorded",
                "target_id": art_id2,
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:01:00Z",
                "provenance": provenance,
                "evidence_ids": [evt_id],
                "payload": {},
            },
        ],
        "producer": {"producer_id": prod_id, "contract_version": "artifact-cycle-manifest/v2", "source_revision": "abc"},
    }


def _codes(report):
    return {v.code for v in report.violations}


class TestPositive(unittest.TestCase):
    def test_accepts_valid_multi_artifact_manifest_with_three_shared_reference_kinds(self):
        for kind in ("shared-spec", "cumulative-analysis", "shared-research"):
            doc = _valid_document()
            doc["shared_references"][0]["kind"] = kind
            report = m.validate(doc)
            self.assertTrue(report.ok, report.violations)


class TestClosedSchemaUnknownKey(unittest.TestCase):
    def test_rejects_unknown_top_level_key(self):
        doc = _valid_document()
        doc["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_campaign(self):
        doc = _valid_document()
        doc["campaign"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_cycle(self):
        doc = _valid_document()
        doc["cycle"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_outcome_criterion(self):
        doc = _valid_document()
        doc["cycle"]["outcome_criterion"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_artifact_row(self):
        doc = _valid_document()
        doc["artifacts"][0]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_artifact_revision_row(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_locator(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["locator"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_provenance(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["provenance"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_shared_reference_row(self):
        doc = _valid_document()
        doc["shared_references"][0]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_shared_reference_revision_row(self):
        doc = _valid_document()
        doc["shared_reference_revisions"][0]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_route_row(self):
        doc = _valid_document()
        doc["routes"][0]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_event_row(self):
        doc = _valid_document()
        doc["events"][0]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_event_actor(self):
        doc = _valid_document()
        doc["events"][0]["actor"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))

    def test_rejects_unknown_key_in_producer(self):
        doc = _valid_document()
        doc["producer"]["bogus"] = 1
        self.assertIn("unknown-key", _codes(m.validate_shape(doc)))


class TestMissingAndTypes(unittest.TestCase):
    def test_rejects_missing_required_top_level_key(self):
        doc = _valid_document()
        del doc["producer"]
        self.assertIn("missing-key", _codes(m.validate_shape(doc)))

    def test_rejects_missing_required_key_in_event_row(self):
        doc = _valid_document()
        del doc["events"][0]["actor"]
        self.assertIn("missing-key", _codes(m.validate_shape(doc)))

    def test_rejects_wrong_schema_version(self):
        doc = _valid_document()
        doc["schema_version"] = 1
        self.assertIn("wrong-literal", _codes(m.validate_shape(doc)))

    def test_rejects_wrong_manifest_kind(self):
        doc = _valid_document()
        doc["manifest_kind"] = "something.else"
        self.assertIn("wrong-literal", _codes(m.validate_shape(doc)))

    def test_rejects_non_integer_revision_sequence(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["revision_sequence"] = 1.0
        report = m.validate_shape(doc)
        self.assertTrue({"wrong-type", "value-float-forbidden"} & _codes(report))

    def test_rejects_float_value_anywhere(self):
        doc = _valid_document()
        doc["campaign"]["goal"] = 1.5
        self.assertIn("value-float-forbidden", _codes(m.validate_shape(doc)))

    def test_rejects_malformed_typed_id(self):
        doc = _valid_document()
        doc["manifest_id"] = "not-a-typed-id"
        self.assertIn("malformed-typed-id", _codes(m.validate_shape(doc)))

    def test_rejects_non_rfc3339_recorded_at(self):
        doc = _valid_document()
        doc["events"][0]["recorded_at"] = "2026-08-11 00:00:00"
        self.assertIn("malformed-timestamp", _codes(m.validate_shape(doc)))

    def test_rejects_malformed_media_type(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["media_type"] = "TEXT/PLAIN; charset=utf-8"
        self.assertIn("malformed-media-type", _codes(m.validate_shape(doc)))

    def test_rejects_oversized_event_payload(self):
        doc = _valid_document()
        doc["events"][0]["payload"] = {"blob": "x" * (70 * 1024)}
        self.assertIn("oversized-payload", _codes(m.validate_shape(doc)))

    def test_rejects_payload_too_deep(self):
        doc = _valid_document()
        nested = {}
        cursor = nested
        for _ in range(20):
            cursor["n"] = {}
            cursor = cursor["n"]
        doc["events"][0]["payload"] = nested
        self.assertIn("payload-too-deep", _codes(m.validate_shape(doc)))


class TestLocatorSafety(unittest.TestCase):
    def _with_path(self, path_value):
        doc = _valid_document()
        doc["artifact_revisions"][0]["locator"]["path"] = path_value
        return doc

    def test_rejects_absolute_locator(self):
        self.assertIn("locator-absolute", _codes(m.validate_locators(self._with_path("/etc/passwd"))))

    def test_rejects_parent_escape_locator(self):
        self.assertIn("locator-dot-segment", _codes(m.validate_locators(self._with_path("../secret"))))

    def test_rejects_dot_segment_locator(self):
        self.assertIn("locator-dot-segment", _codes(m.validate_locators(self._with_path("a/./b"))))

    def test_rejects_backslash_or_control_char_locator(self):
        self.assertIn("locator-backslash", _codes(m.validate_locators(self._with_path("a\\b"))))
        self.assertIn("locator-control-char", _codes(m.validate_locators(self._with_path("a\x01b"))))

    def test_rejects_hidden_component_locator(self):
        self.assertIn("locator-hidden-component", _codes(m.validate_locators(self._with_path(".hidden"))))

    def test_rejects_empty_or_trailing_slash_locator(self):
        self.assertIn("locator-empty", _codes(m.validate_locators(self._with_path(""))))
        self.assertIn("locator-trailing-slash", _codes(m.validate_locators(self._with_path("a/"))))

    def test_rejects_reserved_manifest_filename_locator(self):
        self.assertIn("locator-reserved-name", _codes(m.validate_locators(self._with_path("manifest.json"))))

    def test_rejects_duplicate_locator_path(self):
        doc = _valid_document()
        doc["artifact_revisions"][1]["locator"]["path"] = doc["artifact_revisions"][0]["locator"]["path"]
        self.assertIn("locator-duplicate-path", _codes(m.validate_locators(doc)))


class TestOrphanAndCompleteness(unittest.TestCase):
    def test_rejects_orphan_artifact_revision(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["artifact_id"] = "art_" + "9" * 32
        self.assertIn("orphan-artifact-revision", _codes(m.validate_lineage(doc)))

    def test_rejects_cycle_campaign_id_mismatch(self):
        doc = _valid_document()
        doc["cycle"]["campaign_id"] = "camp_" + "9" * 32
        self.assertIn("cycle-campaign-id-mismatch", _codes(m.validate_lineage(doc)))

    def test_rejects_artifact_cycle_id_mismatch(self):
        doc = _valid_document()
        doc["artifacts"][0]["cycle_id"] = "cyc_" + "9" * 32
        self.assertIn("artifact-cycle-id-mismatch", _codes(m.validate_lineage(doc)))

    def test_rejects_orphan_shared_reference_revision(self):
        doc = _valid_document()
        doc["shared_reference_revisions"][0]["shared_reference_id"] = "ref_" + "9" * 32
        self.assertIn("orphan-shared-reference-revision", _codes(m.validate_lineage(doc)))

    def test_rejects_route_root_id_mismatch(self):
        doc = _valid_document()
        doc["routes"][0]["artifact_root_id"] = "root_" + "9" * 32
        self.assertIn("route-root-id-mismatch", _codes(m.validate_lineage(doc)))

    def test_rejects_event_target_id_not_declared(self):
        doc = _valid_document()
        doc["events"][0]["target_id"] = "art_" + "9" * 32
        self.assertIn("event-target-id-not-declared", _codes(m.validate_lineage(doc)))

    def test_rejects_unresolvable_evidence_id(self):
        doc = _valid_document()
        doc["events"][0]["evidence_ids"] = ["evt_" + "9" * 32]
        self.assertIn("unresolvable-evidence-id", _codes(m.validate_lineage(doc)))

    def test_rejects_completed_cycle_missing_required_role(self):
        doc = _valid_document()
        doc["cycle"]["state"] = "completed"
        doc["cycle"]["outcome_criterion"]["required_artifact_roles"] = ["primary", "tertiary"]
        doc["events"].append(
            {
                "event_id": "evt_" + "8" * 32,
                "stream_id": doc["events"][0]["stream_id"],
                "stream_sequence": 3,
                "event_type": "cycle.completed",
                "target_id": doc["cycle"]["cycle_id"],
                "actor": {"kind": "user", "id": "u"},
                "recorded_at": "2026-08-11T00:02:00Z",
                "provenance": doc["events"][0]["provenance"],
                "evidence_ids": [],
                "payload": {},
            }
        )
        doc["events"].append(
            {
                "event_id": "evt_" + "7" * 32,
                "stream_id": doc["events"][0]["stream_id"],
                "stream_sequence": 4,
                "event_type": "route.terminal.recorded",
                "target_id": doc["cycle"]["cycle_id"],
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:03:00Z",
                "provenance": doc["events"][0]["provenance"],
                "evidence_ids": [],
                "payload": {},
            }
        )
        self.assertIn("cycle-completion-incomplete", _codes(m.validate_lineage(doc)))

    def test_rejects_completed_cycle_without_terminal_route_evidence(self):
        doc = _valid_document()
        doc["cycle"]["state"] = "completed"
        doc["cycle"]["outcome_criterion"]["required_artifact_roles"] = []
        doc["events"].append(
            {
                "event_id": "evt_" + "8" * 32,
                "stream_id": doc["events"][0]["stream_id"],
                "stream_sequence": 3,
                "event_type": "cycle.completed",
                "target_id": doc["cycle"]["cycle_id"],
                "actor": {"kind": "user", "id": "u"},
                "recorded_at": "2026-08-11T00:02:00Z",
                "provenance": doc["events"][0]["provenance"],
                "evidence_ids": [],
                "payload": {},
            }
        )
        self.assertIn("cycle-completion-incomplete", _codes(m.validate_lineage(doc)))

    def test_rejects_completed_cycle_without_decision_event(self):
        doc = _valid_document()
        doc["cycle"]["state"] = "completed"
        doc["cycle"]["outcome_criterion"]["required_artifact_roles"] = []
        doc["cycle"]["outcome_criterion"]["decision_required"] = True
        doc["events"].append(
            {
                "event_id": "evt_" + "8" * 32,
                "stream_id": doc["events"][0]["stream_id"],
                "stream_sequence": 3,
                "event_type": "cycle.completed",
                "target_id": doc["cycle"]["cycle_id"],
                "actor": {"kind": "user", "id": "u"},
                "recorded_at": "2026-08-11T00:02:00Z",
                "provenance": doc["events"][0]["provenance"],
                "evidence_ids": [],
                "payload": {},
            }
        )
        doc["events"].append(
            {
                "event_id": "evt_" + "7" * 32,
                "stream_id": doc["events"][0]["stream_id"],
                "stream_sequence": 4,
                "event_type": "route.terminal.recorded",
                "target_id": doc["cycle"]["cycle_id"],
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:03:00Z",
                "provenance": doc["events"][0]["provenance"],
                "evidence_ids": [],
                "payload": {},
            }
        )
        self.assertIn("cycle-completion-incomplete", _codes(m.validate_lineage(doc)))


class TestDuplicatesAndSequence(unittest.TestCase):
    def test_rejects_duplicate_stable_id_within_manifest(self):
        doc = _valid_document()
        doc["artifacts"][1]["artifact_id"] = doc["artifacts"][0]["artifact_id"]
        self.assertIn("duplicate-stable-id", _codes(m.validate_lineage(doc)))

    def test_rejects_reused_revision_id_within_manifest(self):
        doc = _valid_document()
        doc["artifact_revisions"][1]["artifact_revision_id"] = doc["artifact_revisions"][0]["artifact_revision_id"]
        self.assertIn("reused-revision-id", _codes(m.validate_lineage(doc)))

    def test_rejects_reused_event_id_within_manifest(self):
        doc = _valid_document()
        doc["events"][1]["event_id"] = doc["events"][0]["event_id"]
        self.assertIn("reused-event-id", _codes(m.validate_lineage(doc)))
        self.assertIn("event-id-reused", _codes(m.validate_events(doc)))

    def test_rejects_non_monotonic_stream_sequence(self):
        doc = _valid_document()
        doc["events"][0]["stream_sequence"] = 2
        doc["events"][1]["stream_sequence"] = 1
        self.assertIn("event-sequence-nonmonotonic", _codes(m.validate_events(doc)))

    def test_rejects_gap_in_stream_sequence(self):
        doc = _valid_document()
        doc["events"][1]["stream_sequence"] = 3
        self.assertIn("event-sequence-gap", _codes(m.validate_events(doc)))

    def test_rejects_duplicate_stream_sequence(self):
        doc = _valid_document()
        doc["events"][1]["stream_sequence"] = 1
        self.assertIn("event-sequence-duplicate", _codes(m.validate_events(doc)))

    def test_rejects_revision_sequence_not_starting_at_one(self):
        doc = _valid_document()
        doc["artifact_revisions"][0]["revision_sequence"] = 2
        self.assertIn("revision-append-out-of-scope", _codes(m.validate_lineage(doc)))

    def test_rejects_duplicate_route_composite_within_manifest(self):
        doc = _valid_document()
        doc["routes"].append(dict(doc["routes"][0]))
        self.assertIn("duplicate-route-composite", _codes(m.validate_lineage(doc)))


class TestTransitions(unittest.TestCase):
    def test_rejects_declared_state_unreachable_from_events(self):
        doc = _valid_document()
        doc["cycle"]["state"] = "completed"
        self.assertIn("illegal-transition", _codes(m.validate_lineage(doc)))

    def test_rejects_unknown_event_type(self):
        doc = _valid_document()
        doc["events"][0]["event_type"] = "not.a.real.type"
        self.assertIn("wrong-value", _codes(m.validate_shape(doc)))

    def test_rejects_campaign_satisfied_without_user_actor_event(self):
        doc = _valid_document()
        doc["campaign"]["state"] = "satisfied"
        doc["events"].append(
            {
                "event_id": "evt_" + "8" * 32,
                "stream_id": doc["events"][0]["stream_id"],
                "stream_sequence": 3,
                "event_type": "campaign.satisfied",
                "target_id": doc["campaign"]["campaign_id"],
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:02:00Z",
                "provenance": doc["events"][0]["provenance"],
                "evidence_ids": [],
                "payload": {},
            }
        )
        self.assertIn("campaign-satisfaction-unauthorized", _codes(m.validate_lineage(doc)))

    def test_rejects_transition_out_of_terminal_state(self):
        """A stream that reaches a terminal state cannot transition again.

        `cycle.completed` at sequence 2 is terminal for that stream; a later
        `cycle.abandoned` at sequence 3 on the SAME stream is a transition out
        of a terminal state and must be refused (D-10 cycle completion).
        """
        doc = _valid_document()
        stream_id = doc["events"][0]["stream_id"]
        provenance = doc["events"][0]["provenance"]
        cycle_id = doc["cycle"]["cycle_id"]
        for seq, event_type, suffix in (
            (2, "cycle.completed", "a"),
            (3, "cycle.abandoned", "b"),
        ):
            doc["events"].append(
                {
                    "event_id": "evt_" + (suffix * 32),
                    "stream_id": stream_id,
                    "stream_sequence": seq,
                    "event_type": event_type,
                    "target_id": cycle_id,
                    "actor": {"kind": "producer", "id": "p"},
                    "recorded_at": "2026-08-11T00:0{0}:00Z".format(seq),
                    "provenance": provenance,
                    "evidence_ids": [],
                    "payload": {},
                }
            )
        self.assertIn("illegal-transition", _codes(m.validate_lineage(doc)))


class TestSupersedeRevoke(unittest.TestCase):
    def _extra_event(self, doc, **overrides):
        base = {
            "event_id": "evt_" + "8" * 32,
            "stream_id": doc["events"][0]["stream_id"],
            "stream_sequence": 3,
            "event_type": "user.correction.recorded",
            "target_id": doc["events"][0]["event_id"],
            "actor": {"kind": "user", "id": "u"},
            "recorded_at": "2026-08-11T00:02:00Z",
            "provenance": doc["events"][0]["provenance"],
            "evidence_ids": [],
            "payload": {},
        }
        base.update(overrides)
        return base

    def test_rejects_self_supersession(self):
        doc = _valid_document()
        doc["events"][0]["supersedes_event_id"] = doc["events"][0]["event_id"]
        self.assertIn("event-self-supersession", _codes(m.validate_events(doc)))

    def test_rejects_both_supersedes_and_revokes(self):
        doc = _valid_document()
        doc["events"][1]["supersedes_event_id"] = doc["events"][0]["event_id"]
        doc["events"][1]["revokes_event_id"] = doc["events"][0]["event_id"]
        self.assertIn("event-supersede-and-revoke", _codes(m.validate_events(doc)))

    def test_rejects_dangling_supersedes_target(self):
        doc = _valid_document()
        doc["events"][1]["supersedes_event_id"] = "evt_" + "9" * 32
        self.assertIn("event-dangling-supersession-target", _codes(m.validate_events(doc)))

    def test_rejects_double_supersession_of_same_event(self):
        doc = _valid_document()
        doc["events"][1]["supersedes_event_id"] = doc["events"][0]["event_id"]
        doc["events"].append(self._extra_event(doc, supersedes_event_id=doc["events"][0]["event_id"]))
        self.assertIn("event-double-supersession", _codes(m.validate_events(doc)))

    def test_rejects_curator_actor_revoking_user_event(self):
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["actor"] = {"kind": "curator-proposal-accepted", "id": "c"}
        doc["events"][1]["revokes_event_id"] = doc["events"][0]["event_id"]
        self.assertIn("event-supersession-unauthorized", _codes(m.validate_events(doc)))

    def test_rejects_producer_actor_revoking_user_event(self):
        # F4/D-11: user-correction precedence binds every non-user actor, not
        # only the curator lane.
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["actor"] = {"kind": "producer", "id": "p"}
        doc["events"][1]["revokes_event_id"] = doc["events"][0]["event_id"]
        self.assertIn("event-supersession-unauthorized", _codes(m.validate_events(doc)))

    def test_rejects_system_actor_superseding_user_event(self):
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["actor"] = {"kind": "system", "id": "s"}
        doc["events"][1]["supersedes_event_id"] = doc["events"][0]["event_id"]
        self.assertIn("event-supersession-unauthorized", _codes(m.validate_events(doc)))

    def test_allows_user_actor_superseding_user_event(self):
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["supersedes_event_id"] = doc["events"][0]["event_id"]
        self.assertNotIn("event-supersession-unauthorized", _codes(m.validate_events(doc)))


class TestDeterminism(unittest.TestCase):
    def test_canonical_bytes_are_key_order_independent(self):
        a = {"b": 1, "a": 2}
        b = {"a": 2, "b": 1}
        self.assertEqual(m.canonical_bytes(a), m.canonical_bytes(b))

    def test_canonical_bytes_utf8_and_single_trailing_newline(self):
        data = m.canonical_bytes({"k": "한"})
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)
        self.assertIn("한".encode("utf-8"), data)

    def test_validate_twice_is_byte_identical(self):
        doc = _valid_document()
        r1 = m.validate(doc)
        r2 = m.validate(doc)
        self.assertEqual(json.dumps(r1.to_payload()), json.dumps(r2.to_payload()))

    def test_violations_are_sorted_deterministically(self):
        doc = _valid_document()
        del doc["producer"]
        doc["bogus"] = 1
        report = m.validate_shape(doc)
        codes_paths = [(v.code, v.path) for v in report.violations]
        self.assertEqual(codes_paths, sorted(codes_paths))

    def test_fold_orders_by_stream_sequence_not_recorded_at(self):
        doc = _valid_document()
        doc["events"][0]["recorded_at"] = "2026-08-11T23:00:00Z"
        doc["events"][1]["recorded_at"] = "2026-08-11T00:00:00Z"
        folded = m.fold_events(doc)
        self.assertEqual([e["event_id"] for e in folded], [doc["events"][0]["event_id"], doc["events"][1]["event_id"]])

    def test_fold_ignores_unauthorized_supersession_of_user_event(self):
        # F4/D-11: even called standalone, fold never lets a producer/system
        # link remove a user event; only a user event can.
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["actor"] = {"kind": "producer", "id": "p"}
        doc["events"][1]["revokes_event_id"] = doc["events"][0]["event_id"]
        folded = m.fold_events(doc)
        self.assertIn(doc["events"][0]["event_id"], [e["event_id"] for e in folded])

    def test_fold_applies_authorized_user_supersession(self):
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["actor"] = {"kind": "user", "id": "u"}
        doc["events"][1]["supersedes_event_id"] = doc["events"][0]["event_id"]
        folded = m.fold_events(doc)
        self.assertNotIn(doc["events"][0]["event_id"], [e["event_id"] for e in folded])

    def test_enum_unhashable_value_rejected_not_crash(self):
        # F1a: an unhashable enum candidate must be a wrong-value violation,
        # never a TypeError escaping the validator.
        doc = _valid_document()
        doc["events"][0]["event_type"] = []
        report = m.validate(doc)
        self.assertFalse(report.ok)
        self.assertIn("wrong-value", {v.code for v in report.violations})

    def test_enum_unhashable_actor_kind_rejected_not_crash(self):
        doc = _valid_document()
        doc["events"][0]["actor"] = {"kind": {}, "id": "x"}
        report = m.validate(doc)
        self.assertFalse(report.ok)
        self.assertIn("wrong-value", {v.code for v in report.violations})

    def test_fold_is_stable_under_input_shuffle(self):
        doc = _valid_document()
        shuffled = copy.deepcopy(doc)
        shuffled["events"] = list(reversed(shuffled["events"]))
        self.assertEqual(m.fold_events(doc), m.fold_events(shuffled))

    def test_fold_twice_is_byte_identical(self):
        doc = _valid_document()
        f1 = json.dumps(m.fold_events(doc))
        f2 = json.dumps(m.fold_events(doc))
        self.assertEqual(f1, f2)


class TestDeclaredHelpers(unittest.TestCase):
    def test_declared_ids_covers_all_kinds_present(self):
        doc = _valid_document()
        ids = m.declared_ids(doc)
        self.assertEqual(ids[doc["campaign"]["campaign_id"]], "campaign")
        self.assertEqual(ids[doc["cycle"]["cycle_id"]], "cycle")
        self.assertEqual(ids[doc["artifacts"][0]["artifact_id"]], "artifact")

    def test_declared_routes_and_streams(self):
        doc = _valid_document()
        self.assertIn((doc["artifact_root_id"], "rt-1"), m.declared_routes(doc))
        streams = m.declared_streams(doc)
        self.assertEqual(streams[doc["events"][0]["stream_id"]], (1, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
