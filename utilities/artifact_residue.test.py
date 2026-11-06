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
        # Disposable lookup projections are outside the rollback's durable state.
        if rel in {".runtime/routes/.route-children-index.json",
                   ".runtime/artifact-producer/v1/cycles/.cycle-routes-index.json"}:
            continue
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

    def migrated_plan_cycle(self, d1="2026-06-09_ami-bandlimit", *, top="plans", files=("final.md",),
                            directory_row=True, map_name="w7c-map.jsonl"):
        """A cycle W7C migrated: a sealed cycle plus one compat map row per `<top>/<d1>/<file>`
        (and, like the fixture always had, a `kind: directory` row for `<top>/<d1>` unless told not to --
        the real roots only carry file rows)."""
        _route, sealed = self.cycle(slug=d1, title=d1, files=tuple(f"{top}/{d1}/{f}" for f in files))
        cycle_dir = P.cycle_dir(self.root, sealed["campaign_id"], sealed["cycle_id"])
        cycle_rel = cycle_dir.relative_to(self.root).as_posix()
        rows = []
        for f in files:
            target = f"{cycle_rel}/artifacts/{top}/{d1}/{f}"
            rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": f"{top}/{d1}/{f}",
                         "target_locator": target, "sha256": "sha256:" + _sha(self.root / target), "identity_refs": []})
        if directory_row:
            rows.append({"schema_version": C.MAP_SCHEMA, "kind": "directory", "source_locator": f"{top}/{d1}",
                         "target_locator": f"{cycle_rel}/artifacts/{top}/{d1}",
                         "sha256": "sha256:" + "0" * 64, "identity_refs": []})
        older = Path(self._tmp.name) / map_name
        C._write_jsonl(older, rows)
        if C.compat_path(self.root).is_file() and json.loads(C.compat_path(self.root).read_text()).get("maps"):
            C.compat_append(self.root, maps=[older], supersedes=[])
        else:
            C.compat_close(self.root, maps=[older], approval_receipt_sha256=None)
        return sealed, d1

    def sealed_cycle_rel(self, sealed):
        return P.cycle_dir(self.root, sealed["campaign_id"], sealed["cycle_id"]).relative_to(self.root).as_posix()

    def last_map_rows(self):
        return C._read_jsonl(Path(C.load_map_state(self.root)["maps"][-1]["path"]))

    def apply(self, **kw):
        kw.setdefault("route_file", self.route_file)
        return RES.apply(self.root, **kw)


