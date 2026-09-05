#!/usr/bin/env python3
"""W7H residue disposal tests: classification, journaled apply, rollback/forward, trash gate, status."""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_cutover as C  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_reader as RD  # noqa: E402
import artifact_relayout as RL  # noqa: E402
import artifact_residue as RES  # noqa: E402
import fleet_cutover_gate as G  # noqa: E402

_RT_SPEC = importlib.util.spec_from_file_location(
    "relayout_test_for_residue", Path(__file__).with_name("artifact_relayout.test.py"))
RT = importlib.util.module_from_spec(_RT_SPEC)
_RT_SPEC.loader.exec_module(RT)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path, skip=(".runtime/artifact-producer/v1/journal", ".runtime/artifact-producer/v1/migrations",
                              ".runtime/artifact-admission", "campaigns/INDEX", ".runtime/routes")):
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(s) for s in skip):
            continue
        out[rel] = _sha(path)
    return out


class ResidueFixture(RT.RelayoutFixture):
    """A readable-layout root (post-W7I) with a legacy top level to dispose of."""

    def setUp(self):
        super().setUp()
        if not C.compat_path(self.root).is_file():
            C.compat_close(self.root, maps=[], approval_receipt_sha256=None)
        self.caller_route, self.route_file = self.route(slug="w7h-fixture")

    def seed(self, rel, data=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((data if data is not None else f"legacy {rel}\n").encode() if isinstance(data, str) or data is None else data)
        return path

    def migrated_plan_cycle(self, d1="2026-06-09_ami-bandlimit"):
        """A plan cycle W7C migrated: a sealed cycle plus a compat map row from `plans/<d1>/final.md`."""
        _route, sealed = self.cycle(slug=d1, title=d1, files=(f"plans/{d1}/final.md",))
        cycle_dir = P.cycle_dir(self.root, sealed["campaign_id"], sealed["cycle_id"])
        target = f"{cycle_dir.relative_to(self.root).as_posix()}/artifacts/plans/{d1}/final.md"
        older = Path(self._tmp.name) / "w7c-map.jsonl"
        C._write_jsonl(older, [{
            "schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": f"plans/{d1}/final.md",
            "target_locator": target, "sha256": "sha256:" + _sha(self.root / target), "identity_refs": [],
        }, {
            "schema_version": C.MAP_SCHEMA, "kind": "directory", "source_locator": f"plans/{d1}",
            "target_locator": f"{cycle_dir.relative_to(self.root).as_posix()}/artifacts/plans/{d1}",
            "sha256": "sha256:" + "0" * 64, "identity_refs": [],
        }])
        C.compat_close(self.root, maps=[older], approval_receipt_sha256=None)
        return sealed, d1

    def apply(self, **kw):
        kw.setdefault("route_file", self.route_file)
        return RES.apply(self.root, **kw)


class ClassificationTests(ResidueFixture):
    def test_every_shape_gets_its_disposition(self):
        sealed, d1 = self.migrated_plan_cycle()
        self.seed(f"plans/{d1}/_internal/plan_reviews/round_1.md")
        self.seed(f"plans/{d1}/RELOCATED-20260901.json", "{}")
        self.seed("plans/2026-07-01_never-migrated/plan.md")
        self.seed("_internal/dev_reviews/phase.md")
        self.seed("shards/frame/direction-brief.md")
        self.seed("papers/2026-03-30_rebuttal/draft.md")
        self.seed("refs/paper.pdf", b"%PDF")
        self.seed("routes/2026-08-05_x.outcome.json", "{}")
        self.seed("hook-sweep-route.json", "{}")
        self.seed("NOTES.md")
        self.seed("spec/prd.md")
        self.seed("research/x/.agent_reports/.runtime/model-worker-governor/lock", "")
        self.seed("plans/.gitkeep", "")
        self.seed("notes/한글 이름.md")
        (self.root / "empty-shape").mkdir()
        plan = RES.build_plan(self.root)
        by_path = {}
        for c in plan["cycles"]:
            for f in c["files"]:
                by_path[f["path"]] = (c["group"], f["target"])
        self.assertEqual(by_path[f"plans/{d1}/_internal/plan_reviews/round_1.md"][0], f"origin:{sealed['cycle_id']}")
        self.assertEqual(by_path[f"plans/{d1}/_internal/plan_reviews/round_1.md"][1],
                         f"artifacts/_internal/plans/{d1}/_internal/plan_reviews/round_1.md")
        self.assertEqual(by_path[f"plans/{d1}/RELOCATED-20260901.json"][0], f"origin:{sealed['cycle_id']}")
        self.assertEqual(by_path["plans/2026-07-01_never-migrated/plan.md"],
                         ("bucket:plans/2026-07-01_never-migrated", "artifacts/plans/2026-07-01_never-migrated/plan.md"))
        self.assertEqual(by_path["_internal/dev_reviews/phase.md"], ("support:root", "artifacts/_internal/_internal/dev_reviews/phase.md"))
        self.assertEqual(by_path["shards/frame/direction-brief.md"][0], "support:root")
        self.assertEqual(by_path["papers/2026-03-30_rebuttal/draft.md"],
                         ("material:papers/2026-03-30_rebuttal", "artifacts/documents/papers/2026-03-30_rebuttal/draft.md"))
        self.assertEqual(by_path["refs/paper.pdf"], ("material:refs", "artifacts/documents/refs/paper.pdf"))
        self.assertEqual(by_path["NOTES.md"], ("material:_root", "artifacts/documents/_root/NOTES.md"))
        routes = {r["path"]: r["target"] for r in plan["routes"]}
        self.assertEqual(routes["routes/2026-08-05_x.outcome.json"], ".runtime/routes/legacy/routes/2026-08-05_x.outcome.json")
        self.assertEqual(routes["hook-sweep-route.json"], ".runtime/routes/legacy/hook-sweep-route.json")
        self.assertEqual({t["path"] for t in plan["trash"]},
                         {"plans/.gitkeep", "research/x/.agent_reports/.runtime/model-worker-governor/lock"})
        deferred = {d["path"]: d["reason"] for d in plan["deferred"]}
        self.assertEqual(deferred, {"spec/prd.md": "spec-consumer-pinned"})
        korean = by_path["notes/한글 이름.md"]
        self.assertEqual(korean[0], "material:notes")
        self.assertTrue(korean[1].startswith("artifacts/documents/notes/") and korean[1].endswith(".md"), korean)
        self.assertIsNone(P._unmanifestable_reason(korean[1]))
        self.assertEqual(plan["totals"]["renamed_locators"], 1)
        self.assertEqual(plan["empty_dirs"], ["empty-shape"])
        specs = {c["group"]: c for c in plan["cycles"]}
        origin_cycle = specs[f"origin:{sealed['cycle_id']}"]
        self.assertEqual(origin_cycle["campaign_id"], sealed["campaign_id"])
        self.assertTrue(origin_cycle["support_all"])
        self.assertEqual(specs["bucket:plans/2026-07-01_never-migrated"]["date"], "2026-07-01")
        self.assertEqual(specs["bucket:plans/2026-07-01_never-migrated"]["date_source"], "directory-date-prefix")
        self.assertEqual(specs["material:papers/2026-03-30_rebuttal"]["date"], "2026-03-30")
        self.assertEqual({s["id"] for s in plan["spec_impact"]},
                         {"D-23-material-shapes", "D-79-residue-cycle", "D-6-sanitized-locator"})
        self.assertEqual(plan["totals"]["files"], 14)
        self.assertEqual(plan["totals"]["movable"], 11)

    def test_sealed_evidence_moves_and_map_is_repointed(self):
        evidence = C.SEALED_EVIDENCE_PATHS[2]  # plans/2026-08-25_artifact-knowledge-index-w7-e2-e3
        self.seed(f"{evidence}/evidence/note.md")
        map_path = self.seed(f"{evidence}/evidence/compatibility-map.jsonl", "")
        C._write_jsonl(map_path, [{"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "plans/gone.md",
                                   "target_locator": "shared/x", "sha256": "sha256:" + "1" * 64, "identity_refs": []}])
        C.compat_close(self.root, maps=[map_path], approval_receipt_sha256=None)
        plan = RES.build_plan(self.root)
        self.assertEqual(len(plan["moved_compat_maps"]), 1)
        self.assertIn("D-82-map-path-repoint", {s["id"] for s in plan["spec_impact"]})
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        state = C.load_map_state(self.root)
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["drifted"], [])
        self.assertTrue(state["maps"][0]["path"].endswith("artifacts/plans/2026-08-25_artifact-knowledge-index-w7-e2-e3/evidence/compatibility-map.jsonl"))
        compat = json.loads(C.compat_path(self.root).read_text())
        self.assertEqual(compat["maps"][0]["relocated_from"], str(map_path))
        self.assertEqual(result["report"]["compat"]["repointed_maps"][0]["from"], str(map_path))
        self.assertFalse((self.root / "plans").exists())


