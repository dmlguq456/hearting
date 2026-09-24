#!/usr/bin/env python3
"""Gate tests for the `--notes` (cairn-w8-notes/v1) input of artifact-w8-handoff.py."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w8", ROOT / "tools" / "artifact-w8-handoff.py")
w8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w8)

GOOD = {"id": "note-a", "parent_id": None, "page_no": None, "repo": "hearting", "source_dir": "/x/.agent_reports/plans/a.md",
        "source_capability": None, "trashed_at": None, "revision": 1}


def write(tmp, doc):
    path = Path(tmp) / "notes.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class LoadNotesTest(unittest.TestCase):
    def test_accepts_exact_allowlist_and_sorts_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, meta = w8.load_notes(write(tmp, {"schema": w8.NOTES_SCHEMA, "exported_at": "2026-08-26T00:00:00Z",
                                                   "notes": [{**GOOD, "id": "note-b"}, GOOD]}))
        self.assertEqual([r["id"] for r in rows], ["note-a", "note-b"])
        self.assertEqual(set(rows[0]), w8.NOTE_ALLOWED_KEYS)
        self.assertEqual(meta, {"exported_at": "2026-08-26T00:00:00Z"})

    def test_rejects_body_bearing_keys_anywhere(self):
        for bad in ({**GOOD, "body": ""}, {**GOOD, "title": "t"}):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
                w8.load_notes(write(tmp, {"notes": [bad]}))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"source": {"body": "leak"}, "notes": [GOOD]}))

    def test_rejects_extra_missing_keys_duplicates_and_wrong_types(self):
        cases = [
            {**GOOD, "card_id": "c1"},
            {k: v for k, v in GOOD.items() if k != "revision"},
            {**GOOD, "page_no": "1"},
            {**GOOD, "id": ""},
        ]
        for bad in cases:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
                w8.load_notes(write(tmp, {"notes": [bad]}))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"notes": [GOOD, dict(GOOD)]}))

    def test_rejects_foreign_schema(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"schema": "other/v9", "notes": [GOOD]}))


class BundleNotesRowTest(unittest.TestCase):
    def test_existing_notes_row_and_counts(self):
        b = w8.Bundle.__new__(w8.Bundle)
        b.notes = [GOOD, {**GOOD, "id": "note-t", "trashed_at": "2026-01-01T00:00:00Z"}, {**GOOD, "id": "note-o", "repo": "other"}]
        b.notes_meta = {"exported_at": "x"}
        counts = b.existing_note_counts()
        self.assertEqual(counts, {"total": 3, "active": 2, "trashed": 1, "active_by_repo": {"hearting": 1, "other": 1}})
        row = b.existing_notes()
        self.assertEqual(row["schema"], w8.NOTES_SCHEMA)
        self.assertTrue(row["body_free"])
        self.assertEqual(row["columns"], sorted(w8.NOTE_ALLOWED_KEYS))
        self.assertEqual(w8._forbidden_keys(row), [])
        b.notes = None
        self.assertIsNone(b.existing_note_counts())



class PickPrimaryTest(unittest.TestCase):
    def rows(self, *locators):
        return [{"locator": f"campaigns/c/cycles/y/artifacts/plans/x/{l}"} for l in sorted(locators)]

    def test_name_priority_beats_locator_order(self):
        rows = self.rows("_internal/prompts/plan.md", "evidence/a.json", "final_report.md")
        self.assertTrue(w8.pick_primary(rows)["locator"].endswith("/x/final_report.md"))

    def test_shallowest_path_wins_within_a_name(self):
        rows = self.rows("plan/plan.md", "plan.md", "notes.md")
        self.assertTrue(w8.pick_primary(rows)["locator"].endswith("/x/plan.md"))

    def test_falls_back_to_first_row(self):
        rows = self.rows("b.md", "a.md")
        self.assertTrue(w8.pick_primary(rows)["locator"].endswith("/x/a.md"))


class HistoricalMappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.b = w8.Bundle.__new__(w8.Bundle)
        self.b.root = self.root
        self.b.dir = self.root / "output"
        self.b.manifest_inputs = {}
        self.b.w7_evidence = self.root / "w7"
        self.b.w7c_run = self.root / "w7c"
        self.b.retirement_run = self.root / "retirement"
        self.b.backup_tar = None
        for folder in (self.b.w7_evidence, self.b.w7c_run):
            folder.mkdir()
        self.population = []
        for i, identity in enumerate(("manifest", "manifest", "shared-revision", "excluded")):
            locator = f"current/{i}/report.md"
            path = self.root / locator
            path.parent.mkdir(parents=True)
            path.write_text(f"immutable-{i}")
            self.population.append({"locator": locator, "content_digest": w8.sha_file(path),
                                    "byte_size": path.stat().st_size, "identity_class": identity,
                                    "artifact_id": f"art_{i}" if identity == "manifest" else None,
                                    "artifact_revision_id": f"arev_{i}" if identity == "manifest" else None,
                                    "shared_reference_revision_id": "rrev_old" if identity == "shared-revision" else None,
                                    **({"reason": "hidden-component"} if identity == "excluded" else {})})
        self.w7 = [{"source_locator": "legacy/same", "target_locator": "historical/0", "kind": "file"}]
        self.w7c = [{"source_locator": "legacy/same", "target_locator": "historical/1", "kind": "file",
                     "sha256": self.population[1]["content_digest"][7:]}]
        self.links = [{"source_locator": f"historical/{i}", "target_locator": row["locator"], "kind": "file"}
                      for i, row in enumerate(self.population)]
        # 최신 source 연결을 사용하면 두 역사 identity가 하나로 잘못 합쳐진다.
        self.links.append({"source_locator": "legacy/same", "target_locator": self.population[1]["locator"], "kind": "file"})
        (self.b.w7_evidence / "backup-seal.json").write_text("{}")
        (self.b.w7_evidence / "apply-receipt.json").write_text("{}")
        (self.b.w7_evidence / "applied-inverse.jsonl").write_text("")
        self.seal_maps()

    def seal_maps(self):
        entries = []
        for path, rows in ((self.b.w7_evidence / "compatibility-map.jsonl", self.w7),
                           (self.b.w7c_run / "compatibility-map.jsonl", self.w7c),
                           (self.root / "relayout.jsonl", self.links)):
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            entries.append({"path": str(path), "sha256": w8.sha_file(path)[7:], "rows": len(rows)})
        compat = w8.C.compat_path(self.root)
        compat.parent.mkdir(parents=True, exist_ok=True)
        compat.write_text(json.dumps({"maps": entries}))
        journal = [{**row, "sha256": self.population[int(row["target_locator"].split("/")[-1])]["content_digest"][7:]}
                   for row in self.w7]
        (self.b.w7_evidence / "applied-journal.jsonl").write_text("".join(json.dumps(row) + "\n" for row in journal))

    def test_historical_targets_preserve_conflict_and_exact_identity(self):
        self.b.prepare_mapping(self.population)
        rows, conflicts = self.b.legacy_mapping(self.population)
        self.assertEqual([r["target_locator"] for r in rows], [r["locator"] for r in self.population[:2]])
        self.assertEqual([r["artifact_revision_id"] for r in rows], ["arev_0", "arev_1"])
        self.assertEqual(conflicts, [{"legacy_locator": "legacy/same", "maps": ["w7-e2e3", "w7c-delta"]}])
        handoff = self.b.relocation_handoff(self.population)
        self.assertEqual(handoff["exceptions"]["map_targets_without_manifest_identity_count"], 0)
        self.assertEqual(handoff["locator_snapshot"]["population_sha256"], self.b.population_digest)
        for row in rows:
            origin = next(m for m in handoff["maps"] if m["name"] == row["map"])
            self.assertEqual(row["provenance"]["map_sha256"], origin["sha256"])
            self.assertEqual(row["provenance"]["map_row"], 1)

    def test_shared_and_excluded_keep_original_identities(self):
        for i in (2, 3):
            self.w7.append({"source_locator": f"legacy/{i}", "target_locator": f"historical/{i}", "kind": "file"})
        self.seal_maps()
        self.b.prepare_mapping(self.population)
        rows = {r["legacy_locator"]: r for r in self.b.legacy_mapping(self.population)[0]}
        self.assertEqual(rows["legacy/2"]["shared_reference_revision_id"], "rrev_old")
        self.assertIsNone(rows["legacy/2"]["artifact_id"])
        self.assertEqual(rows["legacy/3"]["state"], "excluded")

    def test_all_350_conflicts_survive_even_with_conflicting_latest_source_rows(self):
        self.w7 = [{**self.w7[0], "source_locator": f"legacy/{i}"} for i in range(350)]
        self.w7c = [{**self.w7c[0], "source_locator": f"legacy/{i}"} for i in range(350)]
        self.links.extend({"source_locator": "legacy/same", "target_locator": self.population[i]["locator"]}
                          for i in (0, 1))
        self.seal_maps()
        self.b.prepare_mapping(self.population)
        rows, conflicts = self.b.legacy_mapping(self.population)
        self.assertEqual(len(rows), 700)
        self.assertEqual(len(conflicts), 350)
        self.assertTrue(all(r["maps"] == ["w7-e2e3", "w7c-delta"] for r in conflicts))
        self.assertEqual({r["artifact_revision_id"] for r in rows}, {"arev_0", "arev_1"})

    def test_mapping_outside_selected_population_refuses(self):
        with self.assertRaisesRegex(ValueError, "mapping-target-outside-population"):
            self.b.prepare_mapping(self.population[1:])

    def test_snapshot_matches_official_exact_present_and_ancestor_lookup(self):
        self.links.append({"source_locator": "ancestor", "target_locator": "current/0", "kind": "dir"})
        self.seal_maps()
        snap = w8.LocatorSnapshot(self.root)
        for locator in ("historical/0", "historical/1", "current/2/report.md", "ancestor/report.md"):
            expected = w8.C.resolve_legacy(self.root, locator)
            target, provenance = snap.resolve(locator)
            self.assertEqual((target, provenance["resolution"]), (expected["target"], expected["resolution"]))
        self.assertEqual(w8.C.resolve_legacy(self.root, "missing")["resolution"], "unresolved")
        with self.assertRaisesRegex(ValueError, "historical-target-unresolved"):
            snap.resolve("missing")

    def test_missing_manifest_member_refuses_before_any_output(self):
        (self.root / self.population[1]["locator"]).unlink()
        self.b.stable_population = lambda: self.population
        with self.assertRaisesRegex(ValueError, "population-target-missing"):
            self.b.build()
        self.assertFalse(self.b.dir.exists())

    def test_map_drift_and_unregistered_original_refuse(self):
        path = self.root / "relayout.jsonl"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "compat-map-drift"):
            self.b.prepare_mapping(self.population)
        self.seal_maps()
        w8.C.compat_path(self.root).write_text('{"maps": []}')
        with self.assertRaisesRegex(ValueError, "original-map-not-in-compat"):
            self.b.prepare_mapping(self.population)

    def test_snapshot_and_population_changes_refuse(self):
        self.b.prepare_mapping(self.population)
        path = self.root / "relayout.jsonl"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "locator-snapshot-drift"):
            self.b.mapping_snapshot.verify_unchanged()
        self.population[0]["artifact_id"] = "wrong"
        with self.assertRaisesRegex(ValueError, "mapping-population-changed"):
            self.b.legacy_mapping(self.population)

    def test_actual_and_historical_digest_drift_refuse(self):
        path = self.root / self.population[0]["locator"]
        path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "population-target-drift"):
            self.b.prepare_mapping(self.population)
        self.population[0].update(content_digest=w8.sha_file(path), byte_size=path.stat().st_size)
        with self.assertRaisesRegex(ValueError, "historical-target-digest-mismatch"):
            self.b.prepare_mapping(self.population)

    def test_unsafe_and_ambiguous_map_targets_refuse(self):
        self.links.append({"source_locator": "historical/0", "target_locator": "current/1/report.md"})
        self.seal_maps()
        with self.assertRaisesRegex(ValueError, "compat-map-ambiguous"):
            self.b.prepare_mapping(self.population)
        self.links[-1]["source_locator"] = "escape"
        self.links[-1]["target_locator"] = "../outside"
        self.seal_maps()
        with self.assertRaisesRegex(ValueError, "locator-unsafe"):
            self.b.prepare_mapping(self.population)

    def test_target_symlink_or_ancestor_symlink_refuses(self):
        path = self.root / self.population[0]["locator"]
        path.unlink()
        path.symlink_to(self.root / self.population[1]["locator"])
        with self.assertRaisesRegex(ValueError, "locator-symlink"):
            self.b.prepare_mapping(self.population)
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.root / "current/1", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "locator-symlink"):
            self.b.prepare_mapping(self.population)


# 발급 CLI는 실제 임시 producer cycle에만 쓴다. 운영 root/DB/원장은 사용하지 않는다.
fixture_spec = importlib.util.spec_from_file_location(
    "w16_producer_fixture", ROOT / "utilities" / "artifact_producer.test.py")
producer_fixture = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(producer_fixture)


class BundleBoundaryCLITest(producer_fixture.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self.activate()
        (self.root / "shared").mkdir()
        route, route_file = self.route(slug="w16-source-fixture")
        source = w8.P.begin(self.root, route_file=route_file,
                            capability="autopilot-code", intensity="direct")
        self.write_output(source)
        self.close(route, route_file)
        w8.P.finalize(self.root, cycle_id=source["cycle_id"])
        self.source_cycle = source["cycle_id"]
        self.output_route, self.output_route_file = self.route(slug="w16-bundle-fixture")
        self.output = w8.P.begin(self.root, route_file=self.output_route_file,
                                 capability="autopilot-code", intensity="direct")
        self.output_dir = Path(self.output["cycle_dir"]) / "artifacts" / "plans" / "fixture"
        self.inputs = Path(self._tmp.name) / "inputs"
        self.inputs.mkdir()
        for name in ("compatibility-map.jsonl", "applied-journal.jsonl", "applied-inverse.jsonl"):
            (self.inputs / name).write_text("")
        (self.inputs / "backup-seal.json").write_text("{}")
        (self.inputs / "apply-receipt.json").write_text(json.dumps({
            "file_bytes_before": 0, "file_bytes_after": 0, "byte_loss": 0, "applied_row_count": 0}))
        self.backup = self.inputs / "fixture-backup.tar"
        self.backup.write_bytes(b"fixture backup; never an operational rollback\n")

    def issue(self, name="default", *options, success=True):
        bundle = self.output_dir / name
        command = [sys.executable, str(ROOT / "tools/artifact-w8-handoff.py"),
                   "--artifact-root", str(self.root), "--bundle-dir", str(bundle),
                   "--cycle", self.source_cycle, "--w7-evidence", str(self.inputs),
                   "--w7c-run", str(self.inputs), "--retirement-run", str(self.inputs),
                   "--backup-tar", str(self.backup), *options]
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return bundle, result

    def test_default_and_explicit_w16_are_sealed_but_unauthorized(self):
        old, _ = self.issue()
        new, _ = self.issue("with-w16", "--include-w16-namespace-delete")
        old_boundary = w8.read_json(old / "approval-boundary.json")
        new_boundary = w8.read_json(new / "approval-boundary.json")
        self.assertEqual([s["stage"] for s in old_boundary["stages"]],
                         ["W9-dry-run", "W10-D20-destructive-apply", "W10-note-link-apply"])
        self.assertEqual(new_boundary["stages"][:3], old_boundary["stages"])
        self.assertEqual(len(new_boundary["stages"]), 4)
        self.assertEqual(new_boundary["stages"][-1]["stage"], "W16-namespace-delete")
        invariant = new_boundary["stages"][-1]["invariant"]
        for namespace in ("fleet-fd8b0c7bc445", "w9-candidate-1d30600937a5",
                          "w9-candidate-f2c03fff8030", "ap_cand_step7"):
            self.assertIn(namespace, invariant)
        self.assertEqual(len({s["approval_id"] for s in new_boundary["stages"]}), 4)
        for bundle, boundary in ((old, old_boundary), (new, new_boundary)):
            self.assertTrue(all(s["authorized"] is False for s in boundary["stages"]))
            for stage in boundary["stages"]:
                self.assertRegex(stage["approval_id"], r"^apr_[0-9a-f]{32}$")
            handoff = w8.read_json(bundle / "handoff.json")
            self.assertEqual(handoff["bundle_digest"], w8.sha_text(w8.canonical(handoff["files"])))
            for name, seal in handoff["files"].items():
                self.assertEqual(seal["sha256"], w8.sha_file(bundle / name))
                self.assertEqual(seal["bytes"], (bundle / name).stat().st_size)
        self.assertNotEqual(w8.read_json(old / "handoff.json")["bundle_digest"],
                            w8.read_json(new / "handoff.json")["bundle_digest"])

    def test_invalid_option_refuses_before_output(self):
        bundle, result = self.issue("invalid", "--include-w16-namespace-delete=true", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(bundle.exists())

    def test_include_prefix_does_not_arm_namespace_delete(self):
        # Plan item 2: argparse's default allow_abbrev=True let the unique
        # prefix "--include-w" silently arm the namespace-delete boundary.
        bundle, result = self.issue("include-w-prefix", "--include-w", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertFalse(bundle.exists())

    def test_cli_refuses_missing_sealed_member_before_bundle_creation(self):
        record = w8.P.read_cycle_record(self.root, self.source_cycle)
        directory = w8.P.cycle_dir(self.root, record["campaign_id"], self.source_cycle)
        doc = w8.read_json(directory / "manifest.json")
        (directory / doc["artifact_revisions"][0]["locator"]["path"]).unlink()
        bundle, result = self.issue("missing-target", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("population-target-missing", result.stderr)
        self.assertFalse(bundle.exists())

    def test_cli_refuses_manifest_raw_digest_drift(self):
        record = w8.P.read_cycle_record(self.root, self.source_cycle)
        path = w8.P.cycle_dir(self.root, record["campaign_id"], self.source_cycle) / "manifest.json"
        path.write_bytes(path.read_bytes() + b"\n")
        bundle, result = self.issue("manifest-drift", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest-binding-mismatch", result.stderr)
        self.assertFalse(bundle.exists())

    def test_publication_refuses_population_changes_after_initial_validation(self):
        # 감독의 실제 Bundle.build 반례: 최초 검사 후 relocation 반환에서 변경.
        # 두 번째 경계는 나머지 payload 생성 중 변경도 seal 직전에 잡는지 확인.
        for boundary in ("relocation_handoff", "approval_boundary"):
            for action in ("remove", "change", "symlink", "ancestor-symlink", "replace-same-bytes", "directory"):
                with self.subTest(boundary=boundary, action=action):
                    fixture = BundleBoundaryCLITest("test_default_and_explicit_w16_are_sealed_but_unauthorized")
                    fixture.setUp()
                    try:
                        b = w8.Bundle(fixture.root, fixture.output_dir / "changed", [fixture.source_cycle],
                                      fixture.inputs, fixture.inputs, fixture.inputs, fixture.backup, None)
                        population = b.stable_population()
                        target = fixture.root / population[0]["locator"]
                        original = getattr(b, boundary)

                        def mutate(*args):
                            result = original(*args)
                            if action == "remove":
                                target.unlink()
                            elif action == "change":
                                target.write_bytes(b"changed after initial population verification")
                            elif action == "symlink":
                                other = target.with_name("replacement")
                                other.write_bytes(target.read_bytes())
                                target.unlink()
                                target.symlink_to(other)
                            elif action == "ancestor-symlink":
                                moved = target.parent.with_name(target.parent.name + "-moved")
                                target.parent.rename(moved)
                                target.parent.symlink_to(moved, target_is_directory=True)
                            elif action == "replace-same-bytes":
                                other = target.with_name("replacement")
                                other.write_bytes(target.read_bytes())
                                other.replace(target)
                            else:
                                target.unlink()
                                target.mkdir()
                            return result

                        setattr(b, boundary, mutate)
                        with self.assertRaises((ValueError, OSError)):
                            b.build()
                        self.assertFalse((b.dir / "handoff.json").exists())
                        if boundary == "relocation_handoff":
                            self.assertFalse(b.dir.exists())
                    finally:
                        fixture.tearDown()
                        fixture.doCleanups()

    def test_sealed_bundle_cannot_be_reissued_with_w16(self):
        bundle, _ = self.issue()
        before = {p.name: p.read_bytes() for p in bundle.iterdir()}
        self.close(self.output_route, self.output_route_file)
        w8.P.finalize(self.root, cycle_id=self.output["cycle_id"])
        _, result = self.issue("default", "--include-w16-namespace-delete", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bundle dir not writable under the producer contract", result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in bundle.iterdir()})

    @unittest.skipUnless(os.environ.get("CAIRN_CHECKOUT"), "CAIRN_CHECKOUT으로 실제 reader 검증 선택")
    def test_real_cairn_reader_accepts_bindings_and_rejects_invalid_boundaries(self):
        cairn = Path(os.environ["CAIRN_CHECKOUT"]).resolve()
        old, _ = self.issue()
        new, _ = self.issue("with-w16", "--include-w16-namespace-delete")
        script = Path(self._tmp.name) / "reader.mts"
        script.write_text(r'''
import assert from 'node:assert/strict';
import { readFile, writeFile, cp, rm } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const api = await import(pathToFileURL(process.argv[2]).href);
const [oldDir, newDir, badDir] = process.argv.slice(3);
const oldBoundary = await api.loadSealedApprovalBoundary(oldDir);
const boundary = await api.loadSealedApprovalBoundary(newDir);
const d20 = api.bindingForStage(boundary, 'W10-D20-destructive-apply');
const w16 = api.bindingForStage(boundary, 'W16-namespace-delete');
assert.equal(d20.bundle_digest, w16.bundle_digest);
assert.equal(d20.approval_boundary_digest, w16.approval_boundary_digest);
assert.notEqual(d20.gate_id, w16.gate_id);
for (const binding of [d20, w16]) {
  assert.deepEqual(await api.revalidateApprovalBoundaryBinding(newDir, binding, binding.stage_key), binding);
  assert.equal(boundary.stages[binding.stage_key].authorized, false);
}
const code = (expected) => (e) => e.code === expected;
assert.throws(() => api.bindingForStage(oldBoundary, 'W16-namespace-delete'), code('W10_APPROVAL_BOUNDARY_STAGE_MISSING'));
assert.throws(() => api.assertApprovalBoundaryBinding(boundary, api.bindingForStage(oldBoundary, d20.stage_key), d20.stage_key), code('W10_APPROVAL_BOUNDARY_BINDING_MISMATCH'));
assert.throws(() => api.assertApprovalBoundaryBinding(boundary, { ...w16, gate_id: d20.gate_id }, w16.stage_key), code('W10_APPROVAL_BOUNDARY_BINDING_MISMATCH'));
assert.throws(() => api.assertApprovalBoundaryBinding(boundary, w16, d20.stage_key), code('W10_APPROVAL_STAGE_MISMATCH'));
const original = JSON.parse(await readFile(`${newDir}/approval-boundary.json`, 'utf8'));
for (const [kind, mutate, expected] of [
  ['preauthorized', (d) => d.stages[3].authorized = true, 'W10_APPROVAL_BOUNDARY_PREAUTHORIZED'],
  ['missing-required', (d) => d.stages.splice(1, 1), 'W10_APPROVAL_BOUNDARY_STAGE_MISSING'],
  ['unknown', (d) => d.stages[3].stage = 'W17-delete', 'W10_APPROVAL_BOUNDARY_STAGE_UNKNOWN'],
  ['duplicate', (d) => d.stages[3] = d.stages[0], 'W10_APPROVAL_BOUNDARY_STAGE_DUPLICATE'],
  ['bad-id', (d) => d.stages[3].approval_id = 'not-an-id', 'W10_APPROVAL_ID_INVALID'],
  ['empty-invariant', (d) => d.stages[3].invariant = '', 'W10_APPROVAL_BOUNDARY_STAGE_INVALID'],
]) {
  const changed = structuredClone(original); mutate(changed);
  assert.throws(() => api.decodeApprovalBoundary(changed, w16.bundle_digest, w16.approval_boundary_digest), code(expected), kind);
}
await cp(newDir, badDir, { recursive: true });
await writeFile(`${badDir}/approval-boundary.json`, JSON.stringify({ ...original, note: 'tampered fixture' }));
await assert.rejects(() => api.loadSealedApprovalBoundary(badDir), code('W10_APPROVAL_BOUNDARY_DIGEST_MISMATCH'));
await rm(`${badDir}/approval-boundary.json`);
await assert.rejects(() => api.loadSealedApprovalBoundary(badDir), code('W10_APPROVAL_BOUNDARY_MISSING'));
console.log(JSON.stringify({ status: 'PASS', d20, w16, negative_cases: 12, database_access: false }));
''')
        result = subprocess.run([
            "node", "--import", str(cairn / "node_modules/tsx/dist/loader.mjs"), str(script),
            str(cairn / "lib/artifact-reconciliation/approval-boundary.ts"),
            str(old), str(new), str(self.output_dir / "tampered-fixture"),
        ], capture_output=True, text=True, cwd=cairn)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print("cairn-reader=" + result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
