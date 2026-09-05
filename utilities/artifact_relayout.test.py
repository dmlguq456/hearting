#!/usr/bin/env python3
"""W7I Cycle B relayout tests (A-17.6 preservation, A-17.7 naming, A-17.8 rollback).

Fixtures are built through the real producer in the readable layout and then
"legacyized" back into the historical ``campaigns/camp_<id>/cycles/cyc_<id>``
shape with the pre-W7I record fields, so the migration is exercised against
exactly the data shape the ten fleet roots hold.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_cutover as C  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_reader as RD  # noqa: E402
import artifact_relayout as RL  # noqa: E402
import fleet_cutover_gate as G  # noqa: E402

_PT_SPEC = importlib.util.spec_from_file_location(
    "producer_test_for_relayout", Path(__file__).with_name("artifact_producer.test.py"))
PT = importlib.util.module_from_spec(_PT_SPEC)
_PT_SPEC.loader.exec_module(PT)
R = PT.R

AUTO_CAMPAIGN_TITLE = "autopilot-code campaign"
AUTO_CYCLE_TITLE = "autopilot-code standard cycle"
AUTO_GOAL = "autopilot-code cycle output"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path, *, skip=(".runtime/artifact-producer/v1/journal", ".runtime/artifact-producer/v1/migrations",
                                    ".runtime/artifact-admission", "campaigns/INDEX")):
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in skip):
            continue
        out[rel] = _sha(path)
    return out


def _content_map(root: Path):
    """``<cycle_id> -> {artifacts-relative path: sha}`` for every cycle folder."""
    out = {}
    for campaign in artifact_locator.iter_campaign_dirs(root):
        for cycle_dir, _layout in artifact_locator.iter_cycle_dirs(campaign):
            manifest = json.loads((cycle_dir / "manifest.json").read_text()) if (cycle_dir / "manifest.json").is_file() else None
            binding = artifact_locator.read_cycle_binding(cycle_dir)
            cycle_id = (manifest or {}).get("cycle", {}).get("cycle_id") or (binding or {}).get("cycle_id") or cycle_dir.name
            files = {}
            for path in sorted((cycle_dir / "artifacts").rglob("*")):
                if path.is_file():
                    files[path.relative_to(cycle_dir / "artifacts").as_posix()] = _sha(path)
            out[cycle_id] = {"files": files, "manifest": _sha(cycle_dir / "manifest.json") if manifest else None,
                             "dir": cycle_dir.relative_to(root).as_posix()}
    return out


class RelayoutFixture(PT.ProducerTestBase):
    """Readable-layout producer root that can be pushed back into the legacy shape."""

    def setUp(self):
        super().setUp()
        self.activate()
        self.jobs = Path(self._tmp.name) / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")

    def slugless_route(self, capability="autopilot-code", mode="dev"):
        gate = PT.gate_evidence()
        route = R.compile_route(capability, mode, "direct", predicates=PT.ALL, transport=None,
                                inline_reason="atomic-direct", cwd=R.ROOT, artifact_root=self.root,
                                tracking="tracked", tracked_gate_evidence=gate)
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def cycle(self, *, slug="w7i-test", title=None, files=("plans/cycle/plan.md",), seal=True,
              close_route=True, campaign_key=None, goal=None, intensity="direct"):
        route, route_file = self.route(intensity, slug=slug) if slug else self.slugless_route()
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity=intensity,
                         title=title, campaign_key=campaign_key, goal=goal)
        for idx, rel in enumerate(files):
            self.write_output(result, rel, f"body {rel} {idx}\n".encode())
        if close_route:
            self.close(route, route_file)
        if seal:
            sealed = P.finalize(self.root, cycle_id=result["cycle_id"], state="completed")
            self.assertEqual(sealed["status"], "sealed", sealed)
        return route, result

    def route(self, intensity="direct", capability="autopilot-code", mode="dev", **compile_kw):
        route = PT.compile_for(intensity, self.root, capability, mode, **compile_kw)
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def attempt_row(self, route_id, slug):
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write("\t".join([
                "2026-09-01T00:00:00Z", "done", "/cwd", "/cwd", slug,
                f"capability=autopilot-code,route_id={route_id},harness=claude",
            ]) + "\n")

    def legacyize(self, *, keep_titles=False, keep_campaign_titles=None):
        """Push every readable campaign/cycle into the pre-W7I physical and record shape."""
        keep_campaign_titles = keep_titles if keep_campaign_titles is None else keep_campaign_titles
        campaigns = self.root / "campaigns"
        for campaign_dir in list(artifact_locator.iter_campaign_dirs(self.root)):
            record = json.loads((campaign_dir / "campaign.json").read_text())
            campaign_id = record["campaign_id"]
            for cycle_dir, layout in list(artifact_locator.iter_cycle_dirs(campaign_dir)):
                if layout != "readable":
                    continue
                binding = artifact_locator.read_cycle_binding(cycle_dir)
                manifest = P._read_json(cycle_dir / "manifest.json")
                cycle_id = (manifest or {}).get("cycle", {}).get("cycle_id") or binding["cycle_id"]
                (cycle_dir / artifact_locator.CYCLE_BINDING).unlink()
                target = campaign_dir / "cycles" / cycle_id
                target.parent.mkdir(exist_ok=True)
                os.rename(cycle_dir, target)
                cycle_record = P.read_cycle_record(self.root, cycle_id)
                for key in ("slug", "slug_source", "slug_truncated", "locator", "locator_suffix"):
                    cycle_record.pop(key, None)
                if not keep_titles:
                    cycle_record["title"] = AUTO_CYCLE_TITLE
                P._write_cycle_record(self.root, cycle_record, exclusive=False)
            for key in ("slug", "slug_source", "slug_truncated", "locator", "locator_suffix"):
                record.pop(key, None)
            if not keep_campaign_titles:
                record["title"] = AUTO_CAMPAIGN_TITLE
                record["goal"] = AUTO_GOAL
            (campaign_dir / "campaign.json").write_bytes(P._json_bytes(record))
            if campaign_dir.name != campaign_id:
                os.rename(campaign_dir, campaigns / campaign_id)
        for name in (artifact_locator.INDEX_JSON, artifact_locator.INDEX_MD):
            path = campaigns / name
            if path.exists():
                path.unlink()
        adm.rebuild_index(self.root)
        if not C.compat_path(self.root).is_file():
            C.compat_close(self.root, maps=[], approval_receipt_sha256=None)

    def legacy_dirs(self):
        campaigns = self.root / "campaigns"
        camp = sorted(p.name for p in campaigns.iterdir() if p.is_dir() and p.name.startswith("camp_"))
        containers = sorted(str(p.relative_to(self.root)) for p in campaigns.rglob("cycles") if p.is_dir())
        return camp, containers

    def apply(self, **kw):
        kw.setdefault("jobs_path", self.jobs)
        return RL.apply(self.root, **kw)


class PreservationTests(RelayoutFixture):
    """A-17.6: bytes, digests, manifests, IDs and old locators survive."""

    def test_relayout_preserves_content_and_resolves_old_locators(self):
        _route_a, first = self.cycle(slug="alpha-work", title="Alpha work",
                                     files=("plans/cycle/plan.md", "plans/cycle/notes/n.md"))
        _route_b, second = self.cycle(slug="beta-work", title="Beta work", files=("analysis_project/code/a.md",))
        self.legacyize(keep_titles=True)
        camp_dirs, containers = self.legacy_dirs()
        self.assertEqual(len(camp_dirs), 2)
        self.assertEqual(len(containers), 2)
        before = _content_map(self.root)
        old_cycle_rel = before[first["cycle_id"]]["dir"]
        self.assertIn("/cycles/", old_cycle_rel)
        # Mutable records (campaign.json) gain display fields; everything else is byte-stable.
        before_bytes = sum(p.stat().st_size for p in (self.root / "campaigns").rglob("*")
                           if p.is_file() and p.name != "campaign.json")

        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        report = result["report"]
        self.assertEqual(report["totals"]["cycles"], 2)
        self.assertEqual(report["totals"]["campaign_dirs_renamed"], 2)
        self.assertEqual(report["witness"]["cycles_checked"], 2)

        camp_dirs, containers = self.legacy_dirs()
        self.assertEqual(camp_dirs, [])
        self.assertEqual(containers, [])
        after = _content_map(self.root)
        self.assertEqual(set(after), set(before))
        for cycle_id, row in before.items():
            self.assertEqual(after[cycle_id]["files"], row["files"], cycle_id)
            self.assertEqual(after[cycle_id]["manifest"], row["manifest"], "sealed manifest bytes changed")
            self.assertNotEqual(after[cycle_id]["dir"], row["dir"])
            self.assertNotIn("cycles/", after[cycle_id]["dir"])
            self.assertNotIn("camp_", after[cycle_id]["dir"])
            self.assertNotIn("cyc_", after[cycle_id]["dir"])
        after_bytes = sum(p.stat().st_size for p in (self.root / "campaigns").rglob("*") if p.is_file())
        # INDEX.md/INDEX.json plus .cycle.json bindings are the only new bytes.
        machine = {artifact_locator.INDEX_JSON, artifact_locator.INDEX_MD, artifact_locator.CYCLE_BINDING,
                   "campaign.json"}
        after_content_bytes = sum(p.stat().st_size for p in (self.root / "campaigns").rglob("*")
                                  if p.is_file() and p.name not in machine)
        self.assertEqual(after_content_bytes, before_bytes)
        self.assertGreater(after_bytes, before_bytes)

        # Old ID locators resolve through the compat chain: file, directory, ancestor.
        old_file = f"{old_cycle_rel}/artifacts/plans/cycle/plan.md"
        resolved = C.resolve_legacy(self.root, old_file)
        self.assertEqual(resolved["resolution"], "mapped", resolved)
        self.assertEqual(resolved["target"], f"{after[first['cycle_id']]['dir']}/artifacts/plans/cycle/plan.md")
        resolved_dir = C.resolve_legacy(self.root, old_cycle_rel)
        self.assertEqual(resolved_dir["target"], after[first["cycle_id"]]["dir"])
        resolved_manifest = C.resolve_legacy(self.root, f"{old_cycle_rel}/manifest.json")
        self.assertIn(resolved_manifest["resolution"], {"mapped", "mapped-ancestor"})
        self.assertTrue(Path(resolved_manifest["absolute"]).is_file())
        campaign_old = old_cycle_rel.split("/cycles/")[0]
        resolved_campaign = C.resolve_legacy(self.root, f"{campaign_old}/campaign.json")
        self.assertEqual(resolved_campaign["resolution"], "mapped-ancestor", resolved_campaign)
        self.assertTrue(Path(resolved_campaign["absolute"]).is_file())
        self.assertEqual(RD.resolve_path(self.root, old_file)["target"], resolved["target"])

        # Records win: stable IDs unchanged, producer and locator resolve the new path.
        record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertEqual(record["campaign_id"], first["campaign_id"])
        self.assertEqual(record["locator"], Path(after[first["cycle_id"]]["dir"]).name)
        self.assertEqual(record["legacy_locator"], first["cycle_id"])
        self.assertEqual(record["slug_source"], "relayout:record")
        self.assertEqual(P.cycle_dir(self.root, first["campaign_id"], first["cycle_id"]),
                         self.root / after[first["cycle_id"]]["dir"])
        self.assertEqual(artifact_locator.resolve_path(self.root, first["cycle_id"]),
                         self.root / after[first["cycle_id"]]["dir"])
        self.assertEqual(artifact_locator.resolve_path(self.root, first["campaign_id"]),
                         (self.root / after[first["cycle_id"]]["dir"]).parent)
        campaign = P.read_campaign(self.root, first["campaign_id"])
        self.assertEqual(campaign["legacy_locator"], first["campaign_id"])
        self.assertTrue(campaign["locator"].startswith("20"))

        # Admission index cycle_path follows the move and a rebuild agrees.
        index = adm.load_index(self.root)
        for cycle_id in (first["cycle_id"], second["cycle_id"]):
            self.assertEqual(index.cycles[cycle_id]["cycle_path"], after[cycle_id]["dir"])
        rebuilt, _fallback = adm._compute_rebuilt_index(self.root)
        self.assertEqual(adm.artifact_index.canonical_bytes(rebuilt), adm.artifact_index.canonical_bytes(index))

        # Human index lists the readable paths.
        index_md = (self.root / "campaigns" / artifact_locator.INDEX_MD).read_text()
        self.assertIn(after[first["cycle_id"]]["dir"], index_md)
        self.assertEqual(RL.status(self.root)["readable_layout"], "readable")
        self.assertEqual(RL.status(self.root)["transition_window"], "closed")
        self.assertIsNone(RL.relayout_hold(self.root))
        self.assertIsNone(RD.migration_hold(self.root))

    def test_second_apply_is_no_op_and_suffixes_stay_fixed(self):
        self.cycle(slug="same-day-a", title="Same day")
        self.cycle(slug="same-day-b", title="Same day", campaign_key="k1")
        self.legacyize(keep_titles=True)
        self.apply()
        campaigns = sorted(p.name for p in (self.root / "campaigns").iterdir() if p.is_dir())
        self.assertEqual(len(campaigns), 2)
        base = campaigns[0]
        self.assertEqual(campaigns[1], base + "-2")
        suffixes = {r["campaign_id"]: r["locator_suffix"] for r in
                    (json.loads((self.root / "campaigns" / c / "campaign.json").read_text()) for c in campaigns)}
        again = self.apply()
        self.assertEqual(again["status"], "no-op")
        for name in campaigns:
            record = json.loads((self.root / "campaigns" / name / "campaign.json").read_text())
            self.assertEqual(record["locator_suffix"], suffixes[record["campaign_id"]])
            self.assertEqual(record["locator"], name)

    def test_transition_window_closes_after_relayout(self):
        self.cycle(slug="named", title="Named")
        self.legacyize(keep_titles=True)
        # Before: a slugless route is still named by derivation (D-91 window open).
        route, route_file = self.slugless_route()
        opened = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                         title="derived name")
        self.assertEqual(P.read_cycle_record(self.root, opened["cycle_id"])["slug_source"], "derived-legacy-route")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=opened["cycle_id"], state="completed")
        self.apply()
        _route, route_file = self.slugless_route(mode="debug")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "route-slug-missing")
        # A slug-bearing route keeps working and lands readable.
        _route, sealed = self.cycle(slug="after-window", title="After window")
        self.assertNotIn("cycles", Path(sealed["cycle_dir"]).relative_to(self.root).parts)

    def test_hybrid_root_moves_only_legacy_and_keeps_readable_children(self):
        self.cycle(slug="old-one", title="Old one")
        self.legacyize(keep_titles=True)
        _route, fresh = self.cycle(slug="fresh-one", title="Fresh one")
        fresh_dir = Path(fresh["cycle_dir"])
        self.assertTrue(fresh_dir.is_dir())
        snapshot = {p: _sha(p) for p in fresh_dir.rglob("*") if p.is_file()}
        result = self.apply()
        self.assertEqual(result["report"]["totals"]["cycles"], 1)
        self.assertEqual({p: _sha(p) for p in fresh_dir.rglob("*") if p.is_file()}, snapshot)
        self.assertEqual(RL.status(self.root)["readable_layout"], "readable")

    def test_dry_run_leaves_no_trace(self):
        self.cycle(slug="dry", title="Dry")
        self.legacyize(keep_titles=True)
        before = _snapshot(self.root, skip=(".runtime/artifact-producer/v1/journal",))
        result = self.apply(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(result["report"]["totals"]["cycles"], 1)
        self.assertEqual(_snapshot(self.root, skip=(".runtime/artifact-producer/v1/journal",)), before)
        self.assertEqual(RL.run_dirs(self.root), [])

    def test_older_map_rows_are_retargeted_and_superseded(self):
        _route, first = self.cycle(slug="mapped", title="Mapped")
        self.legacyize(keep_titles=True)
        old_dir = _content_map(self.root)[first["cycle_id"]]["dir"]
        old_target = f"{old_dir}/artifacts/plans/cycle/plan.md"
        legacy = self.seed_legacy("plans/legacy.md", (self.root / old_target).read_text())
        older = Path(self._tmp.name) / "older-map.jsonl"
        C._write_jsonl(older, [{
            "schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": "plans/legacy.md",
            "target_locator": old_target, "sha256": "sha256:" + _sha(legacy), "identity_refs": [],
        }])
        C.compat_close(self.root, maps=[older], approval_receipt_sha256=None)
        legacy.unlink()
        self.assertEqual(C.resolve_legacy(self.root, "plans/legacy.md")["target"], old_target)
        result = self.apply()
        self.assertEqual(result["report"]["compat"]["retargeted_rows"], 1)
        resolved = C.resolve_legacy(self.root, "plans/legacy.md")
        self.assertEqual(resolved["resolution"], "mapped")
        self.assertNotIn("/cycles/", resolved["target"])
        self.assertTrue(Path(resolved["absolute"]).is_file())
        state = C.load_map_state(self.root)
        self.assertEqual(len(state["maps"]), 2)
        self.assertEqual(state["maps"][0]["superseded_by"], state["maps"][1]["sha256"])
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["drifted"], [])

    def test_live_open_cycle_is_a_typed_refusal(self):
        self.cycle(slug="sealed-one", title="Sealed one")
        self.cycle(slug="live-one", title="Live one", seal=False, close_route=False)
        self.legacyize(keep_titles=True)
        with self.assertRaises(RL.RelayoutError) as ctx:
            self.apply()
        self.assertEqual(ctx.exception.code, "relayout-live-open-cycle")
        camp_dirs, containers = self.legacy_dirs()
        self.assertEqual(len(camp_dirs), 2)
        self.assertEqual(RL.run_dirs(self.root), [])

    def test_stale_open_cycle_with_closed_route_moves(self):
        _route, opened = self.cycle(slug="stale-open", title="Stale open", seal=False, close_route=True)
        self.legacyize(keep_titles=True)
        result = self.apply()
        self.assertEqual(result["status"], "complete")
        record = P.read_cycle_record(self.root, opened["cycle_id"])
        self.assertEqual(record["state"], "open")
        new_dir = P.cycle_dir(self.root, opened["campaign_id"], opened["cycle_id"])
        self.assertTrue((new_dir / "artifacts" / "plans" / "cycle" / "plan.md").is_file())
        self.assertNotIn("cycles", new_dir.relative_to(self.root).parts)
        # The stale open cycle can still be finalized in its new home.
        sealed = P.finalize(self.root, cycle_id=opened["cycle_id"], state="completed")
        self.assertEqual(sealed["status"], "sealed", sealed)


class ReviewRegressionTests(RelayoutFixture):
    """Independent review (2026-09-05): preconditions past the commit point."""

    def test_missing_compat_chain_is_refused_before_any_write(self):
        self.cycle(slug="pre", title="Pre")
        self.legacyize(keep_titles=True)
        C.compat_path(self.root).unlink()
        before = _snapshot(self.root, skip=(".runtime/artifact-producer/v1/journal",))
        with self.assertRaises(RL.RelayoutError) as ctx:
            self.apply()
        self.assertEqual(ctx.exception.code, "compat-map-missing")
        self.assertEqual(_snapshot(self.root, skip=(".runtime/artifact-producer/v1/journal",)), before)
        self.assertEqual(RL.run_dirs(self.root), [])
        self.assertIsNone(RL.relayout_hold(self.root))

    def test_drifted_map_file_is_refused_before_any_write(self):
        self.cycle(slug="drift", title="Drift")
        self.legacyize(keep_titles=True)
        older = Path(self._tmp.name) / "older-map.jsonl"
        C._write_jsonl(older, [])
        C.compat_close(self.root, maps=[older], approval_receipt_sha256=None)
        older.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(RL.RelayoutError) as ctx:
            self.apply()
        self.assertEqual(ctx.exception.code, "compat-map-drifted")
        self.assertEqual(RL.run_dirs(self.root), [])

    def test_cycle_unlisted_in_campaign_record_is_repaired(self):
        _route, opened = self.cycle(slug="unlisted", title="Unlisted", seal=False)
        self.legacyize(keep_titles=True)
        campaign = P.read_campaign(self.root, opened["campaign_id"])
        campaign["cycles"] = []
        P._write_campaign(self.root, campaign, exclusive=False)
        mapping, _rows = artifact_locator.scan_index(self.root)
        self.assertNotIn(opened["cycle_id"], mapping)
        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertIn(opened["cycle_id"], P.read_campaign(self.root, opened["campaign_id"])["cycles"])
        mapping, _rows = artifact_locator.scan_index(self.root)
        self.assertIn(opened["cycle_id"], mapping)
        self.assertEqual(artifact_locator.resolve_path(self.root, opened["cycle_id"]),
                         P.cycle_dir(self.root, opened["campaign_id"], opened["cycle_id"]))

    def test_hand_renamed_readable_campaign_with_legacy_cycles(self):
        _route, sealed = self.cycle(slug="drifted", title="Drifted")
        self.legacyize(keep_titles=True)
        # Bring the campaign back to readable shape but with a hand-edited name,
        # while its cycle stays under the legacy `cycles/` container.
        campaign = P.read_campaign(self.root, sealed["campaign_id"])
        old_dir = self.root / "campaigns" / sealed["campaign_id"]
        new_dir = self.root / "campaigns" / "2026-09-05_renamed-by-hand"
        os.rename(old_dir, new_dir)
        campaign.update({"slug": "drifted", "locator": "2026-09-05_drifted", "locator_suffix": "",
                         "slug_source": "route", "slug_truncated": False})
        (new_dir / "campaign.json").write_bytes(P._json_bytes(campaign))
        result = self.apply()
        self.assertEqual(result["status"], "complete", result)
        target = P.cycle_dir(self.root, sealed["campaign_id"], sealed["cycle_id"])
        self.assertEqual(target.parent, new_dir)
        self.assertTrue((target / "manifest.json").is_file())
        self.assertFalse((new_dir / "cycles").exists())
        self.assertEqual(RL.status(self.root)["readable_layout"], "readable")

    def test_status_absorbs_malformed_binding_instead_of_raising(self):
        _route, sealed = self.cycle(slug="bad-binding", title="Bad binding")
        self.legacyize(keep_titles=True)
        cycle_dir = self.root / "campaigns" / sealed["campaign_id"] / "cycles" / sealed["cycle_id"]
        (cycle_dir / artifact_locator.CYCLE_BINDING).write_text("{\"kind\": \"garbage\"}", encoding="utf-8")
        view = RL.status(self.root)
        self.assertEqual(view["readable_layout"], "invalid")
        self.assertEqual(view["error"]["code"], "locator-cycle-binding-invalid")
        row = G._relayout_fields(self.root)
        self.assertEqual(row["readable_layout"], "invalid")

    def test_plan_drift_between_plan_and_lock_is_refused(self):
        self.cycle(slug="drift-a", title="Drift A")
        self.legacyize(keep_titles=True)
        real_build = RL.build_plan
        calls = {"n": 0}

        def racing_build(root, **kw):
            plan = real_build(root, **kw)
            calls["n"] += 1
            if calls["n"] == 1:
                # Another producer opens a legacy-shaped cycle after the unlocked plan.
                extra = self.root / "campaigns" / plan["campaigns"][0]["source_dir"].split("/")[-1] / "cycles" / ("cyc_" + "f" * 32)
                extra.mkdir(parents=True)
                (extra / "manifest.json").write_text("{}", encoding="utf-8")
            return plan

        from unittest import mock
        with mock.patch.object(RL, "build_plan", racing_build):
            with self.assertRaises(RL.RelayoutError) as ctx:
                self.apply()
        self.assertIn(ctx.exception.code, {"relayout-plan-drift", "relayout-cycle-id-invalid", "relayout-orphan-cycle-dir"})
        self.assertEqual(RL.run_dirs(self.root), [])


class NamingTests(RelayoutFixture):
    """A-17.7: every D-92 priority fires once and is journaled."""

    def test_all_five_priorities_fire_and_are_journaled(self):
        # 1 record title
        _r1, c1 = self.cycle(slug="p1", title="Real record title", campaign_key="p1")
        # 2 attempt slug (auto title, registry row)
        r2, c2 = self.cycle(slug="p2", title=None, campaign_key="p2")
        # 3 artifacts top dir (auto title, no registry row, files in plans/)
        _r3, c3 = self.cycle(slug="p3", title=None, campaign_key="p3", files=("plans/x/a.md", "plans/x/b.md", "notes/n.md"))
        # 4 campaign goal (auto title, empty artifacts, real goal) -- open cycle, closed route
        _r4, c4 = self.cycle(slug="p4", title=None, campaign_key="p4", goal="Real campaign goal", files=(), seal=False)
        # 5 unnamed (auto title, empty artifacts, auto goal)
        _r5, c5 = self.cycle(slug="p5", title=None, campaign_key="p5", files=(), seal=False)
        self.legacyize(keep_titles=False)
        # Restore the two real values the legacyize step blanked.
        rec1 = P.read_cycle_record(self.root, c1["cycle_id"]); rec1["title"] = "Real record title"
        P._write_cycle_record(self.root, rec1, exclusive=False)
        camp4 = P.read_campaign(self.root, c4["campaign_id"]); camp4["goal"] = "Real campaign goal"
        P._write_campaign(self.root, camp4, exclusive=False)
        self.attempt_row(r2["route_id"], "attempt-slug-p2")

        plan = RL.build_plan(self.root, jobs_path=self.jobs)
        self.assertEqual(plan["cycle_priority_histogram"], {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1})
        self.assertEqual(plan["totals"]["unnamed_cycles"], 1)
        self.assertAlmostEqual(plan["unnamed_ratio"], 0.2)
        by_id = {c["cycle_id"]: c for camp in plan["campaigns"] for c in camp["cycles"]}
        self.assertEqual(by_id[c1["cycle_id"]]["slug"], "real-record-title")
        self.assertEqual(by_id[c2["cycle_id"]]["slug"], "attempt-slug-p2")
        self.assertEqual(by_id[c3["cycle_id"]]["slug"], "plans")
        self.assertEqual(by_id[c4["cycle_id"]]["slug"], "real-campaign-goal")
        self.assertEqual(by_id[c5["cycle_id"]]["slug"], "unnamed")

        result = self.apply()
        report = result["report"]
        self.assertEqual(report["cycle_priority_histogram"], {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1})
        journal = json.loads((Path(result["run_dir"]) / RL.JOURNAL_NAME).read_text())
        self.assertEqual(journal["phase"], "complete")
        plan_on_disk = json.loads((Path(result["run_dir"]) / RL.PLAN_NAME).read_text())
        sources = {c["cycle_id"]: c["slug_source"] for camp in plan_on_disk["campaigns"] for c in camp["cycles"]}
        self.assertEqual(sources[c5["cycle_id"]], "unnamed")
        self.assertEqual(sources[c4["cycle_id"]], "campaign-goal")
        for cid, source in (("c1", "record"), ("c2", "attempt-slug"), ("c3", "artifacts-top-dir"),
                            ("c4", "campaign-goal"), ("c5", "unnamed")):
            record = P.read_cycle_record(self.root, locals()[cid]["cycle_id"])
            self.assertEqual(record["slug_source"], f"relayout:{source}", cid)
        rec5 = P.read_cycle_record(self.root, c5["cycle_id"])
        self.assertEqual(rec5["title"], "unnamed")
        self.assertEqual(rec5["legacy_title"], AUTO_CYCLE_TITLE)
        self.assertEqual(rec5["locator"], f"{rec5['started_on'][:10]}_unnamed")
        # Record title (priority 1) stays verbatim; the slug came from it.
        rec1 = P.read_cycle_record(self.root, c1["cycle_id"])
        self.assertEqual(rec1["title"], "Real record title")

    def test_resplit_titles_use_work_date_and_drop_the_duplicate(self):
        _r, c = self.cycle(slug="resplit", title="2026-08-24_dispatch-incidents-remediation")
        self.legacyize(keep_titles=True)
        record = P.read_cycle_record(self.root, c["cycle_id"])
        record["resplit_started_on"] = "2026-08-24"
        P._write_cycle_record(self.root, record, exclusive=False)
        self.apply()
        record = P.read_cycle_record(self.root, c["cycle_id"])
        self.assertEqual(record["locator"], "2026-08-24_dispatch-incidents-remediation")
        self.assertTrue(record["slug_date_stripped"])
        self.assertEqual(record["title"], "2026-08-24_dispatch-incidents-remediation")

    def test_auto_titles_and_keys_are_treated_as_absent(self):
        self.assertTrue(RL.is_auto_title("autopilot-code campaign"))
        self.assertTrue(RL.is_auto_title("autopilot-spec standard cycle"))
        self.assertTrue(RL.is_auto_title("analyze-project cycle output"))
        self.assertTrue(RL.is_auto_title(""))
        self.assertFalse(RL.is_auto_title("W7 relocation campaign"))
        self.assertIsNone(RL._key_name("autopilot-code:rt-0123456789abcdef"))
        self.assertIsNone(RL._key_name("adopted:camp_" + "0" * 32))
        self.assertEqual(RL._key_name("legacy:hearting:plans/2026-08-24_x"), "plans/2026-08-24_x")
        self.assertEqual(RL._key_name("w7c-delta-migration"), "w7c-delta-migration")


class RollbackTests(RelayoutFixture):
    """A-17.8: crash before the commit point rolls back; after it rolls forward."""

    def _two_campaigns(self):
        self.cycle(slug="first", title="First", campaign_key="a", files=("plans/a/1.md", "plans/a/2.md"))
        self.cycle(slug="second", title="Second", campaign_key="b", files=("plans/b/1.md",))
        self.legacyize(keep_titles=True)

    def test_crash_during_rename_rolls_back_to_pre_state(self):
        self._two_campaigns()
        before = _snapshot(self.root)
        with self.assertRaises(RL.RelayoutError):
            self.apply(crash_at="rename:after-first-cycle")
        hold = RL.relayout_hold(self.root)
        self.assertIsNotNone(hold)
        self.assertEqual(hold["code"], "relayout-in-progress")
        self.assertEqual(hold["phase"], "renaming")
        self.assertEqual(RD.migration_hold(self.root)["code"], "relayout-in-progress")
        self.assertEqual(RL.status(self.root)["readable_layout"], "in-progress")
        # The disk is genuinely mid-flight: one cycle moved, records already updated.
        self.assertNotEqual(_snapshot(self.root), before)
        resumed = self.apply()
        self.assertEqual(resumed["status"], "rolled-back")
        self.assertEqual(_snapshot(self.root), before)
        self.assertIsNone(RL.relayout_hold(self.root))
        self.assertEqual(RL.status(self.root)["transition_window"], "open")
        camp_dirs, containers = self.legacy_dirs()
        self.assertEqual(len(camp_dirs), 2)
        self.assertEqual(len(containers), 2)
        # A fresh run then completes normally.
        done = self.apply()
        self.assertEqual(done["status"], "complete")
        self.assertEqual(RL.status(self.root)["readable_layout"], "readable")

    def test_crash_after_records_rolls_back_records_and_bindings(self):
        self._two_campaigns()
        before = _snapshot(self.root)
        with self.assertRaises(RL.RelayoutError):
            self.apply(crash_after_phase="records-written")
        self.assertNotEqual(_snapshot(self.root), before)
        self.assertEqual(RL.rollback(self.root)["status"], "rolled-back")
        self.assertEqual(_snapshot(self.root), before)

    def test_crash_after_commit_point_rolls_forward(self):
        self._two_campaigns()
        for phase in ("renamed", "witnessed", "compat-reissued", "indexed"):
            with self.subTest(phase=phase):
                self.setUp()
                self._two_campaigns()
                clean_root = Path(self._tmp.name) / "clean"
                shutil.copytree(self.root, clean_root, symlinks=True)
                clean = RL.apply(clean_root, jobs_path=self.jobs)
                expected = _content_map(clean_root)
                with self.assertRaises(RL.RelayoutError):
                    self.apply(crash_after_phase=phase)
                hold = RL.relayout_hold(self.root)
                self.assertEqual(hold["phase"], phase)
                with self.assertRaises(RL.RelayoutError) as ctx:
                    RL.rollback(self.root)
                self.assertEqual(ctx.exception.code, "relayout-past-commit-point")
                resumed = self.apply()
                self.assertEqual(resumed["status"], "complete", resumed)
                self.assertEqual(resumed["resumed_from"], phase)
                self.assertEqual({k: (v["files"], v["manifest"], v["dir"]) for k, v in _content_map(self.root).items()},
                                 {k: (v["files"], v["manifest"], v["dir"]) for k, v in expected.items()})
                self.assertIsNone(RL.relayout_hold(self.root))
                self.assertEqual(len(C.load_map_state(self.root)["maps"]), 1)
                self.assertEqual(RL.status(self.root)["transition_window"], "closed")
                self.assertEqual(clean["status"], "complete")

    def test_second_apply_while_open_run_holds_is_resume_not_new_run(self):
        self._two_campaigns()
        with self.assertRaises(RL.RelayoutError):
            self.apply(crash_after_phase="witnessed")
        self.assertEqual(len(RL.run_dirs(self.root)), 1)
        self.assertEqual(self.apply(dry_run=True)["status"], "hold")
        self.assertEqual(self.apply()["status"], "complete")
        self.assertEqual(len(RL.run_dirs(self.root)), 1)


class GateAndCliTests(RelayoutFixture):
    def test_gate_requires_readable_layout_when_asked(self):
        self.cycle(slug="gate", title="Gate")
        self.legacyize(keep_titles=True)
        row = {"repo_path": "/x", "state": "active", "probe": {"passed": True},
               "lumped_cycles_remaining": 0, "legacy_top_level_retired": True, **G._relayout_fields(self.root)}
        verdict, blocking = G.evaluate([row], waived=False, require_resplit=True, require_relayout=True)
        self.assertEqual(verdict, "incomplete")
        self.assertEqual(blocking[0]["reason"], "relayout-incomplete")
        verdict, _ = G.evaluate([row], waived=False, require_resplit=True)
        self.assertEqual(verdict, "complete")
        self.apply()
        row.update(G._relayout_fields(self.root))
        verdict, blocking = G.evaluate([row], waived=False, require_resplit=True, require_relayout=True)
        self.assertEqual(verdict, "complete", blocking)
        self.assertEqual(row["readable_layout"], "readable")
        self.assertEqual(row["legacy_cycle_dirs"], 0)

    def test_cli_status_plan_apply_and_hold(self):
        import io
        from contextlib import redirect_stdout
        self.cycle(slug="cli", title="Cli")
        self.legacyize(keep_titles=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = RL.main(["--artifact-root", str(self.root), "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["readable_layout"], "legacy")
        out = io.StringIO()
        with redirect_stdout(out):
            code = RL.main(["--artifact-root", str(self.root), "plan", "--jobs", str(self.jobs)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["totals"]["cycles"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            code = RL.main(["--artifact-root", str(self.root), "apply", "--jobs", str(self.jobs), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "dry-run")
        out = io.StringIO()
        with redirect_stdout(out):
            code = RL.main(["--artifact-root", str(self.root), "apply", "--jobs", str(self.jobs), "--content-digests"])
        self.assertEqual(code, 0, out.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "complete")
        self.assertTrue(payload["report"]["witness"]["content_digests"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = RL.main(["--artifact-root", str(self.root), "hold"])
        self.assertEqual(code, 0)
        out = io.StringIO()
        with redirect_stdout(out):
            code = RD.main(["hold", "--artifact-root", str(self.root)])
        self.assertEqual(code, 0)

    def test_reader_hold_cli_reports_relayout_hold(self):
        import io
        from contextlib import redirect_stdout
        self.cycle(slug="hold", title="Hold")
        self.legacyize(keep_titles=True)
        with self.assertRaises(RL.RelayoutError):
            self.apply(crash_after_phase="renamed")
        out = io.StringIO()
        with redirect_stdout(out):
            code = RD.main(["hold", "--artifact-root", str(self.root)])
        self.assertEqual(code, RD.HOLD_EXIT)
        self.assertEqual(json.loads(out.getvalue())["hold"]["code"], "relayout-in-progress")


if __name__ == "__main__":
    unittest.main()