class ApplyTests(ResidueFixture):
    def _populate(self):
        sealed, d1 = self.migrated_plan_cycle()
        self.seed(f"plans/{d1}/_internal/plan_reviews/round_1.md")
        self.seed(f"plans/{d1}/RELOCATED-20260901.json", "{}")
        self.seed("plans/2026-07-01_never-migrated/plan.md")
        self.seed("plans/2026-07-01_never-migrated/notes/n.md")
        self.seed("_internal/dev_reviews/phase.md")
        self.seed("shards/frame/direction-brief.md")
        self.seed("papers/2026-03-30_rebuttal/draft.md")
        self.seed("refs/paper.pdf", b"%PDF")
        self.seed("routes/2026-08-05_x.outcome.json", "{}")
        self.seed("NOTES.md")
        self.seed("spec/prd.md")
        self.seed("plans/.gitkeep", "")
        (self.root / "empty-shape").mkdir()
        return sealed, d1

    def test_apply_moves_everything_movable_and_keeps_bytes(self):
        sealed, d1 = self._populate()
        before = _files(self.root)
        movable = [p for p in before if not p.startswith("campaigns/") and not p.startswith(".runtime/")
                   and p not in {"spec/prd.md", "plans/.gitkeep"}]
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        report = result["report"]
        self.assertEqual(report["totals"]["movable"], len(movable))
        self.assertEqual(report["witness"]["files_checked"], len(movable))
        after = _files(self.root)
        for rel in movable:
            self.assertNotIn(rel, after, rel)
            resolved = C.resolve_legacy(self.root, rel)
            self.assertEqual(resolved["resolution"], "mapped", (rel, resolved))
            self.assertEqual(_sha(Path(resolved["absolute"])), before[rel], rel)
        self.assertIn("spec/prd.md", after)
        self.assertTrue((self.root / "plans" / ".gitkeep").is_file())
        self.assertFalse((self.root / "empty-shape").exists())
        self.assertFalse((self.root / "_internal").exists())
        self.assertFalse((self.root / "routes").exists())
        self.assertTrue((self.root / ".runtime" / "routes" / "legacy" / "routes" / "2026-08-05_x.outcome.json").is_file())
        # Every residue cycle is sealed in the readable layout and cites its origin.
        origin_group = f"origin:{sealed['cycle_id']}"
        cycles = {c["group"]: c for c in report["cycles"]}
        residue = P.read_cycle_record(self.root, cycles[origin_group]["cycle_id"])
        self.assertEqual(residue["state"], "sealed")
        self.assertEqual(residue["campaign_id"], sealed["campaign_id"])
        self.assertEqual(residue["residue_of"]["cycle_id"], sealed["cycle_id"])
        self.assertEqual(residue["started_on"][:10], P.read_cycle_record(self.root, sealed["cycle_id"])["started_on"][:10])
        manifest = json.loads((self.root / cycles[origin_group]["cycle_dir"] / "manifest.json").read_text())
        roles = {a["role"] for a in manifest["artifacts"]}
        self.assertEqual(roles, {"support", "primary"})
        self.assertTrue((self.root / cycles[origin_group]["cycle_dir"] / "artifacts" / "_internal" / "residue-inventory.json").is_file())
        self.assertNotIn("cycles", Path(cycles[origin_group]["cycle_dir"]).parts)
        bucket = P.read_cycle_record(self.root, cycles["bucket:plans/2026-07-01_never-migrated"]["cycle_id"])
        self.assertEqual(bucket["started_on"][:10], "2026-07-01")
        self.assertEqual(bucket["started_on_source"], "directory-date-prefix")
        self.assertEqual(bucket["locator"], "2026-07-01_2026-07-01-never-migrated")
        self.assertEqual(bucket["state"], "sealed")
        original = P.read_cycle_record(self.root, sealed["cycle_id"])
        self.assertEqual(original["state"], "sealed")
        self.assertEqual(before[f"campaigns/{Path(P.cycle_dir(self.root, sealed['campaign_id'], sealed['cycle_id'])).relative_to(self.root / 'campaigns').as_posix()}/manifest.json"],
                         after[f"campaigns/{Path(P.cycle_dir(self.root, sealed['campaign_id'], sealed['cycle_id'])).relative_to(self.root / 'campaigns').as_posix()}/manifest.json"])
        # Status and gate view.
        view = RES.status(self.root)
        self.assertEqual(view["legacy_top_level"], "residue")  # spec + .gitkeep remain
        self.assertEqual(view["legacy_top_level_files"], 2)
        self.assertEqual(view["trash_pending"], 1)
        self.assertEqual([d["path"] for d in view["deferred"]], ["spec/prd.md"])
        self.assertIsNone(RES.residue_hold(self.root))
        self.assertIsNone(RD.migration_hold(self.root))
        second = self.apply()
        self.assertEqual(second["status"], "no-op")

    def test_dry_run_leaves_no_trace(self):
        self._populate()
        before = _files(self.root, skip=(".runtime/artifact-producer/v1/journal",))
        result = self.apply(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(_files(self.root, skip=(".runtime/artifact-producer/v1/journal",)), before)
        self.assertEqual(RES.run_dirs(self.root), [])

    def test_crash_before_seal_rolls_back_completely(self):
        self._populate()
        before = _files(self.root)
        routes_before = sorted(p.name for p in (self.root / ".runtime" / "routes").glob("*.json"))
        for point in ({"crash_at": "begin:after-first-cycle"}, {"crash_at": "rename:after-first-file"},
                      {"crash_after_phase": "renamed"}, {"crash_after_phase": "witnessed"}):
            with self.subTest(point=point):
                with self.assertRaises(RES.ResidueError):
                    self.apply(**point)
                hold = RES.residue_hold(self.root)
                self.assertIsNotNone(hold)
                self.assertEqual(RD.migration_hold(self.root), hold)
                resumed = self.apply()
                self.assertEqual(resumed["status"], "rolled-back", resumed)
                self.assertEqual(_files(self.root), before)
                self.assertEqual(sorted(p.name for p in (self.root / ".runtime" / "routes").glob("*.json")), routes_before)
                self.assertIsNone(RES.residue_hold(self.root))
        done = self.apply()
        self.assertEqual(done["status"], "complete")

    def test_crash_after_seal_rolls_forward(self):
        self._populate()
        for phase in ("sealed", "compat-reissued", "indexed"):
            with self.subTest(phase=phase):
                self.setUp()
                self._populate()
                with self.assertRaises(RES.ResidueError):
                    self.apply(crash_after_phase=phase)
                with self.assertRaises(RES.ResidueError) as ctx:
                    RES.rollback(self.root)
                self.assertEqual(ctx.exception.code, "residue-past-commit-point")
                resumed = self.apply()
                self.assertEqual(resumed["status"], "complete", resumed)
                self.assertEqual(resumed["resumed_from"], phase)
                self.assertEqual(len(C.load_map_state(self.root)["maps"]), 2)
                self.assertEqual(RES.status(self.root)["legacy_top_level_files"], 2)

    def test_relayout_hold_blocks_residue_apply(self):
        self._populate()
        self.cycle(slug="old-layout", title="Old layout")
        self.legacyize(keep_titles=True)
        with self.assertRaises(RL.RelayoutError):
            RL.apply(self.root, jobs_path=self.jobs, crash_after_phase="renamed")
        with self.assertRaises(RES.ResidueError) as ctx:
            self.apply()
        self.assertEqual(ctx.exception.code, "relayout-in-progress")


class SymlinkAndSanitizeTests(ResidueFixture):
    def test_symlink_is_deferred_and_retirable_with_flag(self):
        self.seed("experiments/2026-07-27_x/report.md")
        (self.root / "experiments" / "2026-07-27_x" / "link").symlink_to("/nonexistent/target.wav")
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["symlinks"], 1)
        self.assertEqual([d["reason"] for d in plan["deferred"]], ["symlink"])
        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertTrue((self.root / "experiments" / "2026-07-27_x" / "link").is_symlink())
        backup = Path(self._tmp.name) / "backup"
        package = RES.trash_approval_package(self.root, backup_root=backup, include_symlinks=True)
        self.assertEqual(package["body"]["entry_count"], 1)
        package["authorized"] = True
        approval = Path(self._tmp.name) / "approval.json"
        approval.write_text(json.dumps(package))
        done = RES.retire_trash(self.root, approval_path=approval, backup_root=backup, include_symlinks=True)
        self.assertEqual(done["report"]["retired_files"], 1)
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "empty")

    def test_sanitized_locator_round_trips_through_inventory_and_compat(self):
        original = "notes/한글 이름 (v2).md"
        self.seed(original, "body\n")
        self.seed("_internal/.hidden-note.md", "hidden\n")
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        resolved = C.resolve_legacy(self.root, original)
        self.assertEqual(resolved["resolution"], "mapped")
        self.assertIsNone(P._unmanifestable_reason(resolved["target"].split("/artifacts/", 1)[1]))
        self.assertEqual(Path(resolved["absolute"]).read_text(), "body\n")
        inventory = json.loads((Path(resolved["absolute"]).parents[2] / "_internal" / "residue-inventory.json").read_text()) \
            if (Path(resolved["absolute"]).parents[2] / "_internal" / "residue-inventory.json").is_file() else None
        cycle_dir = Path(resolved["absolute"]).as_posix().split("/artifacts/")[0]
        inventory = json.loads((Path(cycle_dir) / "artifacts" / "_internal" / "residue-inventory.json").read_text())
        renamed = [f for f in inventory["files"] if f.get("locator_renamed_from") == original]
        self.assertEqual(len(renamed), 1)
        hidden = C.resolve_legacy(self.root, "_internal/.hidden-note.md")
        self.assertEqual(hidden["resolution"], "mapped")
        self.assertIsNone(P._unmanifestable_reason(hidden["target"].split("/artifacts/", 1)[1]))
        self.assertRegex(hidden["target"], r"/hidden-note-[0-9a-f]{8}\.md$")
        self.assertRegex(resolved["target"], r"/notes/_-[0-9a-f]{8}\.md$|/notes/[A-Za-z0-9_-]+-[0-9a-f]{8}\.md$")


