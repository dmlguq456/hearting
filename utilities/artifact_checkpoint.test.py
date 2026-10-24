#!/usr/bin/env python3
"""Open-cycle checkpoint: the interim manifest, its ID continuity into
`finalize`, its gates, and the automatic trigger launcher."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import artifact_cycle_titles as CT
import artifact_checkpoint_trigger as T
import artifact_manifest as m
import artifact_producer as P

spec = importlib.util.spec_from_file_location(
    "checkpoint_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def _ids_by_path(document):
    artifacts = {row["artifact_id"]: row for row in document["artifacts"]}
    return {
        row["locator"]["path"]: (row["artifact_id"], row["artifact_revision_id"], row["content_digest"],
                                 artifacts[row["artifact_id"]]["role"])
        for row in document["artifact_revisions"]
    }


class CheckpointTestBase(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {P.CHECKPOINT_INTERVAL_ENV: "0"})
        patch.start()
        self.addCleanup(patch.stop)
        # The route seals this fixture AGENT_HOME as its runtime release; mark it
        # as a release that keeps interim IDs at the seal.
        self.marker = Path(os.environ["AGENT_HOME"]) / P.INTERIM_SUPPORT_MARKER
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("# fixture\n", encoding="utf-8")
        self.activate()
        self.route_obj, self.route_file, self.result = self.begin()
        self.cycle_id = self.result["cycle_id"]

    def interim(self):
        return json.loads(P.open_manifest_path(self.root, self.cycle_id).read_text(encoding="utf-8"))

    def ledger(self):
        return json.loads(P.reservation_path(self.root, self.cycle_id).read_text(encoding="utf-8"))

    def seal(self, **kwargs):
        self.close(self.route_obj, self.route_file)
        sealed = P.finalize(self.root, cycle_id=self.cycle_id, **kwargs)
        return sealed, json.loads((Path(self.result["cycle_dir"]) / "manifest.json").read_text())

    def state(self):
        return json.loads(P.checkpoint_state_path(self.root, self.cycle_id).read_text(encoding="utf-8"))


class InterimManifestTest(CheckpointTestBase):
    def test_checkpoint_prunes_each_marked_payload_tree_before_limits(self):
        cycle = Path(self.result["cycle_dir"])
        cache = cycle / "artifacts" / "experiments" / "cache"
        cache.mkdir(parents=True)
        (cache / "CACHEDIR.TAG").write_bytes(P.artifact_payload.CACHEDIR_SIGNATURE)
        for index in range(8):
            (cache / f"large-{index}.bin").write_bytes(b"x" * 128)
        env = cycle / "_internal" / "venv"
        env.mkdir(parents=True)
        (env / "pyvenv.cfg").write_bytes(b"")
        (env / "large.bin").write_bytes(b"y" * 128)
        report = self.write_output(self.result, "plans/report.md", b"report")

        scan = P._checkpoint_scan(cycle, {}, P.CheckpointLimits(
            max_walk_entries=1, max_files=1, max_file_bytes=32, max_total_bytes=32,
        ))
        self.assertNotIn("skip", scan)
        self.assertEqual([row[0] for row in scan["facts"]], ["artifacts/plans/report.md"])
        self.assertEqual(scan["excluded"]["self-excluded"], 2)
        self.assertEqual(report.read_bytes(), b"report")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id,
                              limits=P.CheckpointLimits(max_walk_entries=1, max_files=1,
                                                        max_file_bytes=32, max_total_bytes=32))
        self.assertEqual(result["status"], "emitted", result)
        self.assertEqual(set(_ids_by_path(self.interim())), {"artifacts/plans/report.md"})

    def test_checkpoint_uses_structural_runtime_signals_and_interim_weight_suffixes(self):
        cycle = Path(self.result["cycle_dir"])
        checkout = cycle / "artifacts/dev_logs/runtime/external-source"
        (checkout / ".git/objects").mkdir(parents=True)
        (checkout / ".git/objects/pack").write_bytes(b"ignored")
        self.write_output(self.result, "dev_logs/model.nemo", b"nemo")
        self.write_output(self.result, "dev_logs/model.gguf", b"gguf")
        self.write_output(self.result, "reports/summary.md", b"report")
        scan = P._checkpoint_scan(cycle, {}, P.CheckpointLimits())
        self.assertEqual([row[0] for row in scan["facts"]], ["artifacts/reports/summary.md"])
        self.assertEqual(scan["excluded_roots"], ["artifacts/dev_logs/runtime/external-source"])
        self.assertEqual(scan["excluded"]["binary"], 2)

    def test_checkpoint_prunes_raw_results_before_walk_and_byte_limits(self):
        cycle = Path(self.result["cycle_dir"])
        raw = cycle / "artifacts" / "raw-results"
        raw.mkdir(parents=True)
        for index in range(25):
            (raw / f"f{index:02d}.bin").write_bytes(b"x" * 8)
        report = self.write_output(self.result, "reports/final_report.md", b"report")
        scan = P._checkpoint_scan(cycle, {}, P.CheckpointLimits(
            max_walk_entries=1, max_files=1, max_file_bytes=100, max_total_bytes=100,
        ))
        self.assertNotIn("skip", scan)
        self.assertEqual([row[0] for row in scan["facts"]], ["artifacts/reports/final_report.md"])
        self.assertEqual(report.read_bytes(), b"report")
        self.assertEqual(sorted(p.name for p in raw.iterdir()), [f"f{index:02d}.bin" for index in range(25)])

    def test_pruned_raw_reservation_is_kept_and_exact_primary_id_continues(self):
        cycle = Path(self.result["cycle_dir"])
        raw = self.write_output(self.result, "raw-results/evidence.md", b"evidence")
        report = self.write_output(self.result, "reports/final_report.md", b"report")
        record = P.read_cycle_record(self.root, self.cycle_id)
        facts = [(raw.relative_to(cycle).as_posix(), "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest(), raw.stat().st_size),
                 (report.relative_to(cycle).as_posix(), "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest(), report.stat().st_size)]
        reserved_doc = P.build_manifest(
            self.root, record, self.route_obj, (), state="completed", primary=None,
            allow_open_route=True, allocator=P.artifact_identity.IdAllocator(), now=1,
            interim=True, facts=facts,
        )
        raw_row = next(row for row in reserved_doc["artifact_revisions"]
                       if row["locator"]["path"] == "artifacts/raw-results/evidence.md")
        P.reservation_path(self.root, self.cycle_id).parent.mkdir(parents=True, exist_ok=True)
        P.reservation_path(self.root, self.cycle_id).write_bytes(
            P._json_bytes(P._reservation_payload(record, reserved_doc, None)))
        P.open_manifest_path(self.root, self.cycle_id).parent.mkdir(parents=True, exist_ok=True)
        P.open_manifest_path(self.root, self.cycle_id).write_bytes(m.canonical_bytes(reserved_doc))

        checkpoint = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(checkpoint["status"], "emitted", checkpoint)
        interim = self.interim()
        self.assertNotIn("artifacts/raw-results/evidence.md", _ids_by_path(interim))
        ledger = self.ledger()
        self.assertEqual(ledger["artifacts"]["artifacts/raw-results/evidence.md"]["artifact_id"],
                         raw_row["artifact_id"])
        self.close(self.route_obj, self.route_file)
        P.finalize(self.root, cycle_id=self.cycle_id, primary="artifacts/raw-results/evidence.md")
        sealed = json.loads((cycle / "manifest.json").read_text(encoding="utf-8"))
        raw_sealed = next(row for row in sealed["artifact_revisions"]
                          if row["locator"]["path"] == "artifacts/raw-results/evidence.md")
        self.assertEqual(raw_sealed["artifact_id"], raw_row["artifact_id"])

    def legacy_move(self, rel, to):
        """What a release before the move's removal left: the file at `to` and its ledger row."""
        cycle = Path(self.result["cycle_dir"])
        source, target = cycle / "artifacts" / rel, cycle / "artifacts" / to
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        ledger = P.producer_dir(self.root) / "bucket-placements" / f"{self.cycle_id}.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"moves": [{"from": f"artifacts/{rel}", "to": f"artifacts/{to}"}]}))
        return target

    def test_loose_report_tree_stays_where_it_was_written_and_html_is_primary(self):
        files = {
            "report/report.html": b'<img src="../field/img1.png"><audio src="audio/a.wav">',
            "report/audio/a.wav": b"audio",
            "field/img1.png": b"image",
            "request.md": b"request",
            "make_report.log": b"log",
        }
        for path, data in files.items():
            self.write_output(self.result, path, data)
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "emitted", result)
        self.assertNotIn("moved_outputs", result)
        output = Path(self.result["cycle_dir"]) / "artifacts"
        for path, data in files.items():
            self.assertEqual((output / path).read_bytes(), data)
        self.assertFalse((output / "plans").exists())
        self.assertFalse((P.producer_dir(self.root) / "bucket-placements" / f"{self.cycle_id}.json").exists())
        primary = [path for path, fields in _ids_by_path(self.interim()).items() if fields[-1] == "primary"]
        self.assertEqual(primary, ["artifacts/report/report.html"])
        again = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(again["status"], "unchanged")
        self.assertNotIn("moved_outputs", again)
        _, sealed = self.seal(primary="report/report.html")
        self.assertEqual([path for path, fields in _ids_by_path(sealed).items() if fields[-1] == "primary"], primary)

    def test_html_assets_and_utf8_names_survive_checkpoint_and_seal(self):
        paths = {
            "analysis/report/report.html": b'<img src="field_0928/fig/image.png"><audio src="field_0928/audio/sample.wav">',
            "analysis/report/field_0928/fig/image.png": b"image",
            "analysis/report/field_0928/audio/sample.wav": b"audio",
            "analysis/report/field_0928/fig/공장잡음1.png": b"unicode image",
            "analysis/report/field_0928/audio/무잡음.wav": b"unicode audio",
            "analysis/report/field_0928/fig/測定📈.png": b"unicode figure",
        }
        for path, data in paths.items():
            self.write_output(self.result, path, data)
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "emitted", result)
        expected = {"artifacts/" + path for path in paths}
        self.assertEqual(set(_ids_by_path(self.interim())), expected)
        self.assertEqual(sum(result["excluded"].values()), 0)
        before = _ids_by_path(self.interim())
        _, sealed = self.seal()
        self.assertEqual(_ids_by_path(sealed), before)
        for path, data in paths.items():
            self.assertEqual((Path(self.result["cycle_dir"]) / "artifacts" / path).read_bytes(), data)

    def test_unrepresentable_name_reports_exclusion_reason_and_count(self):
        self.write_output(self.result, "analysis/report/report.html", b"report")
        self.write_output(self.result, "analysis/report/fig/bad\nname.png", b"image")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "emitted", result)
        self.assertEqual(result["artifact_count"], 1)
        self.assertEqual(result["excluded"]["invalid-locator"], 1)
        self.assertEqual(result["excluded"]["invalid-locator-reasons"],
                         {"locator-control-char": 1})
        self.assertEqual(self.state()["excluded"], result["excluded"])

    def test_finalize_keeps_loose_output_in_place(self):
        written = self.write_output(self.result, "code_change.md", b"changes")
        result, document = self.seal(primary="code_change.md")
        self.assertNotIn("moved_outputs", result)
        self.assertIn("artifacts/code_change.md", _ids_by_path(document))
        self.assertTrue(written.is_file())

    def test_default_bucket_mapping_and_compose_notice(self):
        self.assertEqual(P.default_bucket("autopilot-lab"), "experiments")
        self.assertEqual(P.default_bucket("analysis"), "analysis")
        self.assertEqual(P.default_bucket("analyze-project"), "analysis_project")
        card = F.R.compose_card(self.route_obj)
        folder = Path(self.result["cycle_dir"]) / "artifacts/plans"
        self.assertIn("산출물 " + str(folder), card)

    def test_completion_evidence_remains_readable_after_recorded_move(self):
        evidence = self.write_output(self.result, "report/report.html", b"completed report")
        node = next(node for node in self.route_obj["nodes"] if node.get("terminal"))
        F.R.write_completion_marker(self.route_obj, node, node["id"], evidence)
        self.legacy_move("report/report.html", "plans/report/report.html")
        self.assertFalse(evidence.exists())
        self.assertTrue(F.R.terminal_gate_observation(self.route_obj)[node["id"]]["passed"])
        mapped = P.resolve_placed_output(evidence)
        self.assertEqual(mapped.read_bytes(), b"completed report")
        mapped.write_bytes(b"changed after completion")
        with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}):
            self.assertFalse(F.R.terminal_gate_observation(self.route_obj)[node["id"]]["passed"])

    def test_reviewed_plan_evidence_follows_recorded_move(self):
        # plan-check launch read the plan marker's original path and was refused with
        # reviewed-evidence-producer-unproven once an earlier release had placed the plan.
        import review_input as RI
        evidence = self.write_output(self.result, "plan.md", b"exact plan\n")
        self.legacy_move("plan.md", "plans/plan.md")
        self.assertFalse(evidence.exists())
        found = RI._file(str(evidence))
        self.assertEqual(Path(found["path"]), P.resolve_placed_output(evidence).resolve())
        self.assertEqual(found["sha256"], hashlib.sha256(b"exact plan\n").hexdigest())

    def test_owner_envelope_primary_follows_recorded_move(self):
        # The owner envelope used to look only at the marker's original evidence path,
        # so every closure whose report an earlier release had placed stayed closure-pending
        # with recovery-unavailable/material-primary-required.
        import dispatch_terminal_commit as TC
        evidence = self.write_output(self.result, "final_report.md", b"final report\n")
        node = next(node for node in self.route_obj["nodes"] if node.get("terminal"))
        F.R.write_completion_marker(self.route_obj, node, node["id"], evidence)
        self.legacy_move("final_report.md", "plans/final_report.md")
        self.assertFalse(evidence.exists())
        gates = {node["id"]: {"evidence": str(evidence)}}
        route = dict(self.route_obj, workflow_contract={"terminal_nodes": [node["id"]]})
        primary = TC.select_primary_artifact(route, gates, {}, artifact_root=self.root)
        self.assertIsNotNone(primary)
        self.assertEqual(primary.read_bytes(), b"final report\n")
        self.assertEqual(primary, P.resolve_placed_output(evidence).resolve())

    def test_placed_output_proof_binds_ledger_manifest_and_current_bytes(self):
        original = self.write_output(self.result, "plan.md", b"exact plan\n")
        self.legacy_move("plan.md", "plans/plan.md")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        expected = dict(route_id=self.route_obj["route_id"], route_hash=self.route_obj["route_hash"])
        proof = P.placed_output_proof(original, **expected)
        self.assertIsNotNone(proof)
        target = Path(proof["destination"])
        self.assertEqual(target.read_bytes(), b"exact plan\n")
        self.assertEqual(proof["current_content_digest"], "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertIsNone(P.placed_output_proof(original, route_id=expected["route_id"], route_hash="sha256:" + "0" * 64))
        target.write_bytes(b"changed after manifest")
        self.assertIsNone(P.placed_output_proof(original, **expected))
        target.write_bytes(b"exact plan\n")
        manifest = P.open_manifest_path(self.root, self.cycle_id)
        original_manifest = manifest.read_bytes()
        changed = json.loads(original_manifest)
        changed["artifact_revisions"][0]["content_digest"] = "sha256:" + "0" * 64
        manifest.write_text(json.dumps(changed), encoding="utf-8")
        self.assertIsNone(P.placed_output_proof(original, **expected))
        manifest.write_bytes(original_manifest)
        ledger = P.producer_dir(self.root) / "bucket-placements" / f"{self.cycle_id}.json"
        original_ledger = ledger.read_bytes()
        ledger.write_text('{"moves":[]}', encoding="utf-8")
        self.assertIsNone(P.placed_output_proof(original, **expected))
        duplicate = json.loads(original_ledger)
        duplicate["moves"].append(dict(duplicate["moves"][0]))
        ledger.write_text(json.dumps(duplicate), encoding="utf-8")
        self.assertIsNone(P.placed_output_proof(original, **expected))
        ledger.write_text("[]", encoding="utf-8")
        self.assertIsNone(P.placed_output_proof(original, **expected))
        ledger.write_bytes(original_ledger)
        original.write_bytes(b"different file at old path")
        self.assertIsNone(P.placed_output_proof(original, **expected))

    def test_internal_migration_snapshot_keeps_its_recorded_paths(self):
        self.write_output(self.result, "shared-input/_internal/migration-shared-bases.json", b"{}")
        self.write_output(self.result, "shared-input/spec/prd.md", b"source snapshot")
        self.write_output(self.result, "report/report.html", b"report")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertNotIn("moved_outputs", result)
        interim = _ids_by_path(self.interim())
        self.assertIn("artifacts/shared-input/spec/prd.md", interim)
        self.assertIn("artifacts/report/report.html", interim)

    def test_document_fallback_precedes_media(self):
        self.write_output(self.result, "plans/a.png", b"image")
        self.write_output(self.result, "plans/b.log", b"log")
        self.write_output(self.result, "plans/c.wav", b"audio")
        self.write_output(self.result, "plans/z/overview.html", b"document")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual([path for path, fields in _ids_by_path(self.interim()).items() if fields[-1] == "primary"],
                         ["artifacts/plans/z/overview.html"])

    def test_emits_the_sealed_schema_with_open_state(self):
        self.write_output(self.result, "plans/cycle/plan.md")
        self.write_output(self.result, "experiments/run/report/index.html", b"<html>epoch 1</html>\n")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "emitted", result)
        self.assertEqual(result["artifact_count"], 2)
        path = P.open_manifest_path(self.root, self.cycle_id)
        self.assertEqual(path, self.root / ".runtime/artifact-producer/v1/open-manifests" / f"{self.cycle_id}.json")
        document = self.interim()
        self.assertEqual(document["cycle"]["state"], "open")
        self.assertTrue(m.validate_interim(document).ok, m.validate_interim(document).violations)
        # `open` is not a sealed state: the sealed validator must keep refusing it.
        self.assertFalse(m.validate(document).ok)
        self.assertEqual(document["cycle"]["cycle_id"], self.cycle_id)
        self.assertEqual(document["routes"][0]["route_id"], self.route_obj["route_id"])
        self.assertEqual({e["event_type"] for e in document["events"]}, {"artifact.revision.recorded"})
        self.assertEqual(set(_ids_by_path(document)),
                         {"artifacts/plans/cycle/plan.md", "artifacts/experiments/run/report/index.html"})
        # Nothing is written into the cycle directory itself.
        self.assertEqual(sorted(os.listdir(self.result["cycle_dir"])), [".cycle.json", "artifacts"])
        self.assertEqual(self.state()["last_status"], "emitted")

    def test_cycle_root_support_checkpoint_and_finalize_preserve_identity_and_bundle_scope(self):
        cycle = Path(self.result["cycle_dir"])
        root_support = cycle / "_internal/reviews/manifest.json"
        artifact_support = cycle / "artifacts/_internal/reviews/manifest.json"
        support_raw = cycle / "_internal/raw-results/summary.json"
        binary_support = cycle / "_internal/reviews/weights.bin"
        oversized_support = cycle / "_internal/reviews/large.md"
        for path, data in (
                (root_support, b'{"version":1}\n'),
                (artifact_support, b'{"artifact-side":true}\n'),
                (support_raw, b'{"files":2}\n'),
                (binary_support, b"binary support")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with oversized_support.open("wb") as stream:
            stream.truncate(P.CheckpointLimits().max_file_bytes + 1)
        self.write_output(self.result, "raw-results/inside.txt", b"bundle bytes\n")
        primary = self.write_output(self.result, "documents/final_report.md", b"report\n")

        emitted = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(emitted["status"], "emitted", emitted)
        interim = self.interim()
        interim_rows = _ids_by_path(interim)
        self.assertIn("_internal/reviews/manifest.json", interim_rows)
        self.assertIn("artifacts/_internal/reviews/manifest.json", interim_rows)
        self.assertIn("_internal/raw-results/summary.json", interim_rows)
        self.assertNotIn("_internal/reviews/weights.bin", interim_rows)
        self.assertNotIn("_internal/reviews/large.md", interim_rows)
        self.assertNotIn("artifacts/raw-results/inside.txt", interim_rows)
        self.assertNotEqual(interim_rows["_internal/reviews/manifest.json"][0],
                            interim_rows["artifacts/_internal/reviews/manifest.json"][0])
        self.assertEqual(interim_rows["_internal/reviews/manifest.json"][3], "support")
        unchanged = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(unchanged["status"], "unchanged", unchanged)
        before = _ids_by_path(self.interim())

        root_support.write_bytes(b'{"version":2,"changed":true}\n')
        sealed, document = self.seal(primary="documents/final_report.md")
        self.assertEqual(sealed["status"], "sealed", sealed)
        sealed_rows = _ids_by_path(document)
        old = before["_internal/reviews/manifest.json"]
        new = sealed_rows["_internal/reviews/manifest.json"]
        self.assertEqual(new[0], old[0])
        self.assertNotEqual(new[1], old[1])
        self.assertEqual(new[2], "sha256:" + hashlib.sha256(root_support.read_bytes()).hexdigest())
        self.assertEqual(new[3], "support")
        self.assertEqual(sealed_rows["artifacts/_internal/reviews/manifest.json"][0],
                         before["artifacts/_internal/reviews/manifest.json"][0])
        self.assertEqual(sealed_rows["_internal/raw-results/summary.json"][3], "support")
        self.assertEqual(sealed_rows["_internal/reviews/weights.bin"][3], "support")
        self.assertEqual(sealed_rows["_internal/reviews/large.md"][3], "support")
        self.assertEqual(sealed_rows["_internal/reviews/large.md"][2],
                         "sha256:" + hashlib.sha256(oversized_support.read_bytes()).hexdigest())
        artifacts = {row["artifact_id"]: row for row in document["artifacts"]}
        bundles = [row for row in document["artifact_revisions"]
                   if artifacts[row["artifact_id"]]["type"] == "directory-bundle"]
        self.assertEqual(len(bundles), 1)
        self.assertEqual(artifacts[bundles[0]["artifact_id"]]["title"], "raw-results")
        self.assertEqual(primary.read_bytes(), b"report\n")
        self.assertEqual(root_support.read_bytes(), b'{"version":2,"changed":true}\n')
        self.assertEqual(P.artifact_lifecycle.verify_artifact_revisions(document, cycle), ())
        self.assertEqual(P._output_placements(self.root, P.read_cycle_record(self.root, self.cycle_id)), [])

    def test_ids_are_stable_and_revisions_follow_content(self):
        self.write_output(self.result, "plans/cycle/plan.md", b"v1\n")
        self.write_output(self.result, "plans/cycle/notes.md", b"notes\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        first = self.interim()
        before = _ids_by_path(first)
        self.write_output(self.result, "plans/cycle/plan.md", b"v2 longer\n")
        self.write_output(self.result, "plans/cycle/new.md", b"new\n")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "emitted", result)
        second = self.interim()
        after = _ids_by_path(second)
        plan, notes = "artifacts/plans/cycle/plan.md", "artifacts/plans/cycle/notes.md"
        self.assertEqual(after[plan][0], before[plan][0])        # same artifact
        self.assertNotEqual(after[plan][1], before[plan][1])     # new revision
        self.assertEqual(after[notes][:2], before[notes][:2])    # untouched file: same revision
        self.assertNotIn(after["artifacts/plans/cycle/new.md"][0], {v[0] for v in before.values()})
        self.assertEqual(second["manifest_id"], first["manifest_id"])
        self.assertNotEqual(second["manifest_revision_id"], first["manifest_revision_id"])

    def test_unchanged_output_is_not_rewritten(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        path = P.open_manifest_path(self.root, self.cycle_id)
        data, mtime = path.read_bytes(), path.stat().st_mtime_ns
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "unchanged", result)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (data, mtime))

    def test_minimum_interval_applies_to_every_trigger(self):
        self.write_output(self.result)
        with mock.patch.dict(os.environ, {P.CHECKPOINT_INTERVAL_ENV: "900"}):
            now = time.time()
            self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id, now=now)["status"], "emitted")
            again = P.checkpoint(self.root, cycle_id=self.cycle_id, now=now + 60)
            self.assertEqual((again["status"], again["reason"]), ("skipped", "min-interval"))
            self.assertIn("next_eligible_at", again)
            later = P.checkpoint(self.root, cycle_id=self.cycle_id, now=now + 901, trigger="turn-end")
            self.assertEqual(later["status"], "unchanged")

    def test_route_argument_selects_the_routes_open_cycle(self):
        self.write_output(self.result)
        result = P.checkpoint(self.root, route_file=self.route_file, trigger="stage-complete")
        self.assertEqual((result["status"], result["cycle_id"]), ("emitted", self.cycle_id))

    def test_only_a_live_cycle_is_published(self):
        self.write_output(self.result)
        self.close(self.route_obj, self.route_file)
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "route-closed"))
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())
        P.finalize(self.root, cycle_id=self.cycle_id)
        # §45 D-124: a closed cycle is not skipped; the checkpoint looks for changed files
        # (none here), never publishes an interim document for it.
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())

    def test_empty_cycle_publishes_nothing(self):
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "no-output"))
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())

    def test_weights_and_oversize_files_are_left_out(self):
        self.write_output(self.result, "experiments/run/report.md", b"report\n")
        self.write_output(self.result, "experiments/run/ckpt/epoch3.pt", b"weights")
        self.write_output(self.result, "experiments/run/big.log", b"x" * 64)
        self.write_output(self.result, "experiments/run/.cache/tmp.json", b"{}")
        limits = P.CheckpointLimits(max_file_bytes=32)
        result = P.checkpoint(self.root, cycle_id=self.cycle_id, limits=limits)
        self.assertEqual(result["status"], "emitted", result)
        self.assertEqual(set(_ids_by_path(self.interim())), {"artifacts/experiments/run/report.md"})
        self.assertEqual((result["excluded"]["binary"], result["excluded"]["oversize"],
                          result["excluded"]["hidden"]), (1, 1, 1))

    def test_limits_skip_with_a_recorded_reason_and_keep_the_last_document(self):
        for i in range(3):
            self.write_output(self.result, f"plans/cycle/n{i}.md", b"n\n")
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "emitted")
        kept = P.open_manifest_path(self.root, self.cycle_id).read_bytes()
        self.write_output(self.result, "plans/cycle/n3.md", b"n\n")
        for limits, reason in ((P.CheckpointLimits(max_files=2), "file-count-limit"),
                               (P.CheckpointLimits(max_total_bytes=5), "byte-size-limit"),
                               (P.CheckpointLimits(max_walk_entries=2), "walk-limit")):
            result = P.checkpoint(self.root, cycle_id=self.cycle_id, limits=limits)
            self.assertEqual((result["status"], result["reason"]), ("skipped", reason))
            self.assertEqual(self.state()["last_reason"], reason)
            self.assertEqual(P.open_manifest_path(self.root, self.cycle_id).read_bytes(), kept)

    def test_automatic_trigger_does_not_start_publishing_a_stale_cycle(self):
        target = self.write_output(self.result)
        old = time.time() - 3 * 86400
        os.utime(target, (old, old))
        result = P.checkpoint(self.root, cycle_id=self.cycle_id, trigger="turn-end")
        self.assertEqual((result["status"], result["reason"]), ("skipped", "stale-cycle"))
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "emitted")

    def test_busy_lock_skips_instead_of_waiting(self):
        self.write_output(self.result)
        with P._checkpoint_lock(self.root, self.cycle_id, timeout=0):
            result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "busy"))

    def test_interim_declares_the_primary_role_requirement(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(self.interim()["cycle"]["outcome_criterion"]["required_artifact_roles"], ["primary"])

    def test_route_sealed_to_an_older_release_publishes_nothing(self):
        self.write_output(self.result)
        self.marker.unlink()
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "route-release-predates-interim"))
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())

    def test_a_checkpoint_superseded_during_its_scan_does_not_overwrite(self):
        self.write_output(self.result, "plans/cycle/a.md", b"a\n")
        real_scan = P._checkpoint_scan
        calls = []

        def scan_with_a_concurrent_checkpoint(*args, **kwargs):
            result = real_scan(*args, **kwargs)
            if not calls:
                calls.append(1)
                self.write_output(self.result, "plans/cycle/b.md", b"b\n")
                inner = P.checkpoint(self.root, cycle_id=self.cycle_id)
                self.assertEqual(inner["status"], "emitted")
            return result

        with mock.patch.object(P, "_checkpoint_scan", side_effect=scan_with_a_concurrent_checkpoint):
            outer = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((outer["status"], outer["reason"]), ("skipped", "superseded"))
        self.assertIn("artifacts/plans/cycle/b.md", _ids_by_path(self.interim()))

    def test_a_torn_manifest_on_an_open_cycle_keeps_the_reservation(self):
        # Review N3: only a cycle that is no longer open loses its interim files.
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        real_scan = P._checkpoint_scan

        def scan_then_tear(*args, **kwargs):
            result = real_scan(*args, **kwargs)
            (Path(self.result["cycle_dir"]) / "manifest.json").write_text("{torn", encoding="utf-8")
            return result

        self.write_output(self.result, "plans/cycle/new.md", b"n\n")
        with mock.patch.object(P, "_checkpoint_scan", side_effect=scan_then_tear):
            result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "sealed-manifest-present"))
        self.assertTrue(P.reservation_path(self.root, self.cycle_id).exists())
        self.assertTrue(P.open_manifest_path(self.root, self.cycle_id).exists())

    def test_an_unreadable_reservation_is_never_overwritten(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        before = P.open_manifest_path(self.root, self.cycle_id).read_bytes()
        P.reservation_path(self.root, self.cycle_id).write_text("{not json", encoding="utf-8")
        self.write_output(self.result, "plans/cycle/new.md", b"n\n")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "reservation-unreadable"))
        self.assertEqual(P.open_manifest_path(self.root, self.cycle_id).read_bytes(), before)

    def test_a_lost_reservation_is_rebuilt_from_the_published_document(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        first = _ids_by_path(self.interim())
        P.reservation_path(self.root, self.cycle_id).unlink()
        self.write_output(self.result, "plans/cycle/new.md", b"n\n")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual((result["status"], result["reservation"]), ("emitted", "document-fallback"))
        self.assertEqual(_ids_by_path(self.interim())["artifacts/plans/cycle/plan.md"],
                         first["artifacts/plans/cycle/plan.md"])

    def test_interim_validator_rejects_terminal_events(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        document = self.interim()
        self.assertTrue(m.validate_interim(document).ok)
        forged = json.loads(json.dumps(document))
        forged["routes"][0]["terminal_marker"] = "done"
        self.assertFalse(m.validate_interim(forged).ok)
        forged = json.loads(json.dumps(document))
        event = dict(forged["events"][0], event_type="cycle.completed", target_id=self.cycle_id,
                     event_id="evt_" + "9" * 32, stream_id="strm_" + "9" * 32)
        forged["events"].append(event)
        self.assertIn("interim-terminal-event", {v.code for v in m.validate_interim(forged).violations})

    def test_cli_defaults_to_the_producer_environment(self):
        self.write_output(self.result)
        out = io.StringIO()
        env = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ARTIFACT_CYCLE_ID": self.cycle_id}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = P.main(["checkpoint"])
        self.assertEqual(code, P.OK)
        self.assertEqual(json.loads(out.getvalue())["status"], "emitted")


class CheckpointDisplayTitleTest(CheckpointTestBase):
    def resume(self, **kwargs):
        return P.begin(self.root, route_file=self.route_file, capability="autopilot-code",
                       intensity="direct", **kwargs)

    def test_owner_resume_title_updates_current_binding_without_payload_scan(self):
        self.write_output(self.result, data="# 이전 한국어 제목\n".encode())
        payload = self.write_output(self.result, "plans/cycle/delivery.zip", b"large payload fixture")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        _, _, other = self.begin(mode="debug", title="다른 사이클의 기존 제목")
        self.write_output(other, data=b"other")
        P.checkpoint(self.root, cycle_id=other["cycle_id"])
        other_entry = next(row for row in self.titles() if row["cycle_id"] == other["cycle_id"])
        record_before = P.read_cycle_record(self.root, self.cycle_id)
        protected = [P.open_manifest_path(self.root, self.cycle_id),
                     P.checkpoint_state_path(self.root, self.cycle_id),
                     P.reservation_path(self.root, self.cycle_id), self.route_file, payload]
        before = {path: path.read_bytes() for path in protected}
        title = "9/21 현장 요청 — 최종 결과 전달"
        real_read = Path.read_bytes
        def read(path):
            if path == payload:
                raise AssertionError("title resume must not read ZIP payload")
            return real_read(path)
        with mock.patch.object(P, "_checkpoint_scan", side_effect=AssertionError("no scan")), \
             mock.patch.object(P, "checkpoint", side_effect=AssertionError("no checkpoint")), \
             mock.patch.object(Path, "read_bytes", read):
            resumed = self.resume(title=title)
        self.assertEqual(resumed["status"], "resumed")
        self.assertTrue(resumed["title_updated"])
        self.assertEqual(resumed["cycle_id"], self.cycle_id)
        current = P.read_cycle_record(self.root, self.cycle_id)
        self.assertEqual({k: v for k, v in current.items() if k != "control_record_digest"},
                         {k: v for k, v in {**record_before, "title": title}.items() if k != "control_record_digest"})
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertIn(other_entry, self.titles())
        entry = next(row for row in self.titles() if row["cycle_id"] == self.cycle_id)
        self.assertEqual(entry["display_title"], title)
        self.assertEqual(entry["manifest_bindings"], [{
            "manifest_revision_id": self.interim()["manifest_revision_id"],
            "manifest_digest": CT.binding_digest(self.interim())}])

    def test_omitted_same_title_and_worker_resume_do_not_write_title_metadata(self):
        self.write_output(self.result, data="# 기존 제목 유지 확인\n".encode())
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.resume(title="사용자가 지정한 한국어 제목")
        paths = [P.cycle_record_path(self.root, self.cycle_id), self.root / CT.CYCLE_TITLES_REL]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        with mock.patch.object(P, "_write_cycle_record", side_effect=AssertionError("no record write")), \
             mock.patch.object(CT, "emit_after_checkpoint", side_effect=AssertionError("no title emit")):
            self.resume()
            self.resume(title="사용자가 지정한 한국어 제목")
            self.resume(node_id=self.route_obj["nodes"][0]["id"], title="작업자가 바꾸면 안 되는 제목")
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)

    def test_resume_title_without_manifest_changes_record_only(self):
        with mock.patch.object(P, "_checkpoint_scan", side_effect=AssertionError("no scan")):
            self.resume(title="산출물 생성 전 한국어 제목")
        self.assertEqual(P.read_cycle_record(self.root, self.cycle_id)["title"], "산출물 생성 전 한국어 제목")
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())
        self.assertEqual(self.titles(), [])

    def test_resume_title_failure_uses_existing_checkpoint_retry(self):
        self.write_output(self.result, data="# 원래 표시 제목\n".encode())
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        with mock.patch.object(CT, "_upsert_title_locked", side_effect=OSError("fixture")):
            self.assertTrue(self.resume(title="재시도할 한국어 제목")["title_updated"])
        self.assertEqual(P.read_cycle_record(self.root, self.cycle_id)["title"], "재시도할 한국어 제목")
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "unchanged")
        self.assertEqual(self.titles()[0]["display_title"], "재시도할 한국어 제목")

    def test_resume_title_closed_and_sealed_cycles_are_unchanged(self):
        self.write_output(self.result, data="# 봉인 전 표시 제목\n".encode())
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.seal()
        paths = [P.cycle_record_path(self.root, self.cycle_id), self.root / CT.CYCLE_TITLES_REL,
                 Path(self.result["cycle_dir"]) / "manifest.json"]
        before = {path: path.read_bytes() for path in paths}
        with self.assertRaises(P.ProducerError) as error:
            self.resume(title="봉인 이후 바꾸면 안 되는 제목")
        self.assertEqual(error.exception.code, "route-already-closed")
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_resume_title_campaign_conflict_rejects_without_title_change(self):
        self.write_output(self.result, data="# 캠페인 검사 기존 제목\n".encode())
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        _, _, other = self.begin(mode="debug", campaign_key="other-title-stream")
        paths = [P.cycle_record_path(self.root, self.cycle_id), self.root / CT.CYCLE_TITLES_REL]
        before = {path: path.read_bytes() for path in paths}
        with self.assertRaises(P.ProducerError) as error:
            self.resume(title="다른 캠페인에서 보낸 제목", campaign_id=other["campaign_id"])
        self.assertEqual(error.exception.code, "cycle-campaign-selection-conflict")
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def titles(self):
        path = self.root / CT.CYCLE_TITLES_REL
        return json.loads(path.read_text())["entries"] if path.exists() else []

    def test_month_day_record_title_is_declared_on_first_checkpoint(self):
        title = "9/21 현장 요청 — 최종 분석 및 평가 보고서"
        _, _, result = self.begin(mode="debug", title=title)
        cycle_id = result["cycle_id"]
        self.write_output(result, data="# M3_5 → M3_9 최종 분석 및 평가 보고서\n".encode())
        self.assertEqual(P.checkpoint(self.root, cycle_id=cycle_id)["status"], "emitted")
        entries = self.titles()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["cycle_id"], cycle_id)
        self.assertEqual(entries[0]["display_title"], title)
        self.assertEqual(entries[0]["manifest_bindings"][0]["manifest_revision_id"],
                         json.loads(P.open_manifest_path(self.root, cycle_id).read_text())["manifest_revision_id"])

    def test_heading_refresh_rebinds_current_manifest_and_seal(self):
        self.write_output(self.result, data="# 열린 사이클의 첫 제목\n".encode())
        first = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(first["status"], "emitted")
        self.assertEqual(self.titles()[0]["display_title"], "열린 사이클의 첫 제목")
        self.write_output(self.result, data="# 열린 사이클의 고친 제목\n".encode())
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "emitted")
        entry = self.titles()[0]
        self.assertEqual(entry["display_title"], "열린 사이클의 고친 제목")
        self.assertEqual(entry["manifest_bindings"], [{
            "manifest_revision_id": self.interim()["manifest_revision_id"],
            "manifest_digest": CT.binding_digest(self.interim())}])
        before = (self.root / CT.CYCLE_TITLES_REL).read_bytes()
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "unchanged")
        self.assertEqual((self.root / CT.CYCLE_TITLES_REL).read_bytes(), before)
        _, sealed = self.seal()
        self.assertEqual(self.titles()[0]["manifest_bindings"][0]["manifest_digest"], CT.binding_digest(sealed))

    def test_no_output_or_only_slug_never_creates_title(self):
        record = P.read_cycle_record(self.root, self.cycle_id)
        record["title"] = "사람이 읽을 수 있는 제목"
        P._write_cycle_record(self.root, record, exclusive=False)
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["reason"], "no-output")
        self.assertEqual(self.titles(), [])
        record["title"] = "fixture-slug"
        P._write_cycle_record(self.root, record, exclusive=False)
        self.write_output(self.result, data=b"plain body without a heading\n")
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "emitted")
        self.assertEqual(self.titles(), [])
        self.write_output(self.result, data="# 제목이 있는 중간 산출물\n".encode())
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(len(self.titles()), 1)
        self.write_output(self.result, data=b"heading removed\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(self.titles(), [])

    def test_title_write_failure_does_not_block_and_unchanged_checkpoint_retries(self):
        self.write_output(self.result, data="# 다음 체크포인트에서 재시도\n".encode())
        with mock.patch.object(CT, "_upsert_title_locked", side_effect=OSError("fixture")):
            self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "emitted")
        self.assertEqual(self.titles(), [])
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id)["status"], "unchanged")
        self.assertEqual(self.titles()[0]["display_title"], "다음 체크포인트에서 재시도")

    def test_other_cycles_and_backfill_preserve_open_title(self):
        self.write_output(self.result, data="# 첫 사이클의 제목 유지\n".encode())
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        first = self.titles()[0]
        _, _, other = self.begin(mode="debug")
        self.write_output(other, data="# 다른 사이클의 제목 유지\n".encode())
        P.checkpoint(self.root, cycle_id=other["cycle_id"])
        self.assertIn(first, self.titles())
        self.assertEqual(len(self.titles()), 2)
        def reader(raw, *_args, **_kwargs):
            return CT.ReaderResult("accepted", frozenset(row["cycle_id"] for row in json.loads(raw)["entries"]), "", None)
        result = CT.backfill(self.root, reader=reader)
        self.assertEqual(result["counts"]["existing_kept"], 2)
        self.assertEqual(result["counts"]["existing_dropped"], 0)
        self.assertEqual(result["post_digest"], result["pre_digest"])