class ClassificationTests(ResidueFixture):
    def test_payload_manifest_does_not_override_exact_cycle_ancestor(self):
        campaign = self.root / "campaigns" / "camp_test"
        cycle = campaign / "cycle_test"
        nested = cycle / "artifacts" / "_internal" / "candidate"
        nested.mkdir(parents=True)
        cycle_id = "cyc_" + "1" * 32
        (cycle / "manifest.json").write_text(json.dumps({"cycle": {"cycle_id": cycle_id}}), encoding="utf-8")
        (nested / "manifest.json").write_text(json.dumps({"cycle": {"cycle_id": "cyc_" + "2" * 32}}), encoding="utf-8")
        (nested / "result.md").write_text("payload", encoding="utf-8")
        rel = (nested / "result.md").relative_to(self.root).as_posix()
        self.assertEqual(RES._cycle_id_above(self.root, rel), cycle_id)

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

    def test_entrance_names_are_never_residue(self):
        for name in P.ENTRANCE_NAMES:
            (self.root / name).write_text("x\n", encoding="utf-8")
        for name in P.ENTRANCE_NAMES:
            self.assertTrue(RES._is_runtime_owned(name), name)
        self.assertEqual(list(RES.iter_residue_files(self.root)), [])
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["files"], 0)
        self.assertEqual(plan["empty_dirs"], [])
        self.assertEqual(RES._prune_empty(self.root), [])

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
                   and p not in {"spec/prd.md", "plans/.gitkeep"} and p not in P.ENTRANCE_NAMES]
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
                begun = json.loads(Path(hold["journal"]).read_text())["begun"].values()
                route_ids = {row["route_id"] for row in begun}
                removed_ids = {row["cycle_id"] for row in begun}
                self.assertTrue(removed_ids)
                indexed = P.list_cycle_records(self.root, route_ids=route_ids)
                self.assertTrue(removed_ids <= {row["cycle_id"] for row in indexed})
                resumed = self.apply()
                self.assertEqual(resumed["status"], "rolled-back", resumed)
                self.assertEqual(_files(self.root), before)
                self.assertEqual(sorted(p.name for p in (self.root / ".runtime" / "routes").glob("*.json")), routes_before)
                # A rolled-back cycle must disappear from indexed lookup too.
                expected = [row for row in P.list_cycle_records(self.root) if row.get("route_id") in route_ids]
                self.assertFalse(removed_ids & {row["cycle_id"] for row in expected})
                self.assertEqual(P.list_cycle_records(self.root, route_ids=route_ids), expected)
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

    def test_only_dangling_symlinks_are_retired_with_the_dangling_flag(self):
        self.seed("experiments/2026-07-27_x/report.md")
        (self.root / "experiments" / "2026-07-27_x" / "dead").symlink_to("/nonexistent/target.wav")
        # A live link points outside the residue (a corpus file); a link whose
        # target moves into a cycle becomes dangling by the move itself.
        live_target = Path(self._tmp.name) / "corpus" / "real.wav"
        live_target.parent.mkdir(); live_target.write_bytes(b"RIFF")
        (self.root / "experiments" / "2026-07-27_x" / "alive").symlink_to(live_target)
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["symlinks"], 2)
        self.assertEqual(plan["totals"]["symlinks_dangling"], 1)
        self.apply()
        backup = Path(self._tmp.name) / "backup"
        package = RES.trash_approval_package(self.root, backup_root=backup, include_dangling_symlinks=True)
        self.assertEqual([e["path"] for e in package["body"]["entries"]], ["experiments/2026-07-27_x/dead"])
        self.assertEqual(package["body"]["entries"][0]["reason"], "symlink-dangling")
        package["authorized"] = True
        approval = Path(self._tmp.name) / "approval.json"
        approval.write_text(json.dumps(package))
        done = RES.retire_trash(self.root, approval_path=approval, backup_root=backup, include_dangling_symlinks=True)
        self.assertEqual(done["report"]["retired_files"], 1)
        self.assertTrue((self.root / "experiments" / "2026-07-27_x" / "alive").is_symlink())
        # `report.md` moved into a bucket cycle in the first apply, so the live
        # link now has a migrated sibling: it is residue that rejoins on the
        # next apply (never in the run that seals its destination -- a symlink
        # inside a cycle being sealed is `symlink-forbidden` at finalize).
        view = RES.status(self.root)
        self.assertEqual(view["legacy_top_level"], "residue")
        self.assertEqual(view["rejoin_pending"], 1)
        self.assertEqual(view["symlinks"], 0)
        self.assertEqual(view["deferred"], [])
        second = self.apply()
        self.assertEqual(second["status"], "complete", second)
        self.assertEqual(second["report"]["totals"]["rejoined"], 1)
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "empty")
        moved = self.root / second["report"]["rejoins"][0]["target"]
        self.assertTrue(moved.is_symlink())
        self.assertEqual(os.readlink(moved), str(live_target))

    def test_dangling_symlink_beside_migrated_siblings_is_not_rejoined(self):
        _sealed, d1 = self.migrated_plan_cycle()
        (self.root / "plans" / d1).mkdir(parents=True)
        (self.root / "plans" / d1 / "dead.wav").symlink_to("/nonexistent/corpus/dead.wav")
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 0)
        self.assertEqual(plan["deferred"][0]["reason"], "symlink")
        self.assertTrue(plan["deferred"][0]["dangling"])
        self.assertEqual(self.apply()["status"], "no-op")
        inventory = RES.trash_inventory(self.root, include_dangling_symlinks=True)
        self.assertEqual([e["path"] for e in inventory["entries"]], [f"plans/{d1}/dead.wav"])
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "deferred-only")

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