class TrashTests(ResidueFixture):
    def test_trash_needs_authorized_approval_and_is_backed_up(self):
        self.seed("plans/.gitkeep", "")
        nested = self.seed("research/x/.agent_reports/.runtime/model-worker-governor/state.json", "{}")
        backup = Path(self._tmp.name) / "backup"
        dry = RES.retire_trash(self.root, approval_path=None, backup_root=backup, dry_run=True)
        self.assertEqual(dry["status"], "dry-run")
        self.assertEqual(dry["inventory"]["entry_count"], 2)
        with self.assertRaises(RES.ResidueError) as ctx:
            RES.retire_trash(self.root, approval_path=None, backup_root=backup)
        self.assertEqual(ctx.exception.code, "trash-approval-required")
        package = RES.trash_approval_package(self.root, backup_root=backup)
        approval = Path(self._tmp.name) / "approval.json"
        approval.write_text(json.dumps(package))
        with self.assertRaises(RES.ResidueError) as ctx:
            RES.retire_trash(self.root, approval_path=approval, backup_root=backup)
        self.assertEqual(ctx.exception.code, "approval-not-authorized")
        package["authorized"] = True
        approval.write_text(json.dumps(package))
        nested.write_text("changed")
        with self.assertRaises(RES.ResidueError) as ctx:
            RES.retire_trash(self.root, approval_path=approval, backup_root=backup)
        self.assertEqual(ctx.exception.code, "approval-stale")
        nested.write_text("{}")
        result = RES.retire_trash(self.root, approval_path=approval, backup_root=backup)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["report"]["retired_files"], 2)
        self.assertFalse((self.root / "plans").exists())
        self.assertFalse((self.root / "research").exists())
        seal = Path(result["report"]["backup_seal"]["archive"])
        self.assertTrue(seal.is_file())
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "empty")


