#!/usr/bin/env python3
"""Temporary-registry regressions for historical batch consumer paths."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_replacement_batch as REPLACEMENT
import model_profile
import recovery_history as HISTORY
from replica_batch_contract import build_manifest, verify_manifest
from route_identity import route_hash, route_id_from_hash

SPEC = importlib.util.spec_from_file_location("recovery_history_batch", Path(__file__).with_name("dispatch-batch.py"))
BATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BATCH)


class RecoveryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.route_file = self.base / "source-route.json"
        self.prompt = "Run the original bounded group."
        self.configure()

    def configure(self, width=3, encoding="persona", version=3):
        demand = {"schema_version": 1, "judgment_requirement": "predetermined",
            "execution_scope": "short-local", "judgment_reason": "bounded original work",
            "execution_reason": "original scope", "evidence_refs": ["fixture:original"]}
        profiles = ("deep", "balanced", "light")[:width]
        nodes = [{"id": "leg-" + str(i), "parallel_group": "work", "dispatch_depth": 2,
            "parallel_leg_index": i, "parallel_leg_count": width,
            "parallel_independence_axes": ["cross-harness", "model-profile", "perspective"],
            "model_profile": profiles[i], "perspective": "original-" + str(i),
            **({"profile_demand": demand,
                "profile_selection": model_profile.resolve_profile_demand(demand, explicit_profile=profiles[i])}
               if version == 3 else {})} for i in range(width)]
        self.route = {"cwd": str(self.base), "nodes": nodes, "parent_harness": "codex"}
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route))
        members = [{"assignment_sha256": "sha256:" + hashlib.sha256(self.prompt.encode()).hexdigest(),
            "attempt_id": "att-original-" + str(i), "route_node": node["id"],
            "harness": ("codex", "claude", "opencode")[i] if encoding == "cross-harness" or width == 3 else "codex",
            "fallback_hop": "same-harness-headless" if i == 0 else "cross-harness-headless",
            "fallback_ordinal": i + 1, "model_profile": node["model_profile"],
            "perspective": node["perspective"], "parallel_leg_index": i, "leg_class": "peer",
            **({key: node[key] for key in ("profile_demand", "profile_selection")} if version == 3 else {})}
            for i, node in enumerate(nodes)]
        if encoding == "degraded-same-harness":
            for member in members:
                member["harness"] = "codex"
        realized = ["model-profile", "perspective"]
        if len({member["harness"] for member in members}) >= 2:
            realized.insert(0, "cross-harness")
        manifest, _, _ = build_manifest(parallel_group="work", route_id=self.route["route_id"],
            parent_attempt_id="att-owner-original", independence=encoding, members=members,
            required_independence_axes=nodes[0]["parallel_independence_axes"], realized_independence_axes=realized,
            degradation_reason="cross-harness-unavailable-user-allowed" if encoding == "degraded-same-harness" else "")
        if version == 2:
            manifest = dict(manifest, schema_version=2, members=[
                {key: value for key, value in member.items() if key != "leg_class"}
                for member in manifest["members"]])
        elif version == 1:
            fields = {"assignment_sha256", "attempt_id", "route_node", "harness", "fallback_hop", "fallback_ordinal"}
            manifest = {"schema_version": 1, "kind": "replica-batch", "declared_size": 2,
                "replica_group": "work", "route_id": self.route["route_id"],
                "parent_attempt_id": "att-owner-original", "independence": encoding,
                "members": [{key: value for key, value in member.items() if key in fields}
                            for member in manifest["members"]]}
        self.manifest, self.digest, leg_digests = verify_manifest(manifest)
        self.rows = []
        for member in self.manifest["members"]:
            row = {"attempt_schema_version": "2" if version >= 2 else "1", "dispatch_depth": "2",
                "transport": "headless", "execution_surface": "registered-headless", "registered_worker": "1",
                "parent": "owner-original", "parent_attempt_id": "att-owner-original",
                "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                "route_file": str(self.route_file), "route_node": member["route_node"],
                "attempt_id": member["attempt_id"], "harness": member["harness"],
                "fallback_hop": member["fallback_hop"], "fallback_ordinal": str(member["fallback_ordinal"]),
                "batch_group": "work", "batch_declared_size": str(width),
                "batch_route_id": self.route["route_id"], "batch_parent_attempt_id": "att-owner-original",
                "batch_attempt_id": member["attempt_id"], "batch_route_node": member["route_node"],
                "batch_harness": member["harness"], "batch_fallback_hop": member["fallback_hop"],
                "batch_fallback_ordinal": str(member["fallback_ordinal"]), "batch_independence": encoding,
                "batch_assignment_sha256": member["assignment_sha256"], "batch_manifest_sha256": self.digest,
                "batch_leg_sha256": leg_digests[member["attempt_id"]]}
            if version >= 2:
                row.update({"batch_" + key: str(member[key]) for key in ("model_profile", "perspective", "parallel_leg_index")})
            self.rows.append(row)
        self.write_rows()
        self.partial = {"source_group_id": "work", "source_batch_manifest_digest": self.digest,
            "gap_leg_id": self.rows[-1]["route_node"], "failed_source_attempt_id": self.rows[-1]["attempt_id"],
            "realized_peer_set": [{"node_id": row["route_node"], "terminal_attempt_id": row["attempt_id"]}
                                  for row in self.rows[:-1]]}
        self.current = copy.deepcopy(self.route)
        self.current["parent_harness"] = "opencode"
        for node in self.current["nodes"]:
            node["model_profile"] = "top"
            node["perspective"] = "current-" + node["id"]
            node["parallel_independence_axes"] = ["perspective"]

    def write_rows(self):
        self.jobs.write_text("".join("\t".join(("2026-10-09T00:00:00Z", "done", str(self.base), str(self.base),
            row["attempt_id"], ",".join(key + "=" + value for key, value in row.items()))) + "\n"
            for row in self.rows))

    def seal(self):
        args = SimpleNamespace(continuation=None, route=self.route_file, parallel_group="work",
            slug_prefix="original", parent="owner-original", prompt_text=self.prompt,
            qa=None, log_dir=None, allow_degraded_independence=False, subdivision_manifest=None)
        REPLACEMENT.seal_launch_input(self.jobs, args, self.route, self.manifest, self.digest)

    def partial_input(self):
        return BATCH.partial_batch_input(self.jobs, self.current, self.current["nodes"], self.partial)

    def test_payload_is_primary_and_all_three_harness_records_are_preserved(self):
        self.seal()
        before = self.jobs.read_bytes()
        self.route_file.unlink()
        with mock.patch.object(HISTORY, "reconstruct_batch_input", side_effect=AssertionError("current reconstruction")):
            payload = self.partial_input()
            source = REPLACEMENT._input(self.jobs, self.rows[-1])
        self.assertEqual(payload, source)
        self.assertEqual(payload["manifest"], self.manifest)
        self.assertEqual(payload["manifest_digest"], self.digest)
        self.assertEqual({member["harness"] for member in payload["manifest"]["members"]}, {"codex", "claude", "opencode"})
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_payload_absent_uses_original_route_axes_and_model_demand(self):
        before = self.jobs.read_bytes()
        payload = self.partial_input()
        self.assertEqual(payload["manifest"], self.manifest)
        self.assertEqual(payload["manifest_digest"], self.digest)
        self.assertEqual(payload["manifest"]["parent_attempt_id"], "att-owner-original")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_old_payload_rows_need_no_new_mirror_columns(self):
        self.configure(width=2, version=1, encoding="cross-harness")
        self.seal()
        for row in self.rows:
            for key in list(row):
                if key.startswith("batch_") and key not in {"batch_manifest_sha256", "batch_leg_sha256"}:
                    row.pop(key)
        self.write_rows()
        self.assertEqual(REPLACEMENT._input(self.jobs, self.rows[0])["manifest"], self.manifest)
        self.assertEqual(self.partial_input()["manifest_digest"], self.digest)

    def test_partial_uses_current_admitted_tuple_and_keeps_original_manifest(self):
        self.seal()
        before = self.jobs.read_bytes()
        admitted = SimpleNamespace(fallback_hop="cross-harness-headless", ordinal=8)
        with mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", return_value=admitted):
            assignments = BATCH.partial_source_assignments(self.jobs, self.current,
                                                          self.current["nodes"], self.partial,
                                                          {"parent_harness": "opencode"})
        self.assertTrue(all((row[2], row[3]) == ("cross-harness-headless", 8) for row in assignments))
        self.assertEqual(self.partial_input()["manifest"], self.manifest)
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_legacy_schemas_and_independence_encoding_are_read_without_rewrite(self):
        for version in (1, 2, 3):
            for encoding in ("cross-harness", "degraded-same-harness", "persona"):
                with self.subTest(version=version, encoding=encoding):
                    self.configure(width=2, encoding=encoding, version=version)
                    before = self.jobs.read_bytes()
                    payload = self.partial_input()
                    self.assertEqual(payload["manifest"], self.manifest)
                    self.assertEqual(payload["manifest_digest"], self.digest)
                    self.assertEqual(self.jobs.read_bytes(), before)

    def test_partial_requires_exact_original_members_even_with_payload(self):
        for stored in (False, True):
            with self.subTest(stored=stored):
                self.configure()
                if stored:
                    self.seal()
                self.rows.pop(0)
                self.write_rows()
                with self.assertRaises(BATCH.BatchError):
                    self.partial_input()

    def test_prior_stored_census_can_read_one_member_and_assignment_change_is_refused(self):
        self.seal()
        self.rows = self.rows[:1]
        self.write_rows()
        payload = BATCH.prior_batch_input(self.jobs, self.current, self.current["nodes"], "work",
                                          "att-owner-original", self.prompt)
        self.assertEqual(payload["manifest"], self.manifest)
        with self.assertRaises(BATCH.BatchError):
            BATCH.prior_batch_input(self.jobs, self.current, self.current["nodes"], "work",
                                   "att-owner-original", self.prompt + " changed")
        HISTORY.batch_input_path(self.jobs, self.digest).unlink()
        with self.assertRaises(BATCH.BatchError):
            BATCH.prior_batch_input(self.jobs, self.current, self.current["nodes"], "work",
                                   "att-owner-original", self.prompt)

    def test_row_binding_and_payload_tamper_are_refused(self):
        self.seal()
        original_rows = copy.deepcopy(self.rows)
        path = HISTORY.batch_input_path(self.jobs, self.digest)
        original_payload = path.read_bytes()
        for field in ("batch_parent_attempt_id", "parent_attempt_id", "batch_leg_sha256",
                      "batch_model_profile", "batch_assignment_sha256", "batch_fallback_ordinal", "route_hash"):
            with self.subTest(field=field):
                self.rows = copy.deepcopy(original_rows)
                self.rows[0][field] = "tampered"
                self.write_rows()
                with self.assertRaises(BATCH.BatchError):
                    self.partial_input()
        self.rows = original_rows
        self.write_rows()
        for field in ("manifest", "prompt", "parent"):
            with self.subTest(payload=field):
                payload = json.loads(original_payload)
                if field == "manifest":
                    payload["manifest"]["members"][0]["perspective"] = "tampered"
                elif field == "prompt":
                    payload["options"]["prompt_text"] = "tampered"
                else:
                    payload["options"]["parent"] = "other-owner"
                path.write_text(json.dumps(payload))
                with self.assertRaises(BATCH.BatchError):
                    self.partial_input()

    def test_no_payload_rejects_source_route_tamper_and_foreign_assignment(self):
        self.route_file.write_text(json.dumps(self.current))
        with self.assertRaises(BATCH.BatchError):
            self.partial_input()
        self.route_file.write_text(json.dumps(self.route))
        with self.assertRaises(BATCH.BatchError):
            BATCH.prior_batch_input(self.jobs, self.current, self.current["nodes"], "work",
                                   "att-owner-original", self.prompt + " changed")

    def test_partial_main_new_owner_starts_only_gap_with_and_without_payload(self):
        spec = importlib.util.spec_from_file_location("history_main_fixture", Path(__file__).with_name("dispatch-batch.test.py"))
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        for stored in (False, True):
            with self.subTest(stored=stored):
                fixture = fixture_module.DispatchBatchTest("runTest")
                fixture.setUp()
                try:
                    # The fixture drives dispatch-batch.main and asserts one
                    # gap spawn, unchanged source manifest, successful peer
                    # reuse and a reservation under the current owner.
                    fixture._partial_continuation_launch(
                        sealed=stored, parent_attempt="att-new-owner",
                        retry_prompt="Continue the same authorized gap.")
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