class ReviewRegressionTests(ResidueFixture):
    """Independent review (2026-09-05): per-cycle commit point, reserved names, clock."""

    def test_seal_failure_midway_rolls_forward_never_back(self):
        self.seed("_internal/dev_reviews/phase.md")
        self.seed("papers/2026-03-30_rebuttal/draft.md")
        with self.assertRaises(RES.ResidueError):
            self.apply(crash_at="seal:after-first-cycle")
        hold = RES.residue_hold(self.root)
        self.assertEqual(hold["phase"], "sealing")
        with self.assertRaises(RES.ResidueError) as ctx:
            RES.rollback(self.root)
        self.assertEqual(ctx.exception.code, "residue-past-commit-point")
        resumed = self.apply()
        self.assertEqual(resumed["status"], "complete", resumed)
        self.assertEqual(resumed["resumed_from"], "sealing")
        for cyc in resumed["report"]["cycles"]:
            manifest = json.loads((self.root / cyc["cycle_dir"] / "manifest.json").read_text())
            for rev in manifest["artifact_revisions"]:
                self.assertTrue((self.root / cyc["cycle_dir"] / rev["locator"]["path"]).is_file(), rev["locator"]["path"])
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "empty")

    def test_reserved_manifest_name_is_sanitized_and_seals(self):
        self.assertEqual(RES.sanitize_component("manifest.json"), "manifest-ffa5b716.json")
        historical, _historical_name = self.migrated_plan_cycle(
            d1="2026-08-18_historical", files=("historical.md",))
        historical_dir = P.cycle_dir(self.root, historical["campaign_id"], historical["cycle_id"])
        historical_manifest = historical_dir / "manifest.json"
        historical_payload = historical_dir / "artifacts" / "plans" / "2026-08-18_historical" / "historical.md"
        historical_record = P.cycle_record_path(self.root, historical["cycle_id"])
        compat_map = Path(C.load_map_state(self.root)["maps"][-1]["path"])
        protected = (historical_record, historical_dir / ".cycle.json", historical_manifest,
                     historical_payload, compat_map)
        historical_before = {str(path): path.read_bytes() for path in protected}
        self.seed("plans/2026-08-19_pilot/evidence/staging/campaigns/x/cycles/y/manifest.json", "{}")
        plan = RES.build_plan(self.root)
        target = plan["cycles"][0]["files"][0]["target"]
        self.assertNotEqual(Path(target).name, "manifest.json")
        self.assertTrue(Path(target).name.startswith("manifest-") and target.endswith(".json"), target)
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        resolved = C.resolve_legacy(self.root, "plans/2026-08-19_pilot/evidence/staging/campaigns/x/cycles/y/manifest.json")
        self.assertEqual(resolved["resolution"], "mapped")
        self.assertEqual({str(path): path.read_bytes() for path in protected}, historical_before)

    def test_cycles_are_dated_without_backdating_the_clock(self):
        from unittest import mock
        self.seed("plans/2026-07-01_never-migrated/plan.md")
        seen = []
        real_begin = P.begin

        def spy(root, **kw):
            seen.append(kw.get("now"))
            return real_begin(root, **kw)

        with mock.patch.object(P, "begin", spy):
            result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(seen, [None])
        cyc = result["report"]["cycles"][0]
        record = P.read_cycle_record(self.root, cyc["cycle_id"])
        self.assertEqual(record["started_on"][:10], "2026-07-01")
        self.assertTrue(record["locator"].startswith("2026-07-01_"))
        self.assertEqual(P.cycle_dir(self.root, record["campaign_id"], cyc["cycle_id"]), self.root / cyc["cycle_dir"])

    def test_residue_hold_blocks_relayout(self):
        self.seed("_internal/dev_reviews/phase.md")
        with self.assertRaises(RES.ResidueError):
            self.apply(crash_after_phase="renamed")
        with self.assertRaises(RL.RelayoutError) as ctx:
            RL.apply(self.root, jobs_path=self.jobs)
        self.assertEqual(ctx.exception.code, "residue-in-progress")
        self.assertEqual(self.apply()["status"], "rolled-back")


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

    def test_spec_top_is_deferred_unless_included(self):
        import io
        from contextlib import redirect_stdout
        self.seed("spec/prd.md", "# PRD\n")
        self.seed("spec/.pipeline-lock", "")
        self.seed("spec/_internal/versions/v1/prd.md", "# v1\n")
        shas = {rel: _sha(self.root / rel) for rel in ("spec/prd.md", "spec/.pipeline-lock", "spec/_internal/versions/v1/prd.md")}
        # The spec lane needs a shared/spec revision to fall back to (review M6).
        self.seed(f"shared/spec/ref_{'a' * 32}/revisions/rrev_{'b' * 32}/prd.md", "# shared\n")
        self.assertIsNotNone(C.latest_shared_revision(self.root, "spec"))
        # An earlier W7H run already sealed this root's `support:root` residue
        # cycle (cairn's case); the spec lane opens a second one later.
        self.seed("_internal/dev_reviews/phase.md")
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["movable"], 1)
        self.assertEqual({d["reason"] for d in plan["deferred"]}, {"spec-consumer-pinned"})
        first = self.apply()
        self.assertEqual(first["status"], "complete", first)
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "deferred-only")
        self.assertEqual(self.apply()["status"], "no-op")
        included = RES.build_plan(self.root, include_spec_top=True)
        self.assertEqual(included["totals"]["movable"], 3)
        self.assertEqual(included["deferred"], [])
        self.assertTrue(included["include_spec_top"])
        self.assertIn("D-23-b-spec-top", {s["id"] for s in included["spec_impact"]})
        (cycle,) = included["cycles"]
        self.assertEqual(cycle["group"], "support:root")
        by_path = {f["path"]: f for f in cycle["files"]}
        self.assertEqual(by_path["spec/prd.md"]["target"], "artifacts/_internal/spec/prd.md")
        self.assertEqual(by_path["spec/.pipeline-lock"]["locator_renamed_from"], "spec/.pipeline-lock")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(RES.main(["--artifact-root", str(self.root), "plan", "--include-spec-top"]), 0)
        self.assertEqual(json.loads(out.getvalue())["totals"]["movable"], 3)
        result = self.apply(include_spec_top=True)
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue(result["report"]["include_spec_top"])
        self.assertFalse((self.root / "spec").exists())
        view = RES.status(self.root)
        self.assertEqual(view["legacy_top_level"], "empty")
        rows = {r["source_locator"]: r for r in self.last_map_rows()}
        self.assertEqual(set(rows), set(shas))
        for rel, sha in shas.items():
            self.assertEqual(rows[rel]["sha256"], "sha256:" + sha)
            self.assertEqual(_sha(self.root / rows[rel]["target_locator"]), sha)
            self.assertEqual(RD.resolve_path(self.root, rel)["resolution"], "mapped")
        row = {"repo_path": "/x", "state": "active", "probe": {"passed": True}, "lumped_cycles_remaining": 0,
               "legacy_top_level_retired": True, "readable_layout": "readable", "relayout_hold": None,
               "transition_window": "closed", **G._residue_fields(self.root)}
        verdict, blocking = G.evaluate([row], waived=False, require_resplit=True, require_relayout=True, require_residue=True)
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


