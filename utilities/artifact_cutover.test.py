#!/usr/bin/env python3
"""G2/G3/G4 executor tests for `artifact_cutover.py` on a synthetic legacy root."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_manifest as M  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_cutover as C  # noqa: E402

_S = importlib.util.spec_from_file_location("route_for_cutover_test", Path(__file__).with_name("capability-route.py"))
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)
ALL = ["atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
       "no-artifact-handoff", "no-independent-verifier", "focused-verification"]
REPO_ID, ROOT_ID = "repo_" + "a" * 32, "root_" + "b" * 32
W7_REF = "ref_" + "7" * 32
W7_RREV = "rrev_" + "7" * 32


class CutoverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "artifact-root"
        self.root.mkdir()
        home = base / "agent-home"
        (home / "core").mkdir(parents=True)
        (home / "core" / "CORE.md").write_text("fixture\n")
        self._env = {k: os.environ.get(k) for k in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR")}
        os.environ["AGENT_HOME"] = str(home)
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("AGENT_ARTIFACT_CYCLE_DIR", None)
        self.addCleanup(self._restore)
        # legacy population
        self.w("plans/2026-01-01_a/plan.md", "plan a\n")
        self.w("plans/2026-01-02_b/final_report.md", "report b\n")
        self.w("research/topic/report.md", "research\n")
        self.w("spec/prd.md", "# prd v2\n")
        self.w("spec/comp/prd.md", "# comp prd\n")
        self.w("analysis_project/code/overview.md", "analysis\n")
        self.w("plans/keep/self-write.md", "excluded\n")
        self.w("research/topic/.gitignore", "*.tmp\n")
        self.w("research/topic/_internal/notes.md", "internal\n")
        # W7-style relocated shared spec (no reference.json) and its map
        self.w(f"shared/spec/{W7_REF}/revisions/{W7_RREV}/prd.md", "# prd v1\n")
        self.w("plans/2026-01-01_a/old.md", "old\n")
        self.w("campaigns/camp_x/cycles/cyc_x/artifacts/plans/2026-01-01_a/old.md", "old\n")
        self.w7_map = self.root / ".runtime" / "w7-map.jsonl"
        self.w7_map.parent.mkdir(parents=True, exist_ok=True)
        self.w7_map.write_text(json.dumps({"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "plans/2026-01-01_a/old.md",
                                           "target_locator": "campaigns/camp_x/cycles/cyc_x/artifacts/plans/2026-01-01_a/old.md"}) + "\n"
                               + json.dumps({"schema_version": C.MAP_SCHEMA, "kind": "directory", "source_locator": "plans/2026-01-01_a",
                                             "target_locator": "campaigns/camp_x/cycles/cyc_x/artifacts/plans/2026-01-01_a"}) + "\n")
        P.activate(self.root, repository_id=REPO_ID, artifact_root_id=ROOT_ID)
        self.rows = base / "census-rows.jsonl"
        rows = [
            {"path": "plans/2026-01-01_a/plan.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:plans"},
            {"path": "plans/2026-01-02_b/final_report.md", "kind": "file", "disposition": "post-w7-arrival", "detail": "cycle-candidate:plans"},
            {"path": "research/topic/report.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:research"},
            {"path": "research/topic/.gitignore", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:research"},
            {"path": "research/topic/_internal/notes.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:research"},
            {"path": "spec/prd.md", "kind": "file", "disposition": "after-cutoff-after_cutoff_drift", "detail": "cycle-candidate:spec"},
            {"path": "plans/keep/self-write.md", "kind": "file", "disposition": "post-w7-arrival", "detail": "cycle-candidate:plans"},
            {"path": "plans/2026-01-01_a/old.md", "kind": "file", "disposition": "w7-source-preserved", "detail": "cycle-candidate:plans"},
        ]
        self.rows.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def w(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def route(self, mode="dev"):
        gate = {"spec_read": {"satisfied": True, "source": "fixture"}, "drift_verdict": "within-spec",
                "workflow_mode": "tracked", "artifact_guard": {"satisfied": True, "source": "fixture"}}
        route = R.compile_route("autopilot-code", mode, "direct", R.ROOT, self.root, predicates=ALL, transport=None,
                                inline_reason="atomic-direct", tracking="tracked", tracked_gate_evidence=gate)
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def close(self, route, route_file):
        ev = Path(self._tmp.name) / "ev.txt"
        ev.write_text("evidence\n")
        for node in route["nodes"]:
            if node.get("terminal"):
                R.write_completion_marker(route, node, node["id"], ev)
        R.close_route(route, route_file, commit="a" * 40, summary="fixture")

    def approve_retire(self, dry_run_report, *, root_id=None):
        self._retire_approval_ordinal = getattr(self, "_retire_approval_ordinal", 0) + 1
        approval_path = Path(self._tmp.name) / f"retire-approval-{self._retire_approval_ordinal}.json"
        approval_path.write_text(json.dumps({
            "authorized": True,
            "body": {
                "root_id": root_id or P.artifact_lifecycle.read_root_identity(self.root).artifact_root_id,
                "retire_inventory_sha256": dry_run_report["inventory_sha256"],
            },
        }))
        return approval_path

    def migrate(self):
        route, route_file = self.route()
        report = C.migrate_delta(self.root, census_rows=self.rows, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", excludes=["plans/keep"], approval_receipt_sha256="x" * 64, campaign_id=None)
        self.close(route, route_file)
        sealed = C.migrate_seal(self.root, run_dir=Path(report["run_dir"]), spec_reference=W7_REF)
        return report, sealed

    def test_migrate_delta_copies_candidates_and_snapshots_shared(self):
        report, sealed = self.migrate()
        self.assertEqual(report["copied_by_bucket"], {"plans": 2, "research": 2})  # w7-source-preserved rows are not re-copied
        self.assertEqual(report["skipped_hidden_components"], ["research/topic/.gitignore"])
        self.assertTrue((Path(report["cycle_dir"]) / "artifacts/research/topic/_internal/notes.md").is_file())
        self.assertFalse((Path(report["cycle_dir"]) / "artifacts/research/topic/.gitignore").exists())
        self.assertEqual(report["shared_snapshots"], {"spec": 2, "analysis": 1})
        self.assertEqual(report["excluded_files"], 1)
        cycle_dir = Path(report["cycle_dir"])
        self.assertTrue((cycle_dir / "artifacts/plans/2026-01-02_b/final_report.md").is_file())
        self.assertTrue((cycle_dir / "artifacts/research/topic/report.md").is_file())
        self.assertFalse((cycle_dir / "artifacts/plans/keep").exists())
        self.assertTrue((self.root / "plans/2026-01-01_a/plan.md").is_file(), "sources preserved")
        self.assertEqual(sealed["state"], "sealed")
        self.assertEqual(sealed["finalize"]["status"], "sealed")
        self.assertTrue((cycle_dir / "manifest.json").is_file())
        spec_adm = sealed["shared_admissions"]["spec"]
        self.assertEqual(spec_adm["shared_reference_id"], W7_REF)
        self.assertFalse(spec_adm["reference_created"])
        rev = Path(spec_adm["revision_dir"])
        self.assertTrue((rev / "prd.md").is_file() and (rev / "comp/prd.md").is_file())
        ref = json.loads((self.root / "shared/spec" / W7_REF / "reference.json").read_text())
        self.assertEqual(ref["revisions"][0], W7_RREV)
        self.assertEqual(ref["latest_revision_id"], spec_adm["shared_reference_revision_id"])
        self.assertIn("analysis", sealed["shared_admissions"])
        mapping = C._read_jsonl(Path(report["run_dir"]) / "compatibility-map.jsonl")
        by_src = {r["source_locator"]: r["target_locator"] for r in mapping}
        self.assertEqual(by_src["spec/prd.md"], os.path.relpath(rev, self.root) + "/prd.md")
        self.assertTrue(by_src["research/topic/report.md"].startswith("campaigns/"))
        self.assertNotIn("shared/research", by_src["research/topic/report.md"])
        self.assertTrue(C._read_jsonl(Path(report["run_dir"]) / "journal.jsonl"))

    def test_migrate_requires_active_cutover(self):
        P.cutover_path(self.root).unlink()
        with self.assertRaises(C.CutoverError) as ctx:
            C.migrate_delta(self.root, census_rows=self.rows, route_file=self.route()[1], capability="autopilot-code",
                            intensity="direct", excludes=[], approval_receipt_sha256=None, campaign_id=None)
        self.assertEqual(ctx.exception.code, "cutover-inactive")

    def test_compat_close_and_resolve_legacy(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        body = C.compat_close(self.root, maps=[self.w7_map, w7c_map], approval_receipt_sha256=None)
        self.assertEqual(body["compatibility_window"], "closed")
        present = C.resolve_legacy(self.root, "spec/prd.md")
        self.assertEqual(present["resolution"], "present")
        (self.root / "spec/prd.md").unlink()
        mapped = C.resolve_legacy(self.root, "spec/prd.md")
        self.assertEqual(mapped["resolution"], "mapped")
        self.assertTrue(mapped["absolute"].endswith("/prd.md") and "/shared/spec/" in mapped["absolute"])
        (self.root / "plans/2026-01-01_a/old.md").unlink()
        anc = C.resolve_legacy(self.root, "plans/2026-01-01_a/old.md")
        self.assertIn(anc["resolution"], ("mapped", "mapped-ancestor"))
        self.assertEqual(C.resolve_legacy(self.root, "plans/none.md")["resolution"], "unresolved")

    def test_prd_candidates_prefer_latest_shared_revision_over_legacy(self):
        # The fixture root already carries a shared/spec revision: it governs
        # over the legacy bucket even before the migration (defect K order).
        before = C.prd_candidates(self.root)
        self.assertTrue(before and all(c.startswith(str(self.root / "shared/spec")) for c in before), before)
        report, sealed = self.migrate()
        rev = Path(sealed["shared_admissions"]["spec"]["revision_dir"])
        # Defect K: while the legacy bucket still exists, the shared revision governs.
        self.assertTrue((self.root / "spec/prd.md").is_file())
        self.assertEqual(C.prd_candidates(self.root), [str(rev / "prd.md"), str(rev / "comp/prd.md")])
        import shutil
        shutil.rmtree(self.root / "spec")
        self.assertEqual(C.prd_candidates(self.root), [str(rev / "prd.md"), str(rev / "comp/prd.md")])

    def test_retire_verifies_backs_up_and_deletes(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        # divergent source: changed after migration -> must be kept
        (self.root / "plans/2026-01-02_b/final_report.md").write_text("changed later\n")
        backup = Path(self._tmp.name) / "backup"
        dry = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                       approval_receipt_sha256=None, dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertTrue((self.root / "spec/prd.md").is_file())
        approval_path = self.approve_retire(dry)
        out = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                       approval_receipt_sha256="y" * 64, approval_path=approval_path)
        self.assertEqual(out["kept_files"], 1)
        self.assertEqual(out["kept"][0]["source"], "plans/2026-01-02_b/final_report.md")
        self.assertFalse((self.root / "spec/prd.md").exists())
        self.assertFalse((self.root / "plans/2026-01-01_a").exists())
        self.assertFalse((self.root / "research/topic/report.md").exists())
        self.assertTrue((self.root / "research/topic/.gitignore").is_file(), "unmigrated hidden file is kept")
        self.assertFalse((self.root / "analysis_project").exists())
        self.assertTrue((self.root / "plans/keep/self-write.md").is_file())
        self.assertTrue((self.root / "plans/2026-01-02_b/final_report.md").is_file())
        self.assertTrue(Path(out["backup_seal"]["archive"]).is_file())
        import tarfile
        with tarfile.open(out["backup_seal"]["archive"]) as tar:
            self.assertIn("spec/prd.md", tar.getnames())
        self.assertEqual(out["retired_files"], out["verified_files"])
        # targets intact and index still verifies
        self.assertTrue((Path(sealed["shared_admissions"]["spec"]["revision_dir"]) / "prd.md").is_file())
        self.assertTrue(adm.verify_index(self.root).ok)
        # canonical prd now resolves to the shared revision
        self.assertTrue(C.prd_candidates(self.root)[0].startswith(str(self.root / "shared/spec")))

    def test_seal_prunes_hidden_copies_left_by_an_earlier_run(self):
        route, route_file = self.route()
        report = C.migrate_delta(self.root, census_rows=self.rows, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", excludes=["plans/keep"], approval_receipt_sha256=None, campaign_id=None)
        run_dir = Path(report["run_dir"])
        stray = Path(report["cycle_dir"]) / "artifacts/research/topic/.gitignore"
        stray.write_text("*.tmp\n")
        rel = os.path.relpath(stray, self.root)
        with open(run_dir / "journal.jsonl", "a") as fh:
            fh.write(json.dumps({"schema_version": C.JOURNAL_SCHEMA, "row_ordinal": 999, "action": "create_destination", "kind": "file",
                                 "source_locator": "research/topic/.gitignore", "target_locator": rel, "sha256": "x", "size": 6}) + "\n")
        with open(run_dir / "compatibility-map.jsonl", "a") as fh:
            fh.write(json.dumps({"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "research/topic/.gitignore", "target_locator": rel}) + "\n")
        self.close(route, route_file)
        sealed = C.migrate_seal(self.root, run_dir=run_dir, spec_reference=W7_REF)
        self.assertEqual(sealed["state"], "sealed")
        self.assertEqual(sealed["pruned_hidden_copies"], ["research/topic/.gitignore"])
        self.assertFalse(stray.exists())
        self.assertFalse(any(".gitignore" in r["target_locator"] for r in C._read_jsonl(run_dir / "compatibility-map.jsonl")))

    def test_migrate_delta_skips_invalid_locator_components(self):
        # Non-ASCII filenames cannot be D-6 locators; they must stay legacy at
        # copy time instead of wedging the later seal (SR_CorrNet_DSC 2026-09-02).
        self.w("plans/2026-01-03_c/핵심요약보고서.html", "korean-named\n")
        self.w("analysis_project/doc/검증결과.md", "korean snapshot\n")
        rows = self.rows.read_text() + json.dumps(
            {"path": "plans/2026-01-03_c/핵심요약보고서.html", "kind": "file",
             "disposition": "post-w7-arrival", "detail": "cycle-candidate:plans"}) + "\n"
        self.rows.write_text(rows)
        report, sealed = self.migrate()
        self.assertEqual(sorted(report["skipped_invalid_components"]),
                         ["analysis_project/doc/검증결과.md", "plans/2026-01-03_c/핵심요약보고서.html"])
        cycle_dir = Path(report["cycle_dir"])
        self.assertFalse((cycle_dir / "artifacts/plans/2026-01-03_c").exists())
        self.assertFalse((cycle_dir / "artifacts/shared-input/analysis/doc/검증결과.md").exists())
        self.assertTrue((self.root / "plans/2026-01-03_c/핵심요약보고서.html").is_file(), "source preserved")
        self.assertEqual(sealed["state"], "sealed")

    def test_seal_prunes_invalid_locator_copies_left_by_an_earlier_run(self):
        route, route_file = self.route()
        report = C.migrate_delta(self.root, census_rows=self.rows, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", excludes=["plans/keep"], approval_receipt_sha256=None, campaign_id=None)
        run_dir = Path(report["run_dir"])
        self.w("research/topic/검증결과.md", "korean source\n")
        stray = Path(report["cycle_dir"]) / "artifacts/research/topic/검증결과.md"
        stray.write_text("korean source\n")
        rel = os.path.relpath(stray, self.root)
        with open(run_dir / "journal.jsonl", "a") as fh:
            fh.write(json.dumps({"schema_version": C.JOURNAL_SCHEMA, "row_ordinal": 999, "action": "create_destination", "kind": "file",
                                 "source_locator": "research/topic/검증결과.md", "target_locator": rel, "sha256": "x", "size": 14}) + "\n")
        with open(run_dir / "compatibility-map.jsonl", "a") as fh:
            fh.write(json.dumps({"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "research/topic/검증결과.md", "target_locator": rel}) + "\n")
        self.close(route, route_file)
        sealed = C.migrate_seal(self.root, run_dir=run_dir, spec_reference=W7_REF)
        self.assertEqual(sealed["state"], "sealed")
        self.assertIn("research/topic/검증결과.md", sealed["pruned_hidden_copies"])
        self.assertIn("research/topic/검증결과.md", sealed.get("skipped_invalid_components", []))
        self.assertFalse(stray.exists())
        self.assertTrue((self.root / "research/topic/검증결과.md").is_file(), "source preserved")
        self.assertFalse(any("검증결과" in r["target_locator"] for r in C._read_jsonl(run_dir / "compatibility-map.jsonl")))

    def test_adopt_campaign_lists_existing_cycles_and_allows_join(self):
        camp = "camp_" + "e" * 32
        cyc = "cyc_" + "e" * 32
        (self.root / "campaigns" / camp / "cycles" / cyc / "artifacts").mkdir(parents=True)
        record = C.adopt_campaign(self.root, camp, title="w7", goal="g")
        self.assertEqual(record["cycles"], [cyc])
        self.assertEqual(C.adopt_campaign(self.root, camp, title="x", goal="y")["title"], "w7")
        route, route_file = self.route("debug")
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct", campaign_id=camp)
        self.assertEqual(begun["campaign_id"], camp)
        self.assertEqual(P.read_campaign(self.root, camp)["cycles"], [cyc, begun["cycle_id"]])
        with self.assertRaises(C.CutoverError) as ctx:
            C.adopt_campaign(self.root, "camp_" + "f" * 32, title="x", goal="y")
        self.assertEqual(ctx.exception.code, "campaign-dir-missing")

    def _external_cycle(self):
        camp = "camp_" + "7" * 32
        cyc = "cyc_" + "7" * 32
        base = self.root / "campaigns" / camp / "cycles" / cyc / "artifacts"
        self.w(f"campaigns/{camp}/cycles/{cyc}/artifacts/plans/2026-01-01_x/plan.md", "relocated plan\n")
        self.w(f"campaigns/{camp}/cycles/{cyc}/artifacts/plans/2026-01-01_x/_internal/note.md", "internal\n")
        self.w(f"campaigns/{camp}/cycles/{cyc}/artifacts/documents/d.md", "doc\n")
        self.w(f"campaigns/{camp}/cycles/{cyc}/artifacts/plans/2026-01-01_x/.claude/settings.json", "{}\n")
        self.w(f"campaigns/{camp}/cycles/{cyc}/artifacts/plans/2026-01-01_x/{'l' * 140}.txt", "long\n")
        C.adopt_campaign(self.root, camp, title="w7", goal="g")
        return camp, cyc, base.parent

    def test_seal_legacy_cycle_builds_manifest_without_touching_bytes(self):
        camp, cyc, cycle_dir = self._external_cycle()
        before = C._tree_digest(cycle_dir / "artifacts")
        route, route_file = self.route("debug")
        with self.assertRaises(C.CutoverError) as ctx:
            C.seal_legacy_cycle(self.root, cycle_dir=cycle_dir, route_file=route_file)
        self.assertEqual(ctx.exception.code, "route-not-closed")
        self.assertIsNone(P.read_cycle_record(self.root, cyc))
        self.close(route, route_file)
        with self.assertRaises(C.CutoverError) as ctx:  # hidden residue fails D-6 unless excluded
            C.seal_legacy_cycle(self.root, cycle_dir=cycle_dir, route_file=route_file)
        self.assertIn("locator-hidden-component", ctx.exception.detail)
        self.assertIsNone(P.read_cycle_record(self.root, cyc))
        result = C.seal_legacy_cycle(self.root, cycle_dir=cycle_dir, route_file=route_file,
                                     title="W7 relocation", started_on="2026-08-25T00:00:00Z",
                                     primary="plans/2026-01-01_x/plan.md", exclude_hidden=True)
        self.assertEqual(result["status"], "sealed")
        self.assertEqual(result["artifact_count"], 3)
        self.assertEqual(result["hidden_excluded"], 2)
        excluded = P.read_cycle_record(self.root, cyc)["adopted"]["hidden_excluded"]
        self.assertEqual(sorted(row["reason"] for row in excluded), ["hidden-component", "invalid-component"])
        self.assertTrue(all(row["sha256"] and row["byte_size"] for row in excluded))
        self.assertTrue(result["bytes_unchanged"])
        self.assertEqual(result["tree"], before)
        self.assertEqual(C._tree_digest(cycle_dir / "artifacts"), before)
        record = P.read_cycle_record(self.root, cyc)
        self.assertEqual((record["state"], record["route_id"], record["started_on"]),
                         ("sealed", route["route_id"], "2026-08-25T00:00:00Z"))
        self.assertEqual(record["adopted"]["kind"], "seal-legacy-cycle")
        document = json.loads((cycle_dir / "manifest.json").read_text())
        self.assertTrue(M.validate(document).ok)
        self.assertEqual(len(document["artifact_revisions"]), 3)
        self.assertIn(cyc, adm.load_index(self.root).manifests)
        self.assertTrue(adm.verify_index(self.root).ok)
        with self.assertRaises(C.CutoverError) as ctx:
            C.seal_legacy_cycle(self.root, cycle_dir=cycle_dir, route_file=route_file)
        self.assertEqual(ctx.exception.code, "manifest-already-present")
        self.assertEqual(P.check_write(self.root, cycle_dir / "artifacts" / "new.md")["reason"], "cycle-not-open")

    def test_seal_legacy_cycle_rejects_bad_shapes(self):
        route, route_file = self.route("debug")
        self.close(route, route_file)
        with self.assertRaises(C.CutoverError) as ctx:
            C.seal_legacy_cycle(self.root, cycle_dir=self.root / "plans", route_file=route_file)
        self.assertEqual(ctx.exception.code, "cycle-dir-shape-invalid")
        camp, cyc, cycle_dir = self._external_cycle()
        with self.assertRaises(C.CutoverError) as ctx:
            C.seal_legacy_cycle(self.root, cycle_dir=cycle_dir, route_file=route_file, capability="autopilot-spec")
        self.assertEqual(ctx.exception.code, "route-capability-mismatch")
        self.assertIsNone(P.read_cycle_record(self.root, cyc))

    def test_retire_refuses_backup_inside_root(self):
        report, sealed = self.migrate()
        with self.assertRaises(C.CutoverError) as ctx:
            C.retire(self.root, maps=[self.w7_map], backup_root=self.root / "_scratch", excludes=[], approval_receipt_sha256=None)
        self.assertEqual(ctx.exception.code, "backup-root-inside-artifact-root")

    # -- A-16.4: compat map chain (append-only) -----------------------------

    def test_a16_4_compat_append_preserves_old_rows_and_grows_maps_by_one(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        closed = C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        self.assertEqual(len(closed["maps"]), 1)
        appended = C.compat_append(self.root, maps=[w7c_map], approval_receipt_sha256=None)
        self.assertEqual(len(appended["maps"]), 2)
        self.assertEqual(appended["maps"][0]["path"], closed["maps"][0]["path"])
        self.assertEqual(appended["maps"][0]["sha256"], closed["maps"][0]["sha256"])
        self.assertEqual(appended["maps"][0]["rows"], closed["maps"][0]["rows"])
        self.assertEqual(appended["maps"][1]["path"], str(w7c_map.resolve()))

    def test_a16_4_old_map_file_sha_unchanged(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        real_sha = C._sha
        seen = []

        def spy(p):
            seen.append(Path(p).resolve())
            return real_sha(p)

        with mock.patch.object(C, "_sha", side_effect=spy):
            appended = C.compat_append(self.root, maps=[w7c_map], approval_receipt_sha256=None)
        self.assertNotIn(self.w7_map.resolve(), seen, "old map file must never be opened for its digest")
        self.assertIn(w7c_map.resolve(), seen)
        self.assertEqual(appended["maps"][0]["sha256"], real_sha(self.w7_map))

    def test_a16_4_superseded_by_and_at_recorded_on_old_entry(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        appended = C.compat_append(self.root, maps=[w7c_map], supersedes=[str(self.w7_map.resolve())],
                                   approval_receipt_sha256=None)
        old, new = appended["maps"]
        self.assertEqual(old["superseded_by"], new["sha256"])
        self.assertTrue(old["superseded_at"])
        self.assertNotIn("superseded_by", new)

    def test_a16_4_resolve_legacy_prefers_new_map(self):
        C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        new_target_dir = self.root / "campaigns/camp_y/cycles/cyc_y/artifacts/plans/2026-01-01_a"
        new_target_dir.mkdir(parents=True)
        (new_target_dir / "old.md").write_text("old\n")
        new_map = Path(self._tmp.name) / "w7g-map.jsonl"
        new_map.write_text(json.dumps({
            "schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "plans/2026-01-01_a/old.md",
            "target_locator": "campaigns/camp_y/cycles/cyc_y/artifacts/plans/2026-01-01_a/old.md"}) + "\n")
        C.compat_append(self.root, maps=[new_map], supersedes=[str(self.w7_map.resolve())],
                        approval_receipt_sha256=None)
        (self.root / "plans/2026-01-01_a/old.md").unlink()
        resolved = C.resolve_legacy(self.root, "plans/2026-01-01_a/old.md")
        self.assertEqual(resolved["resolution"], "mapped")
        self.assertEqual(resolved["target"], "campaigns/camp_y/cycles/cyc_y/artifacts/plans/2026-01-01_a/old.md")

    def test_a16_4_retire_uses_latest_map_target(self):
        new_target_dir = self.root / "campaigns/camp_y/cycles/cyc_y/artifacts/plans/2026-01-01_a"
        new_target_dir.mkdir(parents=True)
        (new_target_dir / "old.md").write_text("old\n")
        new_map = Path(self._tmp.name) / "w7g-map.jsonl"
        new_map.write_text(json.dumps({
            "schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "plans/2026-01-01_a/old.md",
            "target_locator": "campaigns/camp_y/cycles/cyc_y/artifacts/plans/2026-01-01_a/old.md"}) + "\n")
        dry = C.retire(self.root, maps=[self.w7_map, new_map], backup_root=Path(self._tmp.name) / "backup-a16-4",
                       excludes=[], approval_receipt_sha256=None, dry_run=True)
        row = next(r for r in dry["verified_sample"] if r["source"] == "plans/2026-01-01_a/old.md")
        self.assertEqual(row["target"], "campaigns/camp_y/cycles/cyc_y/artifacts/plans/2026-01-01_a/old.md")

    def test_a16_4_map_paths_are_in_retire_excludes(self):
        C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        dry = C.retire(self.root, maps=[self.w7_map], backup_root=Path(self._tmp.name) / "backup-a16-4b",
                       excludes=[], approval_receipt_sha256=None, dry_run=True)
        rel = self.w7_map.resolve().relative_to(self.root).as_posix()
        self.assertIn(rel, dry["excluded_prefixes"])

    def test_a16_4_append_allowed_in_closed_window(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        closed = C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        self.assertEqual(closed["compatibility_window"], "closed")
        appended = C.compat_append(self.root, maps=[w7c_map], approval_receipt_sha256=None)
        self.assertEqual(appended["compatibility_window"], "closed")
        self.assertEqual(len(appended["maps"]), 2)

    def test_a16_4_missing_recorded_map_is_typed_hold_on_write_surfaces(self):
        C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        self.w7_map.unlink()
        self.assertEqual(C.resolve_legacy(self.root, "plans/none.md")["resolution"], "unresolved")
        new_map = Path(self._tmp.name) / "w7g-map2.jsonl"
        new_map.write_text("")
        with self.assertRaises(C.CutoverError) as ctx:
            C.compat_append(self.root, maps=[new_map], approval_receipt_sha256=None)
        self.assertEqual(ctx.exception.code, "compat-map-missing")

    # -- A-16.6: retire (R4) --------------------------------------------------

    def test_a16_6_retire_refused_without_approval(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        with self.assertRaises(C.CutoverError) as ctx:
            C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=Path(self._tmp.name) / "backup-a16-6a",
                     excludes=["plans/keep"], approval_receipt_sha256=None)
        self.assertEqual(ctx.exception.code, "retire-approval-required")

    def test_a16_6_digest_mismatch_is_kept(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        (self.root / "plans/2026-01-02_b/final_report.md").write_text("changed later\n")
        dry = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=Path(self._tmp.name) / "backup-a16-6b",
                       excludes=["plans/keep"], approval_receipt_sha256=None, dry_run=True)
        self.assertEqual(dry["kept"][0]["source"], "plans/2026-01-02_b/final_report.md")
        self.assertEqual(dry["kept"][0]["reason"], "no-target-with-identical-digest")

    def test_a16_6_no_unlink_before_backup_seal_and_reread(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        backup = Path(self._tmp.name) / "backup-a16-6c"
        dry = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                       approval_receipt_sha256=None, dry_run=True)
        approval_path = self.approve_retire(dry)
        with self.assertRaises(C.CutoverError) as ctx:
            C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                     approval_receipt_sha256="z" * 64, approval_path=approval_path, crash_after_phase="backup-sealed")
        self.assertEqual(ctx.exception.code, "crash-fixture")
        self.assertTrue((self.root / "spec/prd.md").is_file(), "no source unlinked before seal+reread")
        self.assertTrue((self.root / "research/topic/report.md").is_file())

    def test_a16_6_archive_tamper_after_seal_is_typed_failure_zero_unlinks(self):
        # 🔴1 (R4 half): the re-read must recompute the archive digest and
        # compare against the sealed one, not just check tar member names.
        # Simulates the archive changing between the seal write and the
        # re-read (the exact window `backup-seal.json` -> re-read covers) by
        # making the second `_sha(archive)` call (the re-read) disagree with
        # the first (the one recorded in the seal).
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        backup = Path(self._tmp.name) / "backup-a16-6g"
        dry = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                       approval_receipt_sha256=None, dry_run=True)
        approval_path = self.approve_retire(dry)
        before_present = (self.root / "spec/prd.md").is_file() and (self.root / "research/topic/report.md").is_file()
        self.assertTrue(before_present)
        real_sha = C._sha
        archive_calls = {"n": 0, "archive": None}

        def tamper_reread(path):
            p = Path(path)
            if p.suffix == ".gz" and p.name == "retired-sources.tar.gz":
                archive_calls["n"] += 1
                if archive_calls["n"] == 2:  # the re-read call
                    return "0" * 64
            return real_sha(p)

        with mock.patch.object(C, "_sha", side_effect=tamper_reread):
            with self.assertRaises(C.CutoverError) as ctx:
                C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                         approval_receipt_sha256="v" * 64, approval_path=approval_path)
        self.assertEqual(ctx.exception.code, "backup-incomplete")
        self.assertEqual(archive_calls["n"], 2)
        self.assertTrue((self.root / "spec/prd.md").is_file(), "zero unlinks after a tampered re-read")
        self.assertTrue((self.root / "research/topic/report.md").is_file())

    def test_a16_6_exclusion_list_preserved(self):
        C.compat_close(self.root, maps=[self.w7_map], approval_receipt_sha256=None)
        dry = C.retire(self.root, maps=[self.w7_map], backup_root=Path(self._tmp.name) / "backup-a16-6d",
                       excludes=["plans/keep"], approval_receipt_sha256=None, dry_run=True)
        for prefix in C.RETIRE_DEFAULT_EXCLUDES:
            self.assertIn(prefix, dry["excluded_prefixes"])
        self.assertIn("plans/keep", dry["excluded_prefixes"])

    def test_a16_6_dry_run_twice_byte_identical(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        kwargs = dict(maps=[self.w7_map, w7c_map], backup_root=Path(self._tmp.name) / "backup-a16-6e",
                     excludes=["plans/keep"], approval_receipt_sha256=None, dry_run=True)
        first = C.retire(self.root, **kwargs)
        second = C.retire(self.root, **kwargs)
        volatile = {"created_at", "run_dir", "backup_dir"}
        self.assertEqual({k: v for k, v in first.items() if k not in volatile},
                         {k: v for k, v in second.items() if k not in volatile})

    def test_a16_6_approval_stale_on_inventory_drift(self):
        report, sealed = self.migrate()
        w7c_map = Path(report["run_dir"]) / "compatibility-map.jsonl"
        backup = Path(self._tmp.name) / "backup-a16-6f"
        dry = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                       approval_receipt_sha256=None, dry_run=True)
        approval_path = self.approve_retire(dry)
        (self.root / "plans/2026-01-02_b/final_report.md").write_text("drifted after approval\n")
        with self.assertRaises(C.CutoverError) as ctx:
            C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                     approval_receipt_sha256="w" * 64, approval_path=approval_path)
        self.assertEqual(ctx.exception.code, "approval-stale")


class CloseoutTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "artifact-root"
        self.root.mkdir()
        home = base / "agent-home"
        (home / "core").mkdir(parents=True)
        (home / "core" / "CORE.md").write_text("fixture\n")
        self._env = {key: os.environ.get(key) for key in (
            "AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR"
        )}
        os.environ["AGENT_HOME"] = str(home)
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("AGENT_ARTIFACT_CYCLE_DIR", None)
        self.addCleanup(self._restore)
        P.activate(self.root, repository_id=REPO_ID, artifact_root_id=ROOT_ID)
        self.core_file = Path(self._tmp.name) / "CORE.md"
        rows = [
            (f"| `{path}/` | fixture | `C-LEG(sealed-evidence)` |"
             if path != "research/hermes-agent/.gitignore"
             else f"| `{path}` | fixture | `C-LEG(sealed-evidence)` |")
            + f" `{C.SEALED_EVIDENCE_CAMPAIGN}/{C.SEALED_EVIDENCE_RELOCATIONS[path]}/` |"
            for path in C.SEALED_EVIDENCE_PATHS
        ]
        self.core_file.write_text("\n".join(rows) + "\n", encoding="utf-8")
        for path in C.SEALED_EVIDENCE_PATHS:
            if path.endswith(".gitignore"):
                self.w(path, "*.tmp\n")
            else:
                self.w(path + "/sealed.md", "sealed\n")
        self.w("plan.md", "root plan\n")
        self.w("notes/old.md", "old note\n")
        self.w("dev_logs/run.log", "run\n")
        (self.root / ".pipeline-lock").write_text("runtime\n", encoding="utf-8")
        self.dead_route, self.dead_route_file = self.route("dev")
        self.residue_route, self.residue_route_file = self.route("debug")
        alias = self.root / ".runtime" / "routes" / "legacy-alias.json"
        alias.write_bytes(self.dead_route_file.read_bytes())
        self.sync_route_id = "rt-f06e4a05a1bb924d"
        sync_outcome = self.root / ".runtime" / "routes" / f"{self.sync_route_id}.outcome.json"
        sync_outcome.write_text(json.dumps({
            "schema_version": 3,
            "route_id": self.sync_route_id,
            "route_hash": "sha256:" + "f" * 64,
            "terminal_gate_proven": True,
        }) + "\n", encoding="utf-8")
        self.jobs = Path(self._tmp.name) / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.backup = Path(self._tmp.name) / "backup"

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def w(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def route(self, mode="dev"):
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec",
            "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        route = R.compile_route(
            "autopilot-code", mode, "direct", R.ROOT, self.root,
            predicates=ALL, transport=None, inline_reason="atomic-direct",
            tracking="tracked", tracked_gate_evidence=gate,
        )
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def packages(self):
        preserve = [self.residue_route["route_id"]]
        route_package = C.route_sweep_package(
            self.root, jobs=[self.jobs], preserve_routes=preserve
        )
        closeout_package = C.closeout_residue_package(
            self.root,
            jobs=[self.jobs],
            residue_route=self.residue_route_file,
            backup_root=self.backup,
            core_file=self.core_file,
            preserve_routes=preserve,
            self_cycles=[],
            sync_route_id=self.sync_route_id,
        )
        return route_package, closeout_package

    def approval(self, route_package, closeout_package, authorized):
        path = Path(self._tmp.name) / ("approval-true.json" if authorized else "approval-false.json")
        package = C.closeout_approval_package(
            self.root,
            route_package=route_package,
            closeout_package=closeout_package,
        )
        package["authorized"] = authorized
        path.write_bytes(C._canonical_bytes(package))
        return path

    def test_approval_package_is_canonical_false_and_embeds_exact_plans(self):
        route_package, closeout_package = self.packages()
        first = C.closeout_approval_package(
            self.root,
            route_package=route_package,
            closeout_package=closeout_package,
        )
        second = C.closeout_approval_package(
            self.root,
            route_package=route_package,
            closeout_package=closeout_package,
        )
        self.assertEqual(C._canonical_bytes(first), C._canonical_bytes(second))
        self.assertFalse(first["authorized"])
        self.assertEqual(first["route_sweep_package"], route_package)
        self.assertEqual(first["closeout_package"], closeout_package)
        self.assertEqual(first["source_file_count"], closeout_package["plan"]["totals"]["files"])

    def test_closeout_dry_runs_are_canonical_and_non_mutating(self):
        route_one, closeout_one = self.packages()
        route_two, closeout_two = self.packages()
        self.assertEqual(C._canonical_bytes(route_one), C._canonical_bytes(route_two))
        self.assertEqual(C._canonical_bytes(closeout_one), C._canonical_bytes(closeout_two))
        actions = {row["route_id"]: row["action"] for row in route_one["plan"]["actions"] if row["route_id"]}
        self.assertEqual(actions[self.dead_route["route_id"]], "close-abandoned")
        self.assertEqual(actions[self.residue_route["route_id"]], "preserve-explicit")
        selected = {row["path"] for row in closeout_one["plan"]["source_inventory"]}
        self.assertIn("plan.md", selected)
        self.assertIn("notes/old.md", selected)
        self.assertIn(".runtime/routes/legacy-alias.json", selected)
        self.assertNotIn(".pipeline-lock", selected)
        self.assertTrue(closeout_one["plan"]["apply_ready"], closeout_one["plan"])
        for path in C.SEALED_EVIDENCE_PATHS:
            self.assertTrue((self.root / path).exists())
        self.assertFalse(self.dead_route_file.with_name(self.dead_route_file.stem + ".outcome.json").exists())

    def test_core_sync_requires_relocation_locators_after_w7h(self):
        # PRD v15 §31 D-85-b: a CORE §3 table that still names only the legacy
        # prefixes is not synchronised once the evidence moved.
        legacy_only = [
            f"| `{path}/` | fixture | `C-LEG(sealed-evidence)` |"
            if path != "research/hermes-agent/.gitignore"
            else f"| `{path}` | fixture | `C-LEG(sealed-evidence)` |"
            for path in C.SEALED_EVIDENCE_PATHS
        ]
        stale = Path(self._tmp.name) / "CORE-stale.md"
        stale.write_text("\n".join(legacy_only) + "\n", encoding="utf-8")
        observed = C._core_sync_observation(self.root, stale, "rt-none", set())
        self.assertTrue(observed["sealed_evidence_class_declared"])
        self.assertFalse(observed["sealed_evidence_relocations_declared"])
        self.assertFalse(observed["proven"])
        self.assertEqual(set(observed["sealed_evidence_relocation_rows"]), set(C.SEALED_EVIDENCE_PATHS))
        current = C._core_sync_observation(self.root, self.core_file, "rt-none", set())
        self.assertTrue(current["sealed_evidence_relocations_declared"])
        self.assertEqual(tuple(C.SEALED_EVIDENCE_RELOCATIONS), C.SEALED_EVIDENCE_PATHS)

    def test_duplicate_noncanonical_aliases_reserve_one_canonical_target(self):
        duplicate_route, duplicate_file = self.route("audit")
        route_bytes = duplicate_file.read_bytes()
        duplicate_file.unlink()
        first = duplicate_file.with_name("duplicate-alias-a.json")
        second = duplicate_file.with_name("duplicate-alias-b.json")
        first.write_bytes(route_bytes)
        second.write_bytes(route_bytes)

        package = C.route_sweep_package(
            self.root,
            jobs=[self.jobs],
            preserve_routes=[self.residue_route["route_id"]],
        )
        aliases = [
            row for row in package["plan"]["actions"]
            if row.get("route_id") == duplicate_route["route_id"]
        ]
        self.assertEqual(
            [(Path(row["source_paths"][0]).name, row["action"]) for row in aliases],
            [
                ("duplicate-alias-a.json", "canonicalize"),
                ("duplicate-alias-b.json", "migrate-residue"),
            ],
        )
        targets = [
            target for row in aliases for target in row.get("target_paths", [])
        ]
        self.assertEqual(len(targets), len(set(targets)))

    def test_foreign_artifact_root_route_is_residue_not_canonicalized(self):
        foreign = dict(self.dead_route)
        foreign["artifact_root"] = str(Path(self._tmp.name) / "foreign" / ".agent_reports")
        foreign["route_hash"] = R.route_hash(foreign)
        foreign["route_id"] = "rt-" + foreign["route_hash"].split(":", 1)[-1][:16]
        alias = self.root / ".runtime" / "routes" / "foreign-root-alias.json"
        alias.write_text(json.dumps(foreign) + "\n", encoding="utf-8")

        package = C.route_sweep_package(
            self.root,
            jobs=[self.jobs],
            preserve_routes=[self.residue_route["route_id"]],
        )
        action = next(
            row for row in package["plan"]["actions"]
            if row.get("route_id") == foreign["route_id"]
        )

        self.assertEqual(action["action"], "migrate-residue")
        self.assertFalse(action["root_match"])
        self.assertEqual(action["route_artifact_root"], foreign["artifact_root"])
        self.assertIn(".runtime/routes/foreign-root-alias.json", package["plan"]["residue_paths"])

    def test_canonicalize_closes_after_move_and_recovers_existing_target(self):
        alias_route, canonical = self.route("audit")
        route_bytes = canonical.read_bytes()
        canonical.unlink()
        alias = canonical.with_name("open-alias-without-outcome.json")
        alias.write_bytes(route_bytes)
        package = C.route_sweep_package(
            self.root,
            jobs=[self.jobs],
            preserve_routes=[self.residue_route["route_id"]],
        )
        action = next(
            row for row in package["plan"]["actions"]
            if row.get("route_id") == alias_route["route_id"]
        )
        self.assertEqual(action["action"], "canonicalize")
        target = self.root / action["target_paths"][0]

        # Simulate a crash after the atomic route move and before close.
        os.replace(alias, target)
        result = C._apply_route_sweep_plan(self.root, package)

        self.assertEqual((result["status"], result["phase"]), ("applied", "complete"))
        self.assertTrue(target.is_file())
        self.assertTrue(target.with_name(target.stem + ".outcome.json").is_file())
        self.assertFalse(alias.exists())

    def test_prebackup_recovery_restores_partial_canonical_move(self):
        alias_route, canonical = self.route("audit")
        route_bytes = canonical.read_bytes()
        canonical.unlink()
        alias = canonical.with_name("partial-alias.json")
        alias.write_bytes(route_bytes)
        route_package, closeout_package = self.packages()
        action = next(
            row for row in route_package["plan"]["actions"]
            if row.get("route_id") == alias_route["route_id"]
        )
        self.assertEqual(action["action"], "canonicalize")
        target = self.root / action["target_paths"][0]
        os.replace(alias, target)

        journal_path = C._closeout_journal(
            self.root,
            "residue-" + closeout_package["plan_sha256"].split(":", 1)[-1],
        )
        P._write_atomic(journal_path, P._json_bytes({
            "schema_version": 1,
            "kind": "w7f-closeout-residue-journal",
            "plan_sha256": closeout_package["plan_sha256"],
            "route_sweep_plan_sha256": route_package["plan_sha256"],
            "phase": "prepared",
            "closeout_package": closeout_package,
            "route_sweep_package": route_package,
        }))

        result = C.recover_closeout_prebackup(
            self.root,
            journal_path=journal_path,
            reason="fixture-partial-move",
        )

        self.assertEqual((result["status"], result["phase"]),
                         ("aborted-prebackup", "aborted-prebackup"))
        self.assertTrue(alias.is_file())
        self.assertFalse(target.exists())
        again = C.recover_closeout_prebackup(
            self.root,
            journal_path=journal_path,
            reason="fixture-repeat",
        )
        self.assertEqual(again["status"], "already-aborted")

    def test_explicit_alive_route_cycle_is_a_typed_quiescence_hold(self):
        live_route, live_route_file = self.route("audit")
        begun = P.begin(
            self.root,
            route_file=live_route_file,
            capability="autopilot-code",
            intensity="direct",
        )
        preserve = [self.residue_route["route_id"], live_route["route_id"]]

        def liveness(route_id, _attempts):
            state = "alive" if route_id == live_route["route_id"] else "dead"
            return {"state": state, "attempts": []}

        with mock.patch.object(C, "_route_liveness", side_effect=liveness):
            package = C.closeout_residue_package(
                self.root,
                jobs=[self.jobs],
                residue_route=self.residue_route_file,
                backup_root=self.backup,
                core_file=self.core_file,
                preserve_routes=preserve,
                self_cycles=[],
                sync_route_id=self.sync_route_id,
            )
        quiesce = package["plan"]["quiesce"]
        self.assertEqual(
            quiesce["preserved_live_cycles"],
            [{"cycle_id": begun["cycle_id"], "route_id": live_route["route_id"]}],
        )
        self.assertEqual(quiesce["external_open_cycles"], [])
        self.assertTrue(quiesce["proven"])

    def test_false_approval_refuses_both_apply_surfaces(self):
        route_package, closeout_package = self.packages()
        approval = self.approval(route_package, closeout_package, False)
        with self.assertRaises(C.CutoverError) as caught:
            C.apply_route_sweep(
                self.root,
                package=route_package,
                approval_path=approval,
                closeout_digest=closeout_package["plan_sha256"],
            )
        self.assertEqual(caught.exception.code, "approval-not-authorized")
        with self.assertRaises(C.CutoverError) as caught:
            C.apply_closeout_residue(
                self.root,
                closeout_package=closeout_package,
                route_package=route_package,
                approval_path=approval,
                backup_root=self.backup,
            )
        self.assertEqual(caught.exception.code, "approval-not-authorized")
        self.assertTrue((self.root / "plan.md").is_file())

    def test_apply_refuses_source_digest_drift_before_route_or_backup_mutation(self):
        route_package, closeout_package = self.packages()
        approval = self.approval(route_package, closeout_package, True)
        self.w("plan.md", "changed after approval\n")
        with self.assertRaises(C.CutoverError) as caught:
            C.apply_closeout_residue(
                self.root,
                closeout_package=closeout_package,
                route_package=route_package,
                approval_path=approval,
                backup_root=self.backup,
            )
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("approval-stale", "plan.md"))
        self.assertFalse(self.dead_route_file.with_name(self.dead_route_file.stem + ".outcome.json").exists())
        self.assertFalse(self.backup.exists())
        self.assertTrue((self.root / "plan.md").is_file())

    def test_apply_backs_up_seals_then_retires_and_is_idempotent(self):
        route_package, closeout_package = self.packages()
        approval = self.approval(route_package, closeout_package, True)
        result = C.apply_closeout_residue(
            self.root,
            closeout_package=closeout_package,
            route_package=route_package,
            approval_path=approval,
            backup_root=self.backup,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["phase"], "complete")
        self.assertTrue(Path(result["backup"]["archive"]).is_file())
        self.assertTrue((Path(result["destination_cycle"]["cycle_dir"]) / "manifest.json").is_file())
        self.assertFalse((self.root / "plan.md").exists())
        self.assertFalse((self.root / "notes").exists())
        self.assertFalse((self.root / ".runtime/routes/legacy-alias.json").exists())
        self.assertTrue((self.root / ".pipeline-lock").is_file())
        for path in C.SEALED_EVIDENCE_PATHS:
            self.assertTrue((self.root / path).exists())
        dead_outcome = self.dead_route_file.with_name(self.dead_route_file.stem + ".outcome.json")
        self.assertTrue(dead_outcome.is_file())
        again = C.apply_closeout_residue(
            self.root,
            closeout_package=closeout_package,
            route_package=route_package,
            approval_path=approval,
            backup_root=self.backup,
        )
        self.assertEqual(again["status"], "already-applied")

    def test_standalone_route_sweep_then_closeout_uses_same_approved_packages(self):
        route_package, closeout_package = self.packages()
        approval = self.approval(route_package, closeout_package, True)
        sweep = C.apply_route_sweep(
            self.root,
            package=route_package,
            approval_path=approval,
            closeout_digest=closeout_package["plan_sha256"],
        )
        self.assertEqual(sweep["status"], "applied")
        current_route, current_closeout = self.packages()
        self.assertNotEqual(current_route["plan_sha256"], route_package["plan_sha256"])
        self.assertNotEqual(current_closeout["plan_sha256"], closeout_package["plan_sha256"])
        result = C.apply_closeout_residue(
            self.root,
            closeout_package=current_closeout,
            route_package=current_route,
            approval_path=approval,
            backup_root=self.backup,
        )
        self.assertEqual((result["status"], result["phase"]), ("applied", "complete"))
        self.assertFalse((self.root / "plan.md").exists())

    def test_approved_sweep_does_not_mask_new_residue_before_closeout(self):
        route_package, closeout_package = self.packages()
        approval = self.approval(route_package, closeout_package, True)
        C.apply_route_sweep(
            self.root,
            package=route_package,
            approval_path=approval,
            closeout_digest=closeout_package["plan_sha256"],
        )
        self.w("new-after-approval.md", "new residue\n")
        current_route, current_closeout = self.packages()
        with self.assertRaises(C.CutoverError) as caught:
            C.apply_closeout_residue(
                self.root,
                closeout_package=current_closeout,
                route_package=current_route,
                approval_path=approval,
                backup_root=self.backup,
            )
        self.assertEqual(caught.exception.code, "approval-stale")
        self.assertTrue((self.root / "new-after-approval.md").is_file())
        self.assertFalse(self.backup.exists())

    def test_apply_rolls_forward_from_approved_journal_after_plan_drift(self):
        route_package, closeout_package = self.packages()
        approval = self.approval(route_package, closeout_package, True)
        with self.assertRaises(C.CutoverError) as caught:
            C.apply_closeout_residue(
                self.root,
                closeout_package=closeout_package,
                route_package=route_package,
                approval_path=approval,
                backup_root=self.backup,
                crash_after_phase="destination-started",
            )
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("crash-fixture", "destination-started"))

        current_route, current_closeout = self.packages()
        self.assertNotEqual(current_route["plan_sha256"], route_package["plan_sha256"])
        self.assertNotEqual(current_closeout["plan_sha256"], closeout_package["plan_sha256"])
        self.assertFalse(current_closeout["plan"]["apply_ready"])
        result = C.apply_closeout_residue(
            self.root,
            closeout_package=current_closeout,
            route_package=current_route,
            approval_path=approval,
            backup_root=self.backup,
        )
        self.assertEqual((result["status"], result["phase"]), ("applied", "complete"))
        self.assertFalse((self.root / "plan.md").exists())
        self.assertTrue(Path(result["destination_cycle"]["cycle_dir"]).joinpath("manifest.json").is_file())


class RouteLivenessOwnerAdvanceTest(unittest.TestCase):
    """The verified successor of an open owner counts as live, and a
    conflicting/tampered advance record degrades the anchor's own liveness
    call to `unverifiable` rather than a false `dead`."""

    def _row(self, jobs, **meta):
        return {"status": "open", "jobs": jobs,
                "metadata": {"attempt_id": "att-owner", **meta}}

    def test_verified_successor_is_alive(self):
        attempts = {"att-owner": self._row(
            "/jobs.log", owner_route_file="/r0.json",
            owner_route_id="rt-r0", owner_route_hash="sha256:r0",
        )}
        current = C.OwnerRouteBinding("/r1.json", "rt-r1", "sha256:r1")
        with mock.patch.object(C, "resolve_owner_route_lifecycle",
                               return_value=(current, "owner-route-advance-current")), \
             mock.patch.object(C.DISPATCH, "observed_attempt_liveness") as observed:
            observed.return_value = mock.Mock(state="alive", reason="ok")
            result = C._route_liveness("rt-r1", attempts)
        self.assertEqual(result["state"], "alive")

    def test_post_launch_attachment_is_alive_without_launch_tuple(self):
        attempts = {"att-owner": self._row("/jobs.log")}
        current = C.OwnerRouteBinding("/r0.json", "rt-r0", "sha256:r0")
        with mock.patch.object(
            C, "resolve_owner_route_lifecycle",
            return_value=(current, "owner-route-post-launch-attachment"),
        ), mock.patch.object(C.DISPATCH, "observed_attempt_liveness") as observed:
            observed.return_value = mock.Mock(state="alive", reason="ok")
            result = C._route_liveness("rt-r0", attempts)
        self.assertEqual(result["state"], "alive")

    def test_conflicting_advance_marks_anchor_unverifiable_not_dead(self):
        attempts = {"att-owner": self._row(
            "/jobs.log", owner_route_file="/r0.json",
            owner_route_id="rt-r0", owner_route_hash="sha256:r0",
        )}
        with mock.patch.object(
            C, "resolve_owner_route_lifecycle",
            side_effect=C.OwnerRouteBindingError("owner-route-advance-target-invalid"),
        ):
            result = C._route_liveness("rt-r0", attempts)
        self.assertEqual(result["state"], "unverifiable")

    def test_absent_advance_record_is_unaffected(self):
        attempts = {"att-owner": self._row(
            "/jobs.log", owner_route_file="/r0.json",
            owner_route_id="rt-r0", owner_route_hash="sha256:r0",
        )}
        anchor = C.OwnerRouteBinding("/r0.json", "rt-r0", "sha256:r0")
        with mock.patch.object(C, "resolve_owner_route_lifecycle",
                               return_value=(anchor, "owner-route-launch-binding")), \
             mock.patch.object(C.DISPATCH, "observed_attempt_liveness") as observed:
            observed.return_value = mock.Mock(state="alive", reason="ok")
            result = C._route_liveness("rt-r0", attempts)
        self.assertEqual(result["state"], "alive")


if __name__ == "__main__":
    unittest.main()
