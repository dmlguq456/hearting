#!/usr/bin/env python3
"""G2/G3/G4 executor tests for `artifact_cutover.py` on a synthetic legacy root."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
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

    def migrate(self):
        route, route_file = self.route()
        report = C.migrate_delta(self.root, census_rows=self.rows, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", excludes=["plans/keep"], approval_receipt_sha256="x" * 64, campaign_id=None)
        self.close(route, route_file)
        sealed = C.migrate_seal(self.root, run_dir=Path(report["run_dir"]), spec_reference=W7_REF)
        return report, sealed

    def test_migrate_delta_copies_candidates_and_snapshots_shared(self):
        report, sealed = self.migrate()
        self.assertEqual(report["copied_by_bucket"], {"plans": 2, "research": 1})  # w7-source-preserved rows are not re-copied
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

    def test_prd_candidates_fall_back_to_latest_shared_revision(self):
        self.assertEqual(C.prd_candidates(self.root), [str(self.root / "spec/prd.md"), str(self.root / "spec/comp/prd.md")])
        report, sealed = self.migrate()
        import shutil
        shutil.rmtree(self.root / "spec")
        cands = C.prd_candidates(self.root)
        rev = Path(sealed["shared_admissions"]["spec"]["revision_dir"])
        self.assertEqual(cands, [str(rev / "prd.md"), str(rev / "comp/prd.md")])

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
        out = C.retire(self.root, maps=[self.w7_map, w7c_map], backup_root=backup, excludes=["plans/keep"],
                       approval_receipt_sha256="y" * 64)
        self.assertEqual(out["kept_files"], 1)
        self.assertEqual(out["kept"][0]["source"], "plans/2026-01-02_b/final_report.md")
        self.assertFalse((self.root / "spec/prd.md").exists())
        self.assertFalse((self.root / "plans/2026-01-01_a").exists())
        self.assertFalse((self.root / "research").exists())
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

    def test_retire_refuses_backup_inside_root(self):
        report, sealed = self.migrate()
        with self.assertRaises(C.CutoverError) as ctx:
            C.retire(self.root, maps=[self.w7_map], backup_root=self.root / "_scratch", excludes=[], approval_receipt_sha256=None)
        self.assertEqual(ctx.exception.code, "backup-root-inside-artifact-root")


if __name__ == "__main__":
    unittest.main()