class FinalizeContinuityTest(CheckpointTestBase):
    def test_sealed_manifest_keeps_interim_ids_and_drops_the_interim(self):
        self.write_output(self.result, "plans/cycle/plan.md", b"v1\n")
        self.write_output(self.result, "plans/cycle/final_report.md", b"report\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        interim = _ids_by_path(self.interim())
        interim_manifest_id = self.interim()["manifest_id"]
        self.write_output(self.result, "plans/cycle/plan.md", b"v2 changed\n")
        self.write_output(self.result, "plans/cycle/late.md", b"late\n")
        self.close(self.route_obj, self.route_file)
        sealed = P.finalize(self.root, cycle_id=self.cycle_id)
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual((sealed["interim_ids"], sealed["interim_ids_kept"]), ("present", 2))
        document = json.loads((Path(self.result["cycle_dir"]) / "manifest.json").read_text())
        self.assertTrue(m.validate(document).ok)
        self.assertEqual(document["cycle"]["state"], "completed")
        final = _ids_by_path(document)
        plan, report = "artifacts/plans/cycle/plan.md", "artifacts/plans/cycle/final_report.md"
        self.assertEqual(final[plan][0], interim[plan][0])
        self.assertNotEqual(final[plan][1], interim[plan][1])
        self.assertEqual(final[report][:2], interim[report][:2])
        self.assertNotIn(final["artifacts/plans/cycle/late.md"][0], {v[0] for v in interim.values()})
        self.assertEqual(document["manifest_id"], interim_manifest_id)
        for path in (P.open_manifest_path(self.root, self.cycle_id),
                     P.checkpoint_state_path(self.root, self.cycle_id)):
            self.assertFalse(path.exists(), path)

    def test_abandoned_seal_keeps_ids_too(self):
        self.write_output(self.result, "plans/cycle/plan.md")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        interim = _ids_by_path(self.interim())
        sealed = P.finalize(self.root, cycle_id=self.cycle_id, state="abandoned",
                            abandon_reason="operator-decision", allow_open_route=True)
        document = json.loads((Path(self.result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(document["cycle"]["state"], "abandoned")
        self.assertEqual(_ids_by_path(document)["artifacts/plans/cycle/plan.md"][:2],
                         interim["artifacts/plans/cycle/plan.md"][:2])
        self.assertEqual(sealed["interim_ids_kept"], 1)
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())

    def test_unreadable_reservation_falls_back_to_the_document_and_says_so(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        interim = _ids_by_path(self.interim())
        P.reservation_path(self.root, self.cycle_id).write_text("{not json", encoding="utf-8")
        sealed, document = self.seal()
        self.assertEqual((sealed["status"], sealed["interim_ids"], sealed["interim_ids_kept"]),
                         ("sealed", "unreadable", 1))
        self.assertEqual(_ids_by_path(document)["artifacts/plans/cycle/plan.md"][:2],
                         interim["artifacts/plans/cycle/plan.md"][:2])
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())
        self.assertFalse(P.reservation_path(self.root, self.cycle_id).exists())

    def test_a_malformed_reused_field_never_blocks_the_seal(self):
        # Review N1: a bad timestamp in the ledger used to make finalize refuse.
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        interim = _ids_by_path(self.interim())
        ledger = self.ledger()
        row = ledger["artifacts"]["artifacts/plans/cycle/plan.md"]
        row["recorded_at"] = "2026-09-19 08:00:00"
        P.reservation_path(self.root, self.cycle_id).write_text(json.dumps(ledger), encoding="utf-8")
        sealed, document = self.seal()
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(_ids_by_path(document)["artifacts/plans/cycle/plan.md"][:2],
                         interim["artifacts/plans/cycle/plan.md"][:2])

    def test_a_reservation_the_validator_rejects_is_rebuilt_not_fatal(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        interim = _ids_by_path(self.interim())
        real = P.artifact_manifest.validate
        calls = []

        def reject_first(document):
            calls.append(1)
            if len(calls) == 1:
                return m._report([m.Violation("malformed-timestamp", "$.events[0]", "fixture")])
            return real(document)

        with mock.patch.object(P.artifact_manifest, "validate", side_effect=reject_first):
            sealed, document = self.seal()
        self.assertEqual((sealed["status"], sealed["interim_ids_rebuilt"], sealed["interim_ids_kept"]),
                         ("sealed", "events-fresh", 1))
        self.assertEqual(_ids_by_path(document)["artifacts/plans/cycle/plan.md"][:2],
                         interim["artifacts/plans/cycle/plan.md"][:2])

    def test_content_that_returns_to_an_earlier_digest_returns_to_its_revision(self):
        # Review N2: the ledger took v2, the document write failed, the file went
        # back to v1 -- the published v1 revision ID must survive.
        target = self.write_output(self.result, "plans/cycle/plan.md", b"v1\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        v1 = _ids_by_path(self.interim())["artifacts/plans/cycle/plan.md"]
        target.write_bytes(b"v2 changed\n")
        real_write = P._write_atomic
        interim_path = P.open_manifest_path(self.root, self.cycle_id)

        def fail_document(path, data, mode=0o644):
            if Path(path) == interim_path:
                raise OSError(28, "No space left on device")
            return real_write(path, data, mode)

        with mock.patch.object(P, "_write_atomic", side_effect=fail_document):
            with self.assertRaises(OSError):
                P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertNotEqual(self.ledger()["artifacts"]["artifacts/plans/cycle/plan.md"]["content_digest"], v1[2])
        target.write_bytes(b"v1\n")
        result = P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(result["status"], "emitted", result)
        self.assertEqual(_ids_by_path(self.interim())["artifacts/plans/cycle/plan.md"], v1)
        _sealed, document = self.seal()
        self.assertEqual(_ids_by_path(document)["artifacts/plans/cycle/plan.md"][:3], v1[:3])

    def test_no_reservation_at_all_seals_with_fresh_ids_and_says_so(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        P.reservation_path(self.root, self.cycle_id).write_text("{not json", encoding="utf-8")
        P.open_manifest_path(self.root, self.cycle_id).write_text("garbage", encoding="utf-8")
        sealed, _document = self.seal()
        self.assertEqual((sealed["interim_ids"], sealed["interim_ids_kept"]), ("unreadable", 0))

    def test_a_path_left_out_of_a_later_checkpoint_keeps_its_id_at_the_seal(self):
        # Review repro 1: a log grows past the interim size limit, drops out of
        # the next interim document, and must still seal under its first ID.
        self.write_output(self.result, "experiments/run/train.log", b"x" * 8)
        limits = P.CheckpointLimits(max_file_bytes=32)
        P.checkpoint(self.root, cycle_id=self.cycle_id, limits=limits)
        first = _ids_by_path(self.interim())["artifacts/experiments/run/train.log"]
        self.write_output(self.result, "experiments/run/train.log", b"x" * 64)
        self.write_output(self.result, "experiments/run/notes.md", b"n\n")
        self.assertEqual(P.checkpoint(self.root, cycle_id=self.cycle_id, limits=limits)["status"], "emitted")
        self.assertNotIn("artifacts/experiments/run/train.log", _ids_by_path(self.interim()))
        self.assertIn("artifacts/experiments/run/train.log", self.ledger()["artifacts"])
        _sealed, document = self.seal()
        self.assertEqual(_ids_by_path(document)["artifacts/experiments/run/train.log"][0], first[0])

    def test_a_recreated_file_keeps_its_id(self):
        target = self.write_output(self.result, "plans/cycle/plan.md", b"v1\n")
        self.write_output(self.result, "plans/cycle/other.md", b"o\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        first = _ids_by_path(self.interim())["artifacts/plans/cycle/plan.md"]
        target.unlink()
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertNotIn("artifacts/plans/cycle/plan.md", _ids_by_path(self.interim()))
        target.write_bytes(b"v1\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(_ids_by_path(self.interim())["artifacts/plans/cycle/plan.md"][:2], first[:2])

    def test_unchanged_revisions_are_byte_identical_across_emissions_and_the_seal(self):
        self.write_output(self.result, "plans/cycle/final_report.md", b"report\n")
        self.write_output(self.result, "plans/cycle/log.md", b"1\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)

        def rows(document, locator):
            revision = next(r for r in document["artifact_revisions"] if r["locator"]["path"] == locator)
            event = next(e for e in document["events"] if e["target_id"] == revision["artifact_id"])
            return revision, event

        report = "artifacts/plans/cycle/final_report.md"
        first = rows(self.interim(), report)
        self.write_output(self.result, "plans/cycle/log.md", b"2\n")
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        self.assertEqual(rows(self.interim(), report), first)
        _sealed, document = self.seal()
        self.assertEqual(rows(document, report), first)
        self.assertFalse(P.reservation_path(self.root, self.cycle_id).exists())

    def test_no_interim_leaves_the_finalize_result_unchanged(self):
        self.write_output(self.result)
        self.close(self.route_obj, self.route_file)
        sealed = P.finalize(self.root, cycle_id=self.cycle_id)
        self.assertNotIn("interim_ids", sealed)

    def test_no_lineage_finalize_drops_the_interim(self):
        target = self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        target.unlink()
        target.parent.rmdir(); target.parent.parent.rmdir()
        result = P.finalize(self.root, cycle_id=self.cycle_id, state="abandoned",
                            abandon_reason="operator-decision")
        self.assertEqual(result["status"], "no-lineage")
        self.assertFalse(P.open_manifest_path(self.root, self.cycle_id).exists())

    def test_recover_sweeps_interims_of_cycles_that_are_not_open(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        record = P.read_cycle_record(self.root, self.cycle_id)
        record["state"] = "dropped"
        P._write_cycle_record(self.root, record, exclusive=False)
        result = P.recover(self.root)
        self.assertEqual(result["producer"]["interims_removed"], [self.cycle_id])
        for path in (P.open_manifest_path(self.root, self.cycle_id),
                     P.reservation_path(self.root, self.cycle_id),
                     P.checkpoint_state_path(self.root, self.cycle_id)):
            self.assertFalse(path.exists(), path)

    def test_sweep_keeps_a_cycle_whose_record_cannot_be_read(self):
        self.write_output(self.result)
        P.checkpoint(self.root, cycle_id=self.cycle_id)
        record_path = P.cycle_record_path(self.root, self.cycle_id)
        original = record_path.read_bytes()
        record_path.write_text("{torn", encoding="utf-8")
        self.assertEqual(P._sweep_orphan_interims(self.root), [])
        self.assertTrue(P.reservation_path(self.root, self.cycle_id).exists())
        record_path.write_bytes(original)
        stray = P.open_manifest_path(self.root, self.cycle_id).with_name(".x.json.tmp-1-ab")
        stray.write_text("{}", encoding="utf-8")
        old = time.time() - 7200
        os.utime(stray, (old, old))
        ghost = "cyc_" + "7" * 32
        P.open_manifest_path(self.root, ghost).write_text("{}", encoding="utf-8")
        self.assertEqual(P._sweep_orphan_interims(self.root), [ghost])
        self.assertFalse(stray.exists())
        self.assertTrue(P.open_manifest_path(self.root, self.cycle_id).exists())


class TriggerTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self._tmp.name, "PATH": os.environ.get("PATH", "")}
        self.root = Path(self._tmp.name) / "root"
        (self.root / T.CUTOVER_REL).parent.mkdir(parents=True)
        (self.root / T.CUTOVER_REL).write_text("{}", encoding="utf-8")
        self.r = str(self.root)

    def test_test_processes_never_launch(self):
        with mock.patch.object(T.subprocess, "Popen") as popen:
            self.assertFalse(T.launch(trigger="turn-end", key="k", artifact_root="/r",
                                      cycle_id="cyc_" + "a" * 32, env=self.env))
        popen.assert_not_called()

    def test_stamp_limits_launches_per_key_and_off_disables(self):
        with mock.patch.object(T, "in_test_process", return_value=False), \
                mock.patch.object(T.subprocess, "Popen") as popen:
            kwargs = dict(trigger="stage-complete", key="route-rt-abc", artifact_root=self.r, route="rt-abc")
            self.assertTrue(T.launch(env=self.env, now=1000.0, **kwargs))
            self.assertFalse(T.launch(env=self.env, now=1100.0, **kwargs))
            self.assertTrue(T.launch(env=self.env, now=1000.0 + 901, **kwargs))
            self.assertFalse(T.launch(env={**self.env, "AGENT_ARTIFACT_CHECKPOINT": "off"},
                                      now=5000.0, **kwargs))
            self.assertFalse(T.launch(env=self.env, now=9000.0, **{**kwargs, "trigger": "explicit"}))
        self.assertEqual(popen.call_count, 2)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], [str(T.PRODUCER), "checkpoint", "--trigger", "stage-complete",
                                    "--artifact-root", self.r, "--route", "rt-abc"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_turn_end_prefers_the_worker_cycle_then_the_route_chain(self):
        cycle = "cyc_" + "b" * 32
        with mock.patch.object(T, "in_test_process", return_value=False), \
                mock.patch.object(T.subprocess, "Popen") as popen:
            env = {**self.env, "AGENT_ARTIFACT_ROOT": self.r, "AGENT_ARTIFACT_CYCLE_ID": cycle}
            self.assertTrue(T.launch_for_session("codex", "sid-1", env=env))
            self.assertEqual(popen.call_args.args[0][-2:], ["--cycle", cycle])
            with mock.patch.object(T, "session_route", return_value=(self.r, self.r + "/rt.json")):
                self.assertTrue(T.launch_for_session("claude", "sid-2", env=self.env))
            self.assertEqual(popen.call_args.args[0][-4:], ["--artifact-root", self.r, "--route", self.r + "/rt.json"])
            with mock.patch.object(T, "session_route", return_value=None):
                self.assertFalse(T.launch_for_session("claude", "sid-3", env=self.env))

    def test_session_route_reads_the_latest_route_chain_line(self):
        chain = Path(self._tmp.name) / "chains"
        ledger = chain / "claude" / "sid-9.jsonl"
        ledger.parent.mkdir(parents=True)
        rows = [
            {"v": 1, "ts": 1.0, "harness": "claude", "session_id": "sid-9", "route_id": "rt-1",
             "route_file": "/a/rt-1.json", "artifact_root": "/a"},
            {"v": 1, "ts": 2.0, "harness": "claude", "session_id": "sid-9", "route_id": "rt-2",
             "route_file": "/b/rt-2.json", "artifact_root": "/b"},
        ]
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_ROUTE_CHAIN_DIR": str(chain)}):
            self.assertEqual(T.session_route("claude", "sid-9"), ("/b", "/b/rt-2.json"))

    def test_inactive_roots_and_trigger_keys(self):
        with mock.patch.object(T, "in_test_process", return_value=False), \
                mock.patch.object(T.subprocess, "Popen") as popen:
            route = {"artifact_root": self.r, "route_id": "rt-1"}
            self.assertTrue(T.launch_for_route(route, trigger="supervisor-poll", env=self.env))
            # A stage completion is not swallowed by the supervisor's stamp.
            self.assertTrue(T.launch_for_route(route, trigger="stage-complete", env=self.env))
            self.assertFalse(T.launch_for_route(route, trigger="stage-complete", env=self.env))
            inactive = {"artifact_root": self._tmp.name, "route_id": "rt-2"}
            self.assertFalse(T.launch_for_route(inactive, trigger="stage-complete", env=self.env))
        self.assertEqual(popen.call_count, 2)

    def test_route_trigger_ignores_malformed_routes(self):
        self.assertFalse(T.launch_for_route(None, trigger="stage-complete"))
        self.assertFalse(T.launch_for_route({"route_id": "rt-1"}, trigger="stage-complete"))


if __name__ == "__main__":
    unittest.main()