class RejoinTests(ResidueFixture):
    """A live symlink rejoins the migrated home of its siblings by rename (W7H symlink rejoin)."""

    def corpus(self, name="EN2002a.wav"):
        path = Path(self._tmp.name) / "corpus" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF" + name.encode())
        return path

    def gate_row(self):
        return {"repo_path": "/x", "state": "active", "probe": {"passed": True}, "lumped_cycles_remaining": 0,
                "legacy_top_level_retired": True, "readable_layout": "readable", "relayout_hold": None,
                "transition_window": "closed", **G._residue_fields(self.root)}

    def test_absolute_live_symlink_rejoins_its_migrated_siblings(self):
        # File rows only, as the real roots have them (W7C wrote one row per file).
        sealed, d1 = self.migrated_plan_cycle(files=("plan/plan.md", "STORY.md"), directory_row=False)
        cycle_rel = self.sealed_cycle_rel(sealed)
        manifest_before = (self.root / cycle_rel / "manifest.json").read_bytes()
        wav = self.corpus()
        legacy = f"plans/{d1}/audio_fullband/EN2002a.wav"
        (self.root / legacy).parent.mkdir(parents=True)
        (self.root / legacy).symlink_to(wav)
        self.assertEqual(RD.resolve_path(self.root, legacy)["resolution"], "unresolved")
        plan = RES.build_plan(self.root)
        self.assertEqual((plan["totals"]["rejoined"], plan["totals"]["deferred"], plan["totals"]["movable"]), (1, 0, 1))
        self.assertEqual(plan["by_disposition"], {"rejoin": 1})
        (rejoin,) = plan["rejoins"]
        self.assertEqual(rejoin["target"], f"{cycle_rel}/artifacts/plans/{d1}/audio_fullband/EN2002a.wav")
        self.assertEqual((rejoin["ancestor"], rejoin["rows"], rejoin["cycle_id"]), (f"plans/{d1}", 2, sealed["cycle_id"]))
        self.assertTrue(rejoin["link_absolute"])
        self.assertIn("D-23-b-symlink-rejoin", {s["id"] for s in plan["spec_impact"]})
        # A rejoinable link is a move, never a deletion candidate.
        self.assertEqual(RES.trash_inventory(self.root, include_symlinks=True)["entry_count"], 0)
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "residue")
        self.assertEqual(G.evaluate([self.gate_row()], waived=False, require_residue=True)[1][0]["reason"], "residue-remaining")
        inode = (self.root / legacy).lstat().st_ino
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        report = result["report"]
        self.assertEqual((report["totals"]["rejoined"], report["cycles"], report["witness"]["files_checked"]), (1, [], 1))
        moved = self.root / rejoin["target"]
        self.assertTrue(moved.is_symlink())
        self.assertEqual(os.readlink(moved), str(wav))
        self.assertEqual(moved.lstat().st_ino, inode)  # renamed, not recreated
        self.assertEqual(moved.read_bytes(), wav.read_bytes())
        self.assertFalse((self.root / "plans").exists())
        self.assertIn(f"plans/{d1}/audio_fullband", report["pruned_dirs"])
        self.assertEqual((self.root / cycle_rel / "manifest.json").read_bytes(), manifest_before)
        (row,) = [r for r in self.last_map_rows() if r["source_locator"] == legacy]
        self.assertEqual(row["kind"], "symlink")
        self.assertEqual((row["link_target"], row["relocated_from"], row["identity_refs"]), (str(wav), legacy, [sealed["cycle_id"]]))
        self.assertEqual(row["sha256"], RES._sha_bytes(str(wav).encode()))
        self.assertEqual(row["target_locator"], rejoin["target"])
        resolved = RD.resolve_path(self.root, legacy)
        self.assertEqual((resolved["resolution"], resolved["target"]), ("mapped", rejoin["target"]))
        view = RES.status(self.root)
        self.assertEqual((view["legacy_top_level"], view["rejoin_pending"], view["symlinks"]), ("empty", 0, 0))
        verdict, blocking = G.evaluate([self.gate_row()], waived=False, require_resplit=True, require_relayout=True,
                                       require_residue=True)
        self.assertEqual(verdict, "complete", blocking)
        self.assertEqual(self.apply()["status"], "no-op")

    def test_relative_symlink_rejoins_when_it_resolves_at_its_new_home(self):
        d1 = "2026-07-27_tts-wwd"
        sealed, _ = self.migrated_plan_cycle(d1, top="experiments", files=("html_report/index.html",), directory_row=False)
        cycle_rel = self.sealed_cycle_rel(sealed)
        # The legacy `html_report/` is an empty shell (only a hidden marker dir), so the link is live there.
        (self.root / "experiments" / d1 / "html_report" / ".visual-harness").mkdir(parents=True)
        legacy = f"experiments/{d1}/report"
        (self.root / legacy).symlink_to("html_report")
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 1)
        (rejoin,) = plan["rejoins"]
        self.assertEqual(rejoin["target"], f"{cycle_rel}/artifacts/experiments/{d1}/report")
        self.assertFalse(rejoin["link_absolute"])
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        moved = self.root / rejoin["target"]
        self.assertEqual(os.readlink(moved), "html_report")
        self.assertEqual((moved / "index.html").read_bytes(), (self.root / cycle_rel / "artifacts" / "experiments" / d1 / "html_report" / "index.html").read_bytes())
        self.assertFalse((self.root / "experiments").exists())  # shell dirs pruned with the link gone
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "empty")

    def test_relative_symlink_that_would_dangle_at_its_new_home_stays_deferred(self):
        d1 = "2026-07-27_tts-wwd"
        self.migrated_plan_cycle(d1, top="experiments", files=("html_report/index.html",), directory_row=False)
        (self.root / "experiments" / d1 / "scratch").mkdir(parents=True)  # live here, nothing of it migrated
        legacy = f"experiments/{d1}/report"
        (self.root / legacy).symlink_to("scratch")
        plan = RES.build_plan(self.root)
        self.assertEqual((plan["totals"]["rejoined"], plan["totals"]["deferred"], plan["totals"]["movable"]), (0, 1, 0))
        (deferred,) = plan["deferred"]
        self.assertEqual(deferred["reason"], "symlink-relative-unresolvable")
        self.assertFalse(deferred["dangling"])
        self.assertTrue(deferred["rejoin_target"].endswith(f"/artifacts/experiments/{d1}/report"))
        self.assertEqual(self.apply()["status"], "no-op")
        self.assertTrue((self.root / legacy).is_symlink())
        view = RES.status(self.root)
        self.assertEqual((view["legacy_top_level"], view["symlinks"], view["rejoin_pending"]), ("deferred-only", 1, 0))
        self.assertEqual(G.evaluate([self.gate_row()], waived=False, require_residue=True)[0], "complete")

    def test_live_symlink_without_a_mapped_ancestor_stays_deferred(self):
        self.migrated_plan_cycle()
        wav = self.corpus()
        legacy = "experiments/2026-08-01_never-migrated/link.wav"
        (self.root / legacy).parent.mkdir(parents=True)
        (self.root / legacy).symlink_to(wav)
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 0)
        self.assertEqual([(d["reason"], d["dangling"]) for d in plan["deferred"]], [("symlink", False)])
        self.assertEqual(self.apply()["status"], "no-op")
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "deferred-only")

    def test_rejoin_prefers_the_home_most_siblings_went_to(self):
        # Origin cycle: two plan files. A later residue cycle: one support file
        # of the same legacy directory under `artifacts/_internal/...`.
        origin, d1 = self.migrated_plan_cycle(files=("plan/plan.md", "STORY.md"), directory_row=False)
        _route, residue = self.cycle(slug="residue", title="residue", files=(f"_internal/plans/{d1}/_internal/x.md",))
        residue_rel = self.sealed_cycle_rel(residue)
        later = Path(self._tmp.name) / "residue-map.jsonl"
        target = f"{residue_rel}/artifacts/_internal/plans/{d1}/_internal/x.md"
        C._write_jsonl(later, [{"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": f"plans/{d1}/_internal/x.md",
                                "target_locator": target, "sha256": "sha256:" + _sha(self.root / target), "identity_refs": []}])
        C.compat_append(self.root, maps=[later], supersedes=[])
        found = RES.rejoin_target(self.root, f"plans/{d1}/audio/EN2002a.wav")
        self.assertEqual(found["mapped_ancestor"], f"{self.sealed_cycle_rel(origin)}/artifacts/plans/{d1}")
        self.assertEqual(found["rows"], 2)
        # The nearest ancestor wins outright when it is mapped at all.
        nearer = RES.rejoin_target(self.root, f"plans/{d1}/_internal/link.wav")
        self.assertEqual(nearer["mapped_ancestor"], f"{residue_rel}/artifacts/_internal/plans/{d1}/_internal")
        # A bare bucket is never a home: an unknown `<d1>` under `plans/` stays unmapped.
        self.assertIsNone(RES.rejoin_target(self.root, "plans/2026-01-01_unknown/link.wav"))
        self.assertIsNone(RES.rejoin_target(self.root, "plans/link.wav"))
        self.assertIsNone(RES.rejoin_target(self.root, "link.wav"))

    def test_rejoined_symlink_rolls_back_as_a_link(self):
        sealed, d1 = self.migrated_plan_cycle(directory_row=False)
        self.seed(f"plans/{d1}/_internal/plan_reviews/round_1.md")  # forces a residue cycle -> rollback path exists
        wav = self.corpus()
        legacy = f"plans/{d1}/audio_fullband/EN2002a.wav"
        (self.root / legacy).parent.mkdir(parents=True)
        (self.root / legacy).symlink_to(wav)
        with self.assertRaises(RES.ResidueError):
            self.apply(crash_after_phase="renamed")
        self.assertFalse((self.root / legacy).is_symlink())
        resumed = self.apply()
        self.assertEqual(resumed["status"], "rolled-back", resumed)
        self.assertTrue((self.root / legacy).is_symlink())
        self.assertEqual(os.readlink(self.root / legacy), str(wav))
        cycle_dir = self.root / self.sealed_cycle_rel(sealed)
        self.assertEqual([p for p in cycle_dir.rglob("*") if p.is_symlink()], [])
        done = self.apply()
        self.assertEqual(done["status"], "complete", done)
        self.assertEqual(done["report"]["totals"]["rejoined"], 1)

    def test_occupied_target_defers_the_link_instead_of_failing_the_run(self):
        # Review B1: a migrated sibling already holds the name the link would take.
        sealed, d1 = self.migrated_plan_cycle(files=("STORY.md",), directory_row=False)
        self.seed(f"plans/{d1}/_internal/plan_reviews/round_1.md")  # ordinary residue in the same run
        (self.root / "plans" / d1 / "STORY.md").symlink_to(self.corpus())
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 0)
        (deferred,) = plan["deferred"]
        self.assertEqual(deferred["reason"], "symlink-target-occupied")
        self.assertTrue(deferred["rejoin_target"].endswith(f"/artifacts/plans/{d1}/STORY.md"))
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)  # the residue file still moves
        self.assertTrue((self.root / "plans" / d1 / "STORY.md").is_symlink())
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "deferred-only")
        self.assertEqual(G.evaluate([self.gate_row()], waived=False, require_residue=True)[0], "complete")

    def test_rejoin_never_lands_in_a_shared_revision(self):
        # Review B2: a compat row into `shared/<kind>/.../rrev_*` lends an ancestor home that is not a cycle.
        rev = self.root / "shared" / "analysis" / ("ref_" + "d" * 32) / "revisions" / ("rrev_" + "c" * 32) / "code"
        rev.mkdir(parents=True)
        (rev / "audit.md").write_text("audit\n")
        later = Path(self._tmp.name) / "shared-map.jsonl"
        C._write_jsonl(later, [{"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "analysis_project/code/audit.md",
                                "target_locator": rev.relative_to(self.root).as_posix() + "/audit.md",
                                "sha256": "sha256:" + _sha(rev / "audit.md"), "identity_refs": []}])
        C.compat_close(self.root, maps=[later], approval_receipt_sha256=None)
        (self.root / "analysis_project" / "code" / "evidence").mkdir(parents=True)
        (self.root / "analysis_project" / "code" / "evidence" / "run.wav").symlink_to(self.corpus())
        self.assertIsNone(RES.rejoin_target(self.root, "analysis_project/code/evidence/run.wav"))
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 0)
        self.assertEqual(plan["deferred"][0]["reason"], "symlink")
        self.assertEqual([p for p in rev.rglob("*") if p.is_symlink()], [])

    def test_absolute_link_whose_target_this_run_moves_is_deferred_and_backstopped(self):
        # Review B3: the link is live at plan time; its target (a legacy note) moves in the same run.
        sealed, d1 = self.migrated_plan_cycle(directory_row=False)
        note = self.seed("notes/keep/data.bin", b"\x00\x01")
        (self.root / "plans" / d1 / "audio").mkdir(parents=True)
        legacy = f"plans/{d1}/audio/data.bin"
        (self.root / legacy).symlink_to(note)
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 0)
        (deferred,) = plan["deferred"]
        self.assertEqual(deferred["reason"], "symlink-target-moving")
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue((self.root / legacy).is_symlink())
        self.assertFalse((self.root / legacy).exists())  # now dangling, still at the legacy path, typed
        view = RES.status(self.root)
        self.assertEqual((view["legacy_top_level"], view["symlinks_dangling"]), ("deferred-only", 1))
        # Backstop: a rename-time drift (target vanished after planning) is refused before the rename.
        wav = self.corpus("late.wav")
        (self.root / "plans" / d1 / "audio" / "late.wav").symlink_to(wav)
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 1)
        wav.unlink()
        run_dir = Path(self._tmp.name) / "run-b3"; run_dir.mkdir()
        with self.assertRaises(RES.ResidueError) as ctx:
            RES._phase_rename(self.root, run_dir, {"begun": {}}, plan)
        self.assertEqual(ctx.exception.code, "symlink-unresolvable")
        self.assertTrue((self.root / "plans" / d1 / "audio" / "late.wav").is_symlink())

    def test_nested_relative_link_resolves_through_a_parent_this_run_creates(self):
        # Review M1: the link sits one directory below the mapped ancestor; its `..` target must fold lexically.
        d1 = "2026-07-27_tts-wwd"
        sealed, _ = self.migrated_plan_cycle(d1, top="experiments", files=("html/index.html",), directory_row=False)
        (self.root / "experiments" / d1 / "html").mkdir(parents=True)  # empty legacy shell keeps the link live
        (self.root / "experiments" / d1 / "sub").mkdir()
        legacy = f"experiments/{d1}/sub/up"
        (self.root / legacy).symlink_to("../html")
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 1, plan["deferred"])
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        moved = self.root / plan["rejoins"][0]["target"]
        self.assertEqual(os.readlink(moved), "../html")
        self.assertTrue((moved / "index.html").is_file())
        self.assertEqual(RES.status(self.root)["legacy_top_level"], "empty")

    def test_rejoin_only_run_can_roll_back_from_any_phase(self):
        # Review M3: no cycle is ever sealed, so there is no commit point to be past.
        sealed, d1 = self.migrated_plan_cycle(directory_row=False)
        wav = self.corpus()
        legacy = f"plans/{d1}/audio_fullband/EN2002a.wav"
        (self.root / legacy).parent.mkdir(parents=True)
        (self.root / legacy).symlink_to(wav)
        compat_before = C.compat_path(self.root).read_bytes()
        for phase in ("sealed", "compat-reissued", "indexed"):
            with self.subTest(phase=phase):
                with self.assertRaises(RES.ResidueError):
                    self.apply(crash_after_phase=phase)
                rolled = RES.rollback(self.root)
                self.assertEqual(rolled["status"], "rolled-back", rolled)
                self.assertTrue((self.root / legacy).is_symlink())
                self.assertEqual(os.readlink(self.root / legacy), str(wav))
                self.assertEqual(C.compat_path(self.root).read_bytes(), compat_before)
                self.assertIsNone(RES.residue_hold(self.root))
        done = self.apply()
        self.assertEqual(done["status"], "complete", done)
        self.assertEqual(done["report"]["totals"]["rejoined"], 1)

    def test_spec_top_refuses_when_no_shared_spec_fallback_exists(self):
        # Review M6: moving the only PRD a root has would leave the spec gate with nothing to read.
        self.seed("spec/prd.md", "# only copy\n")
        self.assertIsNone(C.latest_shared_revision(self.root, "spec"))
        with self.assertRaises(RES.ResidueError) as ctx:
            RES.build_plan(self.root, include_spec_top=True)
        self.assertEqual(ctx.exception.code, "spec-top-no-shared-fallback")
        self.assertEqual(RES.build_plan(self.root)["totals"]["movable"], 0)  # default lane unaffected

    def test_link_inside_sealed_evidence_tree_stays_with_the_evidence_lane(self):
        # Review M4: the evidence prefix wins over rejoin for a link too.
        evidence = C.SEALED_EVIDENCE_PATHS[2]  # plans/2026-08-25_artifact-knowledge-index-w7-e2-e3
        d1 = evidence.split("/")[1]
        self.migrated_plan_cycle(d1, files=("note.md",), directory_row=False)
        (self.root / evidence / "evidence").mkdir(parents=True)
        (self.root / evidence / "evidence" / "corpus.wav").symlink_to(self.corpus())
        plan = RES.build_plan(self.root)
        self.assertEqual(plan["totals"]["rejoined"], 0)
        self.assertEqual(plan["deferred"][0]["reason"], "symlink-sealed-evidence")

    def test_rejoin_refuses_a_link_that_changed_after_planning(self):
        sealed, d1 = self.migrated_plan_cycle(directory_row=False)
        wav = self.corpus()
        legacy = f"plans/{d1}/audio_fullband/EN2002a.wav"
        (self.root / legacy).parent.mkdir(parents=True)
        (self.root / legacy).symlink_to(wav)
        plan = RES.build_plan(self.root)
        journal = {"begun": {}}
        run_dir = Path(self._tmp.name) / "run"
        run_dir.mkdir()
        (self.root / legacy).unlink()
        (self.root / legacy).symlink_to(self.corpus("other.wav"))
        with self.assertRaises(RES.ResidueError) as ctx:
            RES._phase_rename(self.root, run_dir, journal, plan)
        self.assertEqual(ctx.exception.code, "residue-link-drifted")
        self.assertTrue((self.root / legacy).is_symlink())
        self.assertFalse(os.path.lexists(self.root / plan["rejoins"][0]["target"]))


if __name__ == "__main__":
    unittest.main()
