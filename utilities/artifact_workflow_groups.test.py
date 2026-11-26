#!/usr/bin/env python3
"""Focused producer workflow-group contract tests."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import artifact_producer as P  # noqa: E402
import artifact_workflow_groups as W  # noqa: E402

spec = importlib.util.spec_from_file_location("workflow_groups_producer_fixture",
                                              Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class WorkflowGroupsTest(fixture.ProducerTestBase):
    def test_coverage_only_evidence_is_unsupported_before_candidate_bytes_are_read(self):
        import artifact_directory_bundle as bundles

        cycle = self.root / "campaigns" / "camp_fixture" / "cycles" / "cyc_fixture"
        raw = cycle / "artifacts" / "raw-results"
        raw.mkdir(parents=True)
        (raw / "evidence.md").write_bytes(b"covered")
        tree = bundles.scan_tree(cycle, "artifacts/raw-results")
        descriptor_rel = "artifacts/_internal/directory-bundles/raw-results.json"
        descriptor = cycle / descriptor_rel
        descriptor.parent.mkdir(parents=True)
        descriptor.write_bytes(tree.descriptor)
        bundle_id = "art_" + "1" * 32
        revision = {"artifact_id": bundle_id, "artifact_revision_id": "arev_" + "2" * 32,
                    "media_type": bundles.MEDIA_TYPE, "locator": {"path": descriptor_rel}}
        document = {"artifacts": [{"artifact_id": bundle_id, "type": "directory-bundle"}],
                    "artifact_revisions": [revision]}
        endpoints = (("cyc_fixture", cycle), ("cyc_other", cycle))
        evidence_path = "campaigns/camp_fixture/cycles/cyc_fixture/artifacts/raw-results/evidence.md"
        with mock.patch.object(W, "_manifest", return_value=document), \
             mock.patch.object(W, "_evidence_bytes", side_effect=AssertionError("candidate bytes read")):
            with self.assertRaises(W.WorkflowGroupError) as caught:
                W._bind_evidence(self.root, {"campaign_id": "camp_fixture"}, {"path": evidence_path}, endpoints)
        self.assertEqual(caught.exception.code, "evidence-coverage-leaf-unsupported")

    def _cycles(self, count=3):
        self.activate()
        rows = []
        for index in range(count):
            route, route_file = self.route(slug=f"workflow-{index}", campaign_key="workflow-test")
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                            intensity="direct", campaign_key="workflow-test")
            payload = self.write_output(begun, f"plans/workflow-{index}.md",
                                        f"cycle {index}\n".encode())
            relative = payload.relative_to(self.root).as_posix()
            artifact_id = "art_" + f"{index + 1:032x}"
            revision_id = "arev_" + f"{index + 1:032x}"
            digest = W._digest(payload.read_bytes())
            interim = {
                "artifact_root_id": fixture.ROOT_ID,
                "repository_id": fixture.REPO_ID,
                "campaign": {"campaign_id": begun["campaign_id"]},
                "cycle": {"cycle_id": begun["cycle_id"], "campaign_id": begun["campaign_id"], "state": "open"},
                "artifacts": [{"artifact_id": artifact_id, "cycle_id": begun["cycle_id"]}],
                "artifact_revisions": [{"artifact_id": artifact_id, "artifact_revision_id": revision_id,
                                        "content_digest": digest,
                                        "locator": {"kind": "cycle-relative", "path": f"artifacts/plans/workflow-{index}.md"}}],
            }
            manifest_path = P.producer_dir(self.root) / "open-manifests" / f"{begun['cycle_id']}.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(interim), encoding="utf-8")
            rows.append({"id": begun["cycle_id"], "campaign": begun["campaign_id"],
                         "path": relative, "file": payload, "manifest": manifest_path})
        return rows

    @staticmethod
    def _proposal(rows, relations=None, title="Test workflow", group_id=None):
        group = {"title": title,
                 "members": [{"cycle_id": row["id"], "stage_label": f"Stage {index}"}
                             for index, row in enumerate(rows)],
                 "relations": relations or []}
        if group_id:
            group["group_id"] = group_id
        return {"groups": [group]}

    @staticmethod
    def _relation(source, target, kind="precedes"):
        if kind == "parallel" and source["id"] > target["id"]:
            source, target = target, source
        return {"from_cycle_id": source["id"], "to_cycle_id": target["id"],
                "kind": kind, "rationale": "The second cycle uses the first cycle's evidence.",
                "evidence_refs": [{"path": source["path"]}, {"path": target["path"]}]}

    def test_prepare_apply_idempotence_and_historical_staleness(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        protected = [P.campaign_dir(self.root, camp) / "campaign.json",
                     *[Path(row["file"]).parent.parent.parent / ".cycle.json" for row in rows],
                     *[row["manifest"] for row in rows]]
        before = {path: path.read_bytes() for path in protected}
        proposal = self._proposal(rows, [self._relation(rows[0], rows[1])])
        plan = W.prepare(self.root, camp, proposal)
        declaration = W.declaration_path(self.root, camp)
        self.assertFalse(declaration.exists())
        self.assertEqual(W.apply(self.root, plan)["status"], "applied")
        self.assertEqual(W.apply(self.root, plan)["status"], "already-applied")
        self.assertEqual(W.verify(self.root, camp)["stale_evidence"], [])
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        rows[0]["file"].write_text("changed\n", encoding="utf-8")
        verified = W.verify(self.root, camp)
        self.assertEqual(verified["stale_evidence"], [rows[0]["path"]])
        self.assertEqual(verified["groups"], 1)

    def _locked(self):
        import artifact_admission as adm
        outer = self

        class Lock:
            def __enter__(self):
                self.fd = adm._acquire_lock(outer.root.resolve(), 5)

            def __exit__(self, *exc):
                adm._release_lock(outer.root.resolve(), self.fd)

        return Lock()

    def test_held_lock_helper_runs_every_check_but_never_writes_and_needs_the_lock(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        plan = W.prepare(self.root, camp, self._proposal(rows, [self._relation(rows[0], rows[1])]))
        declaration = W.declaration_path(self.root, camp)
        with self.assertRaises(W.WorkflowGroupError) as caught:  # a caller without the lock is refused
            W._validated_apply_locked(self.root, plan)
        self.assertEqual(caught.exception.code, "admission-lock-required")
        with self._locked():
            ready = W._validated_apply_locked(self.root, plan)
        self.assertFalse(declaration.exists())  # the helper stops right before the replacement
        self.assertEqual((ready["status"], ready["path"], ready["before_raw"]), ("ready", declaration, None))
        self.assertEqual(ready["sha256"], plan["after_sha256"])
        # the public apply does exactly the helper's replacement, so both paths write the same bytes
        self.assertEqual(W.apply(self.root, plan)["status"], "applied")
        self.assertEqual(declaration.read_bytes(), ready["after_raw"])
        with self._locked():
            again = W._validated_apply_locked(self.root, plan)
        self.assertEqual((again["status"], again["before_raw"], again["after_raw"]),
                         ("already-applied", declaration.read_bytes(), declaration.read_bytes()))
        self.assertEqual(W.apply(self.root, plan)["status"], "already-applied")  # idempotence is unchanged

    def test_held_lock_helper_keeps_the_preimage_and_evidence_checks(self):
        rows = self._cycles(4)
        camp = rows[0]["campaign"]
        first = W.prepare(self.root, camp, self._proposal(rows[:2], [self._relation(rows[0], rows[1])]))
        stale = W.prepare(self.root, camp, self._proposal(rows[:2], [self._relation(rows[0], rows[1])], title="Other"))
        W.apply(self.root, first)
        with self._locked(), self.assertRaises(W.WorkflowGroupError) as caught:
            W._validated_apply_locked(self.root, stale)  # prepared before another writer landed
        self.assertEqual(caught.exception.code, "declaration-preimage-conflict")
        merge = W.prepare(self.root, camp, self._proposal(rows[2:], [self._relation(rows[2], rows[3])],
                                                          title="Second"))
        rows[2]["file"].write_text("changed after prepare\n", encoding="utf-8")  # new evidence moved on
        with self._locked(), self.assertRaises(W.WorkflowGroupError) as caught:
            W._validated_apply_locked(self.root, merge)
        self.assertIn(caught.exception.code, {"evidence-not-current-manifest", "evidence-binding-stale"})
        self.assertEqual(len(W._load_existing(W.declaration_path(self.root, camp))["groups"]), 1)

    def test_merge_preserves_existing_and_stale_plan_conflicts(self):
        rows = self._cycles(3)
        camp = rows[0]["campaign"]
        first = W.prepare(self.root, camp, self._proposal(rows[:2], [self._relation(rows[0], rows[1])]))
        W.apply(self.root, first)
        gid = first["document"]["groups"][0]["group_id"]
        second_proposal = self._proposal([rows[2]], title="Another workflow")
        second = W.prepare(self.root, camp, second_proposal)
        W.apply(self.root, second)
        groups = W._load_existing(W.declaration_path(self.root, camp))["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["group_id"], gid)
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, first)
        self.assertEqual(caught.exception.code, "declaration-preimage-conflict")

    def test_merge_plan_cannot_silently_remove_existing_relation(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        first = W.prepare(self.root, camp, self._proposal(rows, [self._relation(rows[0], rows[1])]))
        W.apply(self.root, first)
        unsafe = W.prepare(self.root, camp, {"groups": []})
        unsafe["document"]["groups"][0]["relations"] = []
        unsafe["after_sha256"] = W._digest(W._bytes(unsafe["document"]))
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, unsafe)
        self.assertEqual(caught.exception.code, "merge-removal-forbidden")
        self.assertEqual(len(W._load_existing(W.declaration_path(self.root, camp))["groups"][0]["relations"]), 1)

    def test_indirect_parallel_and_direction_cycle_refused(self):
        rows = self._cycles(3)
        relations = [self._relation(rows[0], rows[1]), self._relation(rows[1], rows[2]),
                     self._relation(rows[0], rows[2], "parallel")]
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, relations))
        self.assertEqual(caught.exception.code, "parallel-direction-conflict")
        relations[-1] = self._relation(rows[2], rows[0])
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, relations))
        self.assertEqual(caught.exception.code, "relation-cycle")

    def test_shared_evidence_is_read_once_within_total_budget(self):
        rows = self._cycles(3)
        first = self._relation(rows[0], rows[1])
        second = self._relation(rows[0], rows[2])
        first["evidence_refs"] = [{"path": rows[0]["path"]}]
        second["evidence_refs"] = [{"path": rows[0]["path"]}]
        with mock.patch.object(W, "MAX_EVIDENCE_TOTAL", rows[0]["file"].stat().st_size):
            with mock.patch.object(W, "_bind_evidence", wraps=W._bind_evidence) as bound:
                plan = W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, [first, second]))
                self.assertEqual(bound.call_count, 1)
                W.apply(self.root, plan)
                self.assertEqual(bound.call_count, 3)

    def test_same_size_evidence_race_refuses_apply_without_metadata_write(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        plan = W.prepare(self.root, camp, self._proposal(rows, [self._relation(rows[0], rows[1])]))
        original_size_check = W._evidence_size
        calls = 0

        def mutate_after_size_check(root, refs):
            nonlocal calls
            size = original_size_check(root, refs)
            calls += 1
            if calls == 2:
                original = rows[0]["file"].read_bytes()
                rows[0]["file"].write_bytes(b"X" * len(original))
            return size

        with mock.patch.object(W, "_evidence_size", side_effect=mutate_after_size_check):
            with self.assertRaises(W.WorkflowGroupError) as caught:
                W.apply(self.root, plan)
        self.assertEqual(caught.exception.code, "evidence-not-current-manifest")
        self.assertFalse(W.declaration_path(self.root, camp).exists())

    def test_unrelated_merge_keeps_historical_stale_evidence(self):
        rows = self._cycles(4)
        camp = rows[0]["campaign"]
        first = W.prepare(self.root, camp, self._proposal(rows[:2], [self._relation(rows[0], rows[1])]))
        W.apply(self.root, first)
        original = rows[0]["file"].read_bytes()
        rows[0]["file"].write_bytes(b"X" * len(original))
        second = W.prepare(self.root, camp, self._proposal(rows[2:],
                           [self._relation(rows[2], rows[3])], title="Another workflow"))
        self.assertEqual(W.apply(self.root, second)["status"], "applied")
        self.assertEqual(W.verify(self.root, camp)["stale_evidence"], [rows[0]["path"]])

    def test_missing_and_outside_evidence_refused_before_write(self):
        rows = self._cycles(2)
        relation = self._relation(rows[0], rows[1])
        relation["evidence_refs"][0]["path"] = "campaigns/../../escape.md"
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, [relation]))
        self.assertEqual(caught.exception.code, "evidence-path-invalid")
        relation["evidence_refs"][0]["path"] = rows[0]["path"] + ".missing"
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, [relation]))
        self.assertEqual(caught.exception.code, "file-missing")
        self.assertFalse(W.declaration_path(self.root, rows[0]["campaign"]).exists())

    def test_begin_inherits_only_explicit_same_campaign_group_context(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        plan = W.prepare(self.root, camp, self._proposal(rows, [self._relation(rows[0], rows[1])]))
        W.apply(self.root, plan)
        gid = plan["document"]["groups"][0]["group_id"]
        source_record = P.read_cycle_record(self.root, rows[1]["id"])
        inherited = P._env_for(self.root, source_record)
        self.assertEqual(inherited["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], gid)
        route, route_file = self.route(slug="workflow-continued", campaign_key="workflow-test")
        with mock.patch.dict(os.environ, inherited):
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                            intensity="direct", campaign_key="workflow-test")
        self.assertEqual(begun["env"]["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], gid)
        doc = W._load_existing(W.declaration_path(self.root, camp))
        self.assertEqual(len(doc["groups"][0]["members"]), 3)
        route, route_file = self.route(slug="workflow-independent", campaign_key="workflow-test",
                                      parent_cycle_id=rows[0]["id"])
        ungrouped = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                            intensity="direct", campaign_key="workflow-test",
                            parent_cycle_id=rows[0]["id"])
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", ungrouped["env"])
        doc = W._load_existing(W.declaration_path(self.root, camp))
        self.assertEqual(len(doc["groups"][0]["members"]), 3)

    def test_full_group_refuses_begin_before_cycle_or_index_write(self):
        rows = self._cycles(64)
        camp = rows[0]["campaign"]
        plan = W.prepare(self.root, camp, self._proposal(rows))
        W.apply(self.root, plan)
        gid = plan["document"]["groups"][0]["group_id"]
        campaign_dir = P.campaign_dir(self.root, camp)
        protected = [campaign_dir / "campaign.json", campaign_dir / W.NAME,
                     self.root / "campaigns" / "INDEX.json"]
        before = {path: path.read_bytes() for path in protected}
        before_dirs = {path.name for path in campaign_dir.iterdir()}
        before_records = {path.name for path in (P.producer_dir(self.root) / "cycles").iterdir()}
        route, route_file = self.route(slug="workflow-overflow", campaign_key="workflow-test")
        with self.assertRaises(P.ProducerError) as caught:
            P.begin(self.root, route_file=route_file, capability="autopilot-code",
                    intensity="direct", campaign_key="workflow-test", workflow_group_id=gid)
        self.assertEqual(caught.exception.code, "member-count-invalid")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertEqual({path.name for path in campaign_dir.iterdir()}, before_dirs)
        self.assertEqual({path.name for path in (P.producer_dir(self.root) / "cycles").iterdir()}, before_records)

    def test_replace_withdraws_group_and_new_evidence_drift_blocks_apply(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        proposal = self._proposal(rows, [self._relation(rows[0], rows[1])])
        plan = W.prepare(self.root, camp, proposal)
        rows[0]["file"].write_text("changed before apply\n", encoding="utf-8")
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, plan)
        self.assertEqual(caught.exception.code, "evidence-not-current-manifest")
        self.assertFalse(W.declaration_path(self.root, camp).exists())
        rows[0]["file"].write_text("cycle 0\n", encoding="utf-8")
        W.apply(self.root, plan)
        withdrawn = W.prepare(self.root, camp, {"groups": []}, replace=True)
        self.assertEqual(W.apply(self.root, withdrawn)["status"], "applied")
        self.assertEqual(W.verify(self.root, camp)["groups"], 0)

    def _end_empty(self, row):
        """End a cycle with no durable output, through the real finalize (`_remove_empty_cycle`)."""
        record = P.read_cycle_record(self.root, row["id"])
        directory = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)
        row["file"].unlink()
        (directory / "artifacts" / "plans").rmdir()
        result = P.finalize(self.root, cycle_id=row["id"], state="abandoned", abandon_reason="route-unrecoverable")
        self.assertEqual(result["status"], "no-lineage")

    def _declare(self, camp, *proposals):
        for title, members, relations in proposals:
            plan = W.prepare(self.root, camp, self._proposal(members, relations, title=title))
            W.apply(self.root, plan)
        return W._load_existing(W.declaration_path(self.root, camp))

    def test_withdrawal_removes_member_relations_and_empty_group(self):
        rows = self._cycles(6)
        camp = rows[0]["campaign"]
        declared = self._declare(
            camp,
            ("Chain", rows[:3], [self._relation(rows[0], rows[1]), self._relation(rows[1], rows[2])]),
            ("Single", rows[3:4], []),
            ("Solo", rows[4:5], []),
            ("Pair", rows[5:6], []))
        untouched = [W._bytes(group) for group in declared["groups"] if group["title"] in ("Single", "Pair")]
        self._end_empty(rows[1])
        self._end_empty(rows[4])
        plan = W.prepare_withdrawal(self.root, camp, [rows[1]["id"], rows[4]["id"], "cyc_" + "0" * 32])
        self.assertEqual(plan["mode"], "replace")
        self.assertEqual(plan["new_evidence"], [])
        self.assertEqual(W.apply(self.root, plan)["status"], "applied")
        self.assertEqual(W.verify(self.root, camp, expected=plan["after_sha256"])["groups"], 3)
        after = W._load_existing(W.declaration_path(self.root, camp))
        titles = [group["title"] for group in after["groups"]]
        self.assertEqual(titles, ["Chain", "Single", "Pair"])
        chain = after["groups"][0]
        self.assertEqual([item["cycle_id"] for item in chain["members"]], [rows[0]["id"], rows[2]["id"]])
        self.assertEqual(chain["relations"], [])
        self.assertEqual([W._bytes(group) for group in after["groups"][1:]], untouched)
        self.assertEqual(W.declaration_path(self.root, camp).read_bytes(), W._bytes(plan["document"]))

    def test_withdrawal_ignores_stale_historical_evidence(self):
        rows = self._cycles(4)
        camp = rows[0]["campaign"]
        self._declare(camp, ("Kept", rows[:2], [self._relation(rows[0], rows[1])]),
                      ("Leaving", rows[2:], [self._relation(rows[2], rows[3])]))
        kept = W._load_existing(W.declaration_path(self.root, camp))["groups"][0]
        rows[0]["file"].write_bytes(b"X" * len(rows[0]["file"].read_bytes()))
        self._end_empty(rows[2])
        self._end_empty(rows[3])
        plan = W.prepare_withdrawal(self.root, camp, [rows[2]["id"], rows[3]["id"]])
        self.assertEqual(plan["new_evidence"], [])
        self.assertEqual(W.apply(self.root, plan)["status"], "applied")
        after = W._load_existing(W.declaration_path(self.root, camp))
        self.assertEqual(W._bytes(after["groups"][0]), W._bytes(kept))
        self.assertEqual(len(after["groups"]), 1)
        self.assertEqual(W.verify(self.root, camp)["stale_evidence"], [rows[0]["path"]])

    def test_withdrawal_from_already_invalid_declaration(self):
        rows = self._cycles(3)
        camp = rows[0]["campaign"]
        self._declare(camp, ("Chain", rows, [self._relation(rows[0], rows[1]), self._relation(rows[1], rows[2])]))
        self._end_empty(rows[1])
        # The cycle left the campaign, so the declaration on disk no longer validates.
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.verify(self.root, camp)
        self.assertEqual(caught.exception.code, "cycle-not-member")
        with self.assertRaises(W.WorkflowGroupError):
            W.prepare(self.root, camp, {"groups": []})
        plan = W.prepare_withdrawal(self.root, camp, [rows[1]["id"]])
        self.assertEqual(W.apply(self.root, plan)["status"], "applied")
        self.assertEqual(W.verify(self.root, camp, expected=plan["after_sha256"])["groups"], 1)
        after = W._load_existing(W.declaration_path(self.root, camp))
        self.assertEqual([item["cycle_id"] for item in after["groups"][0]["members"]], [rows[0]["id"], rows[2]["id"]])

    def test_withdrawal_noop_and_conflicts(self):
        rows = self._cycles(3)
        camp = rows[0]["campaign"]
        self.assertIsNone(W.prepare_withdrawal(self.root, camp, [rows[0]["id"]]))  # no declaration
        self._declare(camp, ("Chain", rows[:2], [self._relation(rows[0], rows[1])]))
        self.assertIsNone(W.prepare_withdrawal(self.root, camp, [rows[2]["id"]]))  # not a member
        path = W.declaration_path(self.root, camp)
        original = path.read_bytes()
        plan = W.prepare_withdrawal(self.root, camp, [rows[1]["id"]])
        self.assertEqual(path.read_bytes(), original)  # prepare is read-only
        lock = P.artifact_admission._acquire_lock(self.root, 5)
        try:
            with self.assertRaises(P.artifact_admission.AdmissionBusy):
                W.apply(self.root, plan, lock_timeout=0)
        finally:
            P.artifact_admission._release_lock(self.root, lock)
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(original + b"\n")
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, plan, lock_timeout=0)
        self.assertEqual(caught.exception.code, "declaration-preimage-conflict")
        self.assertEqual(path.read_bytes(), original + b"\n")

    def test_duplicate_json_key_is_rejected(self):
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W._json(b'{"groups":[],"groups":[]}')
        self.assertEqual(caught.exception.code, "json-duplicate-key")


if __name__ == "__main__":
    unittest.main()