class GateTests(ResidueFixture):
    def test_gate_reports_residue_and_requires_empty_when_asked(self):
        self.seed("_internal/dev_reviews/phase.md")
        self.seed("spec/prd.md")
        row = {"repo_path": "/x", "state": "active", "probe": {"passed": True}, "lumped_cycles_remaining": 0,
               "legacy_top_level_retired": True, "readable_layout": "readable", "relayout_hold": None,
               "transition_window": "closed", **G._residue_fields(self.root)}
        self.assertEqual(row["legacy_top_level"], "residue")
        verdict, blocking = G.evaluate([row], waived=False, require_resplit=True, require_relayout=True, require_residue=True)
        self.assertEqual(verdict, "incomplete")
        self.assertEqual(blocking[0]["reason"], "residue-remaining")
        verdict, _ = G.evaluate([row], waived=False, require_resplit=True, require_relayout=True)
        self.assertEqual(verdict, "complete")
        self.apply()
        row.update(G._residue_fields(self.root))
        self.assertEqual(row["legacy_top_level"], "deferred-only")
        verdict, blocking = G.evaluate([row], waived=False, require_residue=True)
        self.assertEqual(verdict, "complete", blocking)

    def test_cli_plan_status_hold(self):
        import io
        from contextlib import redirect_stdout
        self.seed("_internal/dev_reviews/phase.md")
        out = io.StringIO()
        with redirect_stdout(out):
            code = RES.main(["--artifact-root", str(self.root), "plan"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["totals"]["movable"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            code = RES.main(["--artifact-root", str(self.root), "apply", "--route-file", str(self.route_file)])
        self.assertEqual(code, 0, out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            code = RES.main(["--artifact-root", str(self.root), "status"])
        self.assertEqual(json.loads(out.getvalue())["legacy_top_level"], "empty")
        out = io.StringIO()
        with redirect_stdout(out):
            code = RES.main(["--artifact-root", str(self.root), "hold"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
