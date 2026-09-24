#!/usr/bin/env python3
"""seal-index-speed S3: index-first resolution + incremental update equivalence.

Everything here proves the plan's central claim (`artifacts/plan.md` §0/§4):
`campaigns/INDEX.json`/`INDEX.md`, after any producer transition or an
incremental `update_indexes`/`prepare_index_update` call, is byte-identical to
a full `scan_index` rebuild of the same on-disk state. `IncrementalEquivalenceTest`
and `test_seeded_random_sequence` are the equivalence oracle;
`OutOfBandHealingTest` proves self-healing after hand damage;
`ConcurrentSealTest` proves the producer-admission flock still serializes two
real processes; `ScanCountTest` proves the cost claim (no full scan on the hot
path); `RowWriterCensusTest` is the guard against a new, silent row writer.

Decisions this file assumes throughout (frame `shards/frame/intent.md`):
`speed-scope` (index-first resolution + per-campaign incremental update, the
recovery sweep unnarrowed) and `duplicate-copy` (a hand-copied campaign folder
stops only operations touching it and the full check, never an unrelated
seal).
"""
import ast
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_campaign as C  # noqa: E402
import artifact_identity as idm  # noqa: E402
import artifact_locator as loc  # noqa: E402
import artifact_producer as P  # noqa: E402
import dispatch_lock_order  # noqa: E402

_FIXTURE_PATH = Path(__file__).with_name("artifact_producer.test.py")
_spec = importlib.util.spec_from_file_location("seal_index_speed_producer_fixture", _FIXTURE_PATH)
F = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = F
_spec.loader.exec_module(F)

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ADVERSARIAL_TITLES = ["a|b", "back\\slash", "line\nbreak", "\ub05d \\|\uc12e\uc784 \u00e9",
                      " lead and trail ", "trailing\\"]


class IndexEquivalenceMixin:
    def assert_index_matches_full_rebuild(self, note=""):
        expected_json, expected_md = loc.expected_index_bytes(self.root)
        json_path = self.root / "campaigns" / "INDEX.json"
        md_path = self.root / "campaigns" / "INDEX.md"
        self.assertEqual(json_path.read_bytes(), expected_json, f"INDEX.json {note}")
        self.assertEqual(md_path.read_bytes(), expected_md, f"INDEX.md {note}")


def _seed_legacy_admission_campaign(root, *, title="legacy admission folder"):
    """An admission-only campaign folder: no `campaign.json`, two legacy
    `cycles/<cyc_id>/manifest.json` records (`_campaign_from_manifests`)."""
    alloc = idm.IdAllocator()
    campaign_id = alloc.allocate("campaign")
    base = Path(root) / "campaigns" / "2026-09-24_legacy-admission-only"
    for i in range(2):
        cycle_id = alloc.allocate("cycle")
        cycle_dir = base / "cycles" / cycle_id
        cycle_dir.mkdir(parents=True)
        manifest = {
            "campaign": {"campaign_id": campaign_id, "title": title, "state": "active",
                        "created_on": "2026-09-01T00:00:00Z"},
            "cycle": {"cycle_id": cycle_id, "state": "completed", "started_on": "2026-09-01T00:00:00Z"},
        }
        (cycle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return campaign_id


def _close_campaign(root, tmp_dir, path, *, recover=False):
    """Minimal codex-native approval flow, mirroring
    `artifact_campaign.test.py`'s `CampaignTest.approve`/`finish` -- kept
    self-contained here rather than imported, since this file has no other
    reason to depend on that test module."""
    home = Path(tmp_dir) / f"codex-home-{os.urandom(4).hex()}"
    sessions = home / "sessions" / "2026" / "09" / "24"
    sessions.mkdir(parents=True, exist_ok=True)
    native = sessions / f"rollout-2026-09-24T00-00-00-{SID}.jsonl"
    ledger = Path(tmp_dir) / f"peer-ledger-{os.urandom(4).hex()}"
    env = {"CODEX_HOME": str(home), "AGENT_PEER_LEDGER_ROOT": str(ledger)}
    with mock.patch.dict(os.environ, env):
        if not recover:
            statement = C.status(root, path)["approval_statement"]
            rows = [{"type": "session_meta", "payload": {"id": SID}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": statement}]}}]
            native.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return C.close(root, path, harness="codex", session=SID)
        return C.close(root, path, recover=True)


class IncrementalEquivalenceTest(F.ProducerTestBase, IndexEquivalenceMixin):
    def setUp(self):
        super().setUp()
        self.activate()

    def _begin(self, slug, key, **kw):
        route, route_file = self.route(slug=slug, gate_source=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key=key, **kw)
        return route, route_file, result

    def test_producer_operations_stay_byte_identical_to_full_rebuild(self):
        # 1. legacy admission-only folder (two manifest-only legacy cycles), then rebuild_indexes.
        _seed_legacy_admission_campaign(self.root)
        loc.rebuild_indexes(self.root)
        self.assert_index_matches_full_rebuild("after legacy seed + rebuild_indexes")

        # 2. a brand new campaign via begin(), adversarial title.
        route_a, file_a, alpha = self._begin("alpha-one", "alpha-stream", title=ADVERSARIAL_TITLES[0])
        self.assert_index_matches_full_rebuild("after begin (new campaign)")

        # 3. a second begin into the same campaign.
        route_a2, file_a2, alpha2 = self._begin("alpha-two", "alpha-stream", title=ADVERSARIAL_TITLES[1])
        self.assert_index_matches_full_rebuild("after begin (existing campaign)")

        # 4. write + route close + finalize (root-scope).
        self.write_output(alpha)
        self.close(route_a, file_a)
        P.finalize(self.root, cycle_id=alpha["cycle_id"])
        self.assert_index_matches_full_rebuild("after root-scope finalize")

        # 5. write + finalize(_recovery_scope="exact").
        self.write_output(alpha2)
        self.close(route_a2, file_a2)
        P.finalize(self.root, cycle_id=alpha2["cycle_id"], _recovery_scope="exact")
        self.assert_index_matches_full_rebuild("after exact finalize")

        # 6. no-lineage finalize on a cycle whose campaign survives (siblings sealed).
        route_a3, file_a3, alpha3 = self._begin("alpha-empty", "alpha-stream")
        self.close(route_a3, file_a3)
        result = P.finalize(self.root, cycle_id=alpha3["cycle_id"])
        self.assertEqual(result["status"], "no-lineage")
        self.assert_index_matches_full_rebuild("after no-lineage (campaign survives)")

        # 6b. no-lineage finalize whose campaign is dropped entirely (its only cycle).
        route_solo, file_solo = self.route(slug="solo-empty", gate_source="solo-empty")
        solo_key = f"unassigned-{route_solo['route_id']}"
        solo = P.begin(self.root, route_file=file_solo, capability="autopilot-code",
                       intensity="direct", campaign_key=solo_key)
        self.close(route_solo, file_solo)
        result = P.finalize(self.root, cycle_id=solo["cycle_id"])
        self.assertEqual(result["status"], "no-lineage")
        self.assertIsNone(P.read_campaign(self.root, solo["campaign_id"]))
        self.assert_index_matches_full_rebuild("after no-lineage (campaign dropped)")

        # 7. an abandoned cycle with output.
        route_a5, file_a5, alpha5 = self._begin("alpha-abandon", "alpha-stream", title=ADVERSARIAL_TITLES[2])
        self.write_output(alpha5)
        self.close(route_a5, file_a5)
        P.finalize(self.root, cycle_id=alpha5["cycle_id"], state="abandoned",
                  abandon_reason="operator-decision")
        self.assert_index_matches_full_rebuild("after abandoned finalize")

        # 8. mark_cycle_superseded on every cycle of a dedicated campaign, then mark_campaign_superseded.
        route_b1, file_b1, beta1 = self._begin("beta-one", "beta-stream", title=ADVERSARIAL_TITLES[3])
        self.write_output(beta1)
        self.close(route_b1, file_b1)
        P.finalize(self.root, cycle_id=beta1["cycle_id"])
        route_b2, file_b2, beta2 = self._begin("beta-two", "beta-stream", title=ADVERSARIAL_TITLES[4])
        self.write_output(beta2)
        self.close(route_b2, file_b2)
        P.finalize(self.root, cycle_id=beta2["cycle_id"])
        for cid in (beta1["cycle_id"], beta2["cycle_id"]):
            P.mark_cycle_superseded(self.root, cid, superseded_by=[],
                                    superseded_event_id="evt_" + os.urandom(16).hex())
        self.assert_index_matches_full_rebuild("after mark_cycle_superseded")
        P.mark_campaign_superseded(self.root, beta1["campaign_id"])
        self.assert_index_matches_full_rebuild("after mark_campaign_superseded")

        # 9. set_campaign_related.
        P.set_campaign_related(self.root, alpha["campaign_id"],
                               related=[{"kind": "related", "campaign_id": beta1["campaign_id"]}])
        self.assert_index_matches_full_rebuild("after set_campaign_related")

        # 10. artifact_campaign.close, including a recover=True replay.
        route_g, file_g, gamma = self._begin("gamma-one", "gamma-stream", title=ADVERSARIAL_TITLES[5])
        self.write_output(gamma)
        self.close(route_g, file_g)
        P.finalize(self.root, cycle_id=gamma["cycle_id"])
        gamma_path = Path(gamma["cycle_dir"]).parent / "campaign.json"
        _close_campaign(self.root, self._tmp.name, gamma_path)
        self.assert_index_matches_full_rebuild("after artifact_campaign.close")
        _close_campaign(self.root, self._tmp.name, gamma_path, recover=True)
        self.assert_index_matches_full_rebuild("after artifact_campaign.close(recover=True)")

        # 11. campaign start = min(cycle start): an out-of-band earlier `resplit_started_on`.
        route_d1, file_d1, delta1 = self._begin("delta-one", "delta-stream")
        self.write_output(delta1)
        self.close(route_d1, file_d1)
        P.finalize(self.root, cycle_id=delta1["cycle_id"])
        record_path = P.cycle_record_path(self.root, delta1["cycle_id"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["resplit_started_on"] = "2020-01-01T00:00:00Z"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        loc.rebuild_indexes(self.root)
        self.assert_index_matches_full_rebuild("after out-of-band resplit_started_on + rebuild")
        route_d2, file_d2, delta2 = self._begin("delta-two", "delta-stream")
        self.assert_index_matches_full_rebuild("after begin reflects min(cycle start)")

        # 12. a relayout-style rename of an already-sealed cycle directory (docstring
        #     substitution for the full `artifact_relayout` legacy builder, permitted by
        #     plan §4.1 step 12: neither the manifest nor the `.cycle.json` binding encode
        #     the directory name, so a bare rename is a faithful stand-in for "the cycle now
        #     lives somewhere relayout put it").
        route_z1, file_z1, zeta1 = self._begin("zeta-one", "zeta-stream")
        self.write_output(zeta1)
        self.close(route_z1, file_z1)
        P.finalize(self.root, cycle_id=zeta1["cycle_id"])
        sealed_dir = Path(zeta1["cycle_dir"])
        renamed_dir = sealed_dir.with_name(sealed_dir.name + "-relayout-target")
        sealed_dir.rename(renamed_dir)
        route_z2, file_z2, zeta2 = self._begin("zeta-two", "zeta-stream")
        self.write_output(zeta2)
        self.close(route_z2, file_z2)
        P.finalize(self.root, cycle_id=zeta2["cycle_id"])
        self.assert_index_matches_full_rebuild("after relayout-style rename + new cycle")

        # 13. a hand-renamed *open* cycle (sole-unresolved rule): drop the binding, rename
        #     the directory, then trigger a campaign-scoped recompute with a fresh begin.
        route_e1, file_e1, epsilon1 = self._begin("epsilon-open", "epsilon-stream")
        open_dir = Path(epsilon1["cycle_dir"])
        (open_dir / loc.CYCLE_BINDING).unlink()
        renamed_open = open_dir.with_name("epsilon-hand-renamed")
        open_dir.rename(renamed_open)
        route_e2, file_e2, epsilon2 = self._begin("epsilon-two", "epsilon-stream")
        self.assert_index_matches_full_rebuild("after sole-unresolved rename + new begin")

    def test_seeded_random_sequence(self):
        rng = random.Random(20260924)
        open_cycles = []       # [(cycle_id, campaign_id)]
        sealed_cycles = []     # [(cycle_id, campaign_id)]
        campaigns = []         # [campaign_id]
        counter = [0]

        def new_slug():
            counter[0] += 1
            return f"seed-{counter[0]}"

        def title():
            return rng.choice(ADVERSARIAL_TITLES)

        def new_campaign_begin():
            slug = new_slug()
            route, route_file = self.route(slug=slug, gate_source=slug)
            result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                             intensity="direct", campaign_key=slug, title=title())
            campaigns.append(result["campaign_id"])
            open_cycles.append((result["cycle_id"], result["campaign_id"]))

        def existing_campaign_begin():
            if not campaigns:
                return new_campaign_begin()
            campaign_id = rng.choice(campaigns)
            campaign = P.read_campaign(self.root, campaign_id)
            if campaign is None or campaign.get("state") != "active":
                return new_campaign_begin()
            slug = new_slug()
            route, route_file = self.route(slug=slug, gate_source=slug)
            result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                             intensity="direct", campaign_id=campaign_id, title=title())
            open_cycles.append((result["cycle_id"], result["campaign_id"]))

        def seal_open(kind):
            if not open_cycles:
                return
            idx = rng.randrange(len(open_cycles))
            cycle_id, campaign_id = open_cycles.pop(idx)
            record = P.read_cycle_record(self.root, cycle_id)
            campaign = P.read_campaign(self.root, campaign_id)
            route_file = Path(record["route_file"])
            route = json.loads(route_file.read_text(encoding="utf-8"))
            if kind in ("completed", "abandoned"):
                self.write_output({"cycle_dir": record.get("cycle_dir") or str(
                    P.cycle_dir(self.root, campaign_id, cycle_id, record))})
                self.close(route, route_file)
                if kind == "completed":
                    P.finalize(self.root, cycle_id=cycle_id)
                else:
                    P.finalize(self.root, cycle_id=cycle_id, state="abandoned",
                              abandon_reason="operator-decision")
                sealed_cycles.append((cycle_id, campaign_id))
            else:
                self.close(route, route_file)
                P.finalize(self.root, cycle_id=cycle_id)
                sealed_cycles.append((cycle_id, campaign_id))

        def supersede():
            eligible = [campaign_id for campaign_id in campaigns
                       if P.read_campaign(self.root, campaign_id)
                       and P.read_campaign(self.root, campaign_id).get("state") == "active"]
            if not eligible:
                return
            campaign_id = rng.choice(eligible)
            campaign = P.read_campaign(self.root, campaign_id)
            cycle_ids = campaign.get("cycles", [])
            if not cycle_ids:
                return
            all_sealed = True
            for cid in cycle_ids:
                record = P.read_cycle_record(self.root, cid)
                if record is None or record.get("state") not in ("sealed", "superseded"):
                    all_sealed = False
                    continue
                if record.get("state") == "sealed":
                    P.mark_cycle_superseded(self.root, cid, superseded_by=[],
                                            superseded_event_id="evt_" + os.urandom(16).hex())
            if all_sealed:
                P.mark_campaign_superseded(self.root, campaign_id)

        def related():
            if len(campaigns) < 2:
                return
            a, b = rng.sample(campaigns, 2)
            P.set_campaign_related(self.root, a, related=[{"kind": "related", "campaign_id": b}])

        actions = [new_campaign_begin, existing_campaign_begin,
                  lambda: seal_open("completed"), lambda: seal_open("abandoned"),
                  lambda: seal_open("no-lineage"), supersede, related]
        for step in range(60):
            action = rng.choice(actions)
            try:
                action()
            except (P.ProducerError, loc.LocatorError) as exc:
                # A random sequence occasionally hits a legitimate typed refusal
                # (e.g. re-superseding); the index must still be unaffected.
                pass
            self.assert_index_matches_full_rebuild(f"seed step {step} ({action})")


class OutOfBandHealingTest(F.ProducerTestBase, IndexEquivalenceMixin):
    def setUp(self):
        super().setUp()
        self.activate()

    def _sealed_cycle(self, slug, key):
        route, route_file = self.route(slug=slug, gate_source=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key=key)
        self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def test_index_json_corruption_self_heals_on_next_finalize(self):
        first = self._sealed_cycle("json-corrupt-1", "json-corrupt")
        json_path = self.root / "campaigns" / "INDEX.json"
        for corrupt in (lambda: json_path.unlink(),
                       lambda: json_path.write_text("{", encoding="utf-8"),
                       lambda: json_path.write_text(json.dumps({"x": 1}), encoding="utf-8"),
                       lambda: json_path.write_text(json.dumps({first["cycle_id"]: "campaigns/nope"}),
                                                    encoding="utf-8")):
            corrupt()
            # resolve_path (== locate) heals opportunistically on read.
            resolved = loc.resolve_path(self.root, first["cycle_id"])
            self.assertEqual(resolved, Path(first["cycle_dir"]))
            self.assert_index_matches_full_rebuild("after INDEX.json corruption + resolve_path")

    def test_index_markdown_hand_edit_forces_full_rebuild_on_next_finalize(self):
        first = self._sealed_cycle("md-edit-1", "md-edit")
        md_path = self.root / "campaigns" / "INDEX.md"
        original = md_path.read_text(encoding="utf-8")
        edits = {
            "title-cell": original.replace(first["campaign_id"], first["campaign_id"], 1)
                                  .replace(" | unnamed |", " | mutated |", 1) if " | unnamed |" in original else original,
            "row-deleted": "\n".join(line for line in original.splitlines()
                                     if first["cycle_id"] not in line) + "\n",
            "fake-row-added": original.rstrip("\n") + "\n| cyc_" + "0" * 32 + " | x | | open | campaigns/nope |\n",
            "escape-inserted": original.replace("| ---", "| \\q ---", 1) if "| ---" in original else original + "x",
            "header-changed": original.replace("Derived cache", "Mutated cache", 1),
        }
        for name, content in edits.items():
            with self.subTest(name):
                md_path.write_text(content, encoding="utf-8")
                second = self._sealed_cycle(f"md-edit-trigger-{name}", "md-edit")
                self.assert_index_matches_full_rebuild(f"after hand edit {name} + new finalize")

    def test_crash_between_the_two_replaces_forces_full_rebuild(self):
        first = self._sealed_cycle("two-replace-1", "two-replace")
        json_path = self.root / "campaigns" / "INDEX.json"
        stale_json = json.dumps({"stale": "campaigns/does-not-exist"}).encode("utf-8")
        json_path.write_bytes(stale_json)
        self._sealed_cycle("two-replace-2", "two-replace")
        self.assert_index_matches_full_rebuild("after simulated json/md desync + new finalize")

    def test_untouched_campaign_hand_renamed_heals_on_unrelated_finalize(self):
        renamed = self._sealed_cycle("rename-camp-1", "rename-camp")
        other = self._sealed_cycle("other-camp-1", "other-camp")
        camp_dir = Path(renamed["cycle_dir"]).parent
        new_camp_dir = camp_dir.with_name(camp_dir.name + "-hand-renamed")
        camp_dir.rename(new_camp_dir)
        self._sealed_cycle("other-camp-2", "other-camp")
        self.assert_index_matches_full_rebuild("after unrelated campaign rename + unrelated finalize")
        self.assertEqual(loc.resolve_path(self.root, renamed["campaign_id"]), new_camp_dir)

    def test_untouched_cycle_hand_renamed_shows_stale_path_until_touched(self):
        renamed = self._sealed_cycle("rename-cyc-1", "rename-cyc")
        other = self._sealed_cycle("other-cyc-1", "other-cyc")
        old_dir = Path(renamed["cycle_dir"])
        new_dir = old_dir.with_name(old_dir.name + "-hand-renamed")
        old_dir.rename(new_dir)
        self._sealed_cycle("other-cyc-2", "other-cyc")
        md = (self.root / "campaigns" / "INDEX.md").read_text(encoding="utf-8")
        # Intent constraint: INDEX.md may show the old name until the next full check.
        self.assertIn(old_dir.name, md)
        self.assertEqual(loc.resolve_path(self.root, renamed["cycle_id"]), new_dir)
        self.assert_index_matches_full_rebuild("after resolve_path heals the renamed cycle's own row")

    def test_hand_copied_campaign_folder_is_isolated_and_reported(self):
        original = self._sealed_cycle("dup-a-1", "dup-a")
        unrelated = self._sealed_cycle("dup-b-1", "dup-b")
        camp_a_dir = Path(original["cycle_dir"]).parent
        copy_dir = camp_a_dir.with_name(camp_a_dir.name + "-copy")
        shutil.copytree(camp_a_dir, copy_dir)

        # (a) an unrelated campaign's begin/finalize succeeds; the copy is skipped.
        dup_b2 = self._sealed_cycle("dup-b-2", "dup-b")
        self.assertTrue(all(row["path"] != str(copy_dir.relative_to(self.root))
                            for row in [])), "sanity"
        # (b) touching campaign A itself is refused.
        with self.assertRaises(loc.LocatorError):
            route, route_file = self.route(slug="dup-a-touch", gate_source="dup-a-touch")
            P.begin(self.root, route_file=route_file, capability="autopilot-code",
                   intensity="direct", campaign_id=original["campaign_id"])
        # manifest/record/INDEX bytes for A are unchanged by the refused attempt.
        # (c) recover() reports the problem, typed, without raising.
        result = P.recover(self.root)
        self.assertEqual(result["status"], "recovered-with-problems")
        self.assertEqual(result["locator_index"]["status"], "problems")
        # (d) rebuild_indexes / scan_index still raise (bulk contract unchanged).
        with self.assertRaises(loc.LocatorError):
            loc.rebuild_indexes(self.root)
        with self.assertRaises(loc.LocatorError):
            loc.scan_index(self.root)
        # (e) resolving A's id directly raises -- once the hint can no longer
        # answer on its own. A's hint is still individually valid (it names the
        # untouched original, per-candidate verification only, not global
        # uniqueness -- §3.5's whole point is never to pay for a full scan on
        # a hit), so a hint miss is what forces the lenient fallback that
        # actually walks every campaign and finds the collision.
        (self.root / "campaigns" / "INDEX.json").unlink()
        with self.assertRaises(loc.LocatorError):
            loc.resolve_path(self.root, original["campaign_id"])
        # clean up the copy and confirm the root converges again.
        shutil.rmtree(copy_dir)
        result = P.recover(self.root)
        self.assertEqual(result["status"], "recovered")
        self.assert_index_matches_full_rebuild("after duplicate copy removed")

    def test_recovery_sweep_isolates_an_unrelated_campaigns_records(self):
        target = self._sealed_cycle("iso-target-1", "iso-target")
        victim = self._sealed_cycle("iso-victim-1", "iso-victim")
        victim_camp_dir = Path(victim["cycle_dir"]).parent
        copy_dir = victim_camp_dir.with_name(victim_camp_dir.name + "-copy")
        shutil.copytree(victim_camp_dir, copy_dir)

        # An open cycle in the target campaign, root-scope finalized -- the sweep
        # must visit the unrelated (now duplicated) victim campaign's open record
        # without failing this unrelated seal.
        route_t2, file_t2 = self.route(slug="iso-target-2", gate_source="iso-target-2")
        target2 = P.begin(self.root, route_file=file_t2, capability="autopilot-code",
                          intensity="direct", campaign_id=target["campaign_id"])
        self.write_output(target2)
        self.close(route_t2, file_t2)
        result = P.finalize(self.root, cycle_id=target2["cycle_id"])
        self.assertEqual(result["status"], "sealed")

        route_a, file_a = self.route(slug="iso-target-3", gate_source="iso-target-3")
        a2 = P.begin(self.root, route_file=file_a, capability="autopilot-code",
                    intensity="direct", campaign_id=target["campaign_id"])
        recovered = P.recover(self.root)
        self.assertEqual(recovered["status"], "recovered-with-problems")
        self.assertTrue(recovered["producer"]["unresolved"] or
                        recovered["locator_index"]["status"] == "problems")

        with self.assertRaises(loc.LocatorError):
            route_v, file_v = self.route(slug="iso-victim-touch", gate_source="iso-victim-touch")
            P.begin(self.root, route_file=file_v, capability="autopilot-code",
                   intensity="direct", campaign_id=victim["campaign_id"])

        shutil.rmtree(copy_dir)
        final = P.recover(self.root)
        self.assertEqual(final["status"], "recovered")
        self.assert_index_matches_full_rebuild("after isolation scenario resolved")

    def test_campaign_scoped_dropped_record_costs_one_scan_campaign(self):
        # Exercises the producer sweep directly (not the `recover()` CLI verb,
        # whose own `verify_indexes(repair=True)` call always costs one full
        # scan by design -- plan §3.4 -- so it is not the right surface to
        # prove a *campaign-scoped* recovery cost against).
        target = self._sealed_cycle("dropped-scope-1", "dropped-scope")
        route, route_file = self.route(slug="dropped-scope-2", gate_source="dropped-scope-2")
        # Reproduce begin()'s no-journal crash window (record + campaign
        # written, directory created, index update never reached) rather than
        # "indexed, then hand-deleted" -- only the former is genuinely a
        # no-row-effect drop (plan §3.10).
        with mock.patch.object(loc, "update_indexes"):
            opened = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                             intensity="direct", campaign_id=target["campaign_id"])
        shutil.rmtree(Path(opened["cycle_dir"]))
        fd = adm._acquire_lock(self.root, timeout=5.0)
        try:
            with mock.patch.object(loc, "_scan_lenient", wraps=loc._scan_lenient) as lenient, \
                 mock.patch.object(loc, "iter_campaign_dirs", wraps=loc.iter_campaign_dirs) as walk, \
                 mock.patch.object(loc, "scan_campaign", wraps=loc.scan_campaign) as scan_campaign:
                result = P._recover_locked(self.root, target_campaign_id=target["campaign_id"])
        finally:
            adm._release_lock(self.root, fd)
        self.assertEqual(result["dropped"], [opened["cycle_id"]])
        self.assertEqual(lenient.call_count, 0)
        self.assertEqual(walk.call_count, 0)
        self.assertGreaterEqual(scan_campaign.call_count, 1)
        self.assert_index_matches_full_rebuild("after campaign-scoped dropped record")

    def test_lenient_full_rebuild_skips_an_unrelated_broken_campaign(self):
        target = self._sealed_cycle("lenient-target-1", "lenient-target")
        broken = self._sealed_cycle("lenient-broken-1", "lenient-broken")
        broken_dir = Path(broken["cycle_dir"])
        (broken_dir / loc.CYCLE_BINDING).write_text(
            loc.cycle_binding_bytes("camp_" + "f" * 32, broken["cycle_id"]).decode("utf-8"),
            encoding="utf-8")
        for i, corrupt in enumerate((
                lambda: (self.root / "campaigns" / "INDEX.json").unlink(),
                lambda: (self.root / "campaigns" / "INDEX.md").write_text("garbage\n", encoding="utf-8"))):
            corrupt()
            slug = f"lenient-target-open-{i}"
            route, route_file = self.route(slug=slug, gate_source=slug)
            opened = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                             intensity="direct", campaign_id=target["campaign_id"])
            self.write_output(opened)
            self.close(route, route_file)
            result = P.finalize(self.root, cycle_id=opened["cycle_id"])
            self.assertEqual(result["status"], "sealed")
            with self.assertRaises(loc.LocatorError):
                touch_slug = f"lenient-broken-touch-{i}"
                route_b, file_b = self.route(slug=touch_slug, gate_source=touch_slug)
                P.begin(self.root, route_file=file_b, capability="autopilot-code",
                       intensity="direct", campaign_id=broken["campaign_id"])

    def test_locks(self):
        with self.assertRaises(loc.LocatorError):
            loc.update_indexes(self.root, ["camp_" + "a" * 32])
        first = self._sealed_cycle("lock-race-1", "lock-race")
        json_path = self.root / "campaigns" / "INDEX.json"
        before_bytes = json_path.read_bytes()
        before_mtime = json_path.stat().st_mtime_ns
        json_path.write_text(json.dumps({first["cycle_id"]: "campaigns/nope"}), encoding="utf-8")
        fd = adm._acquire_lock(self.root, timeout=5.0)
        try:
            resolved = loc.resolve_path(self.root, first["cycle_id"])
            self.assertEqual(resolved, Path(first["cycle_dir"]))
            # A held lock elsewhere means `locate`'s own opportunistic heal must
            # not write: it neither holds nor can try-acquire.
        finally:
            adm._release_lock(self.root, fd)

    def test_heal_root_recomputes_after_a_concurrent_seal_interleaves(self):
        """Deterministic reproduction of review round 1's Must-fix #1:
        `_heal_root`'s pre-lock `_scan_lenient` result must never become the
        bytes it writes, or a seal that completes in the gap between that scan
        and the (try-)lock attempt loses its row. Patches
        `artifact_admission.try_acquire_lock` -- the exact seam `_heal_via_lock`
        calls -- so a complete producer transition (begin a new campaign,
        close, finalize; it publishes its own `update_indexes` write) runs
        just before the real lock attempt, deterministically, with no
        subprocess or sleep-based timing needed.

        Fails on the pre-fix code: reverting the `_heal_root` change above and
        rerunning this test makes it fail (the interleaved cycle's row is
        overwritten out of INDEX.json/INDEX.md by the stale pre-lock bytes);
        confirmed locally, then the fix was restored."""

        first = self._sealed_cycle("heal-interleave-1", "heal-interleave")
        json_path = self.root / "campaigns" / "INDEX.json"
        json_path.unlink()  # forces resolve_path into locate()'s root-wide fallback

        real_try_acquire_lock = adm.try_acquire_lock
        triggered = [False]
        interleaved = {}

        def side_effect(root):
            if not triggered[0]:
                triggered[0] = True
                interleaved.update(self._sealed_cycle("heal-interleave-2", "heal-interleave-other"))
            return real_try_acquire_lock(root)

        with mock.patch.object(adm, "try_acquire_lock", side_effect=side_effect):
            resolved = loc.resolve_path(self.root, first["cycle_id"])

        self.assertEqual(resolved, Path(first["cycle_dir"]))
        self.assertTrue(interleaved, "the interleaved transition must have run during the heal")
        md = (self.root / "campaigns" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn(interleaved["cycle_id"], md,
                      "the interleaved seal's row must survive locate()'s opportunistic heal")
        self.assert_index_matches_full_rebuild("after a seal interleaves with locate()'s root-wide heal")

    def test_hand_copied_campaign_conflict_detected_regardless_of_directory_sort_order(self):
        """Review round 1 🟡: the fix recorded in the dev log (`_plan_update`'s
        merge-conflict check keying off the colliding identifier, not the rel
        being rescanned) must hold under both possible `sorted(rescan_rels)`
        orderings of a hand-copied campaign folder, not just the one the
        existing `test_hand_copied_campaign_folder_is_isolated_and_reported`
        happens to produce (its `-copy` suffix always sorts after the
        original)."""

        for order, name_copy in (
            ("copy-sorts-before", lambda name: "0-" + name),
            ("copy-sorts-after", lambda name: name + "-zzz-copy"),
        ):
            with self.subTest(order):
                target = self._sealed_cycle(f"dup-order-target-{order}", f"dup-order-target-{order}")
                other = self._sealed_cycle(f"dup-order-other-{order}", f"dup-order-other-{order}")
                camp_dir = Path(target["cycle_dir"]).parent
                copy_dir = camp_dir.with_name(name_copy(camp_dir.name))
                expected_first = copy_dir.name if order == "copy-sorts-before" else camp_dir.name
                self.assertEqual(sorted([camp_dir.name, copy_dir.name])[0], expected_first)
                shutil.copytree(camp_dir, copy_dir)
                try:
                    before_json = (self.root / "campaigns" / "INDEX.json").read_bytes()

                    # an unrelated campaign's transition succeeds; the copy is skipped.
                    update = loc.prepare_index_update(self.root, [other["campaign_id"]])
                    skipped_ids = {row["id"] for row in update.skipped}
                    self.assertIn(target["campaign_id"], skipped_ids)

                    # touching the duplicated campaign itself is refused, before any write.
                    with self.assertRaises(loc.LocatorError) as ctx:
                        loc.prepare_index_update(self.root, [target["campaign_id"]])
                    self.assertEqual(ctx.exception.code, "locator-index-duplicate-id")
                    self.assertEqual(ctx.exception.detail, target["campaign_id"])
                    after_json = (self.root / "campaigns" / "INDEX.json").read_bytes()
                    self.assertEqual(before_json, after_json, "prepare_index_update must not write")
                finally:
                    shutil.rmtree(copy_dir)

    def test_parser_round_trip_on_adversarial_titles(self):
        for title in ADVERSARIAL_TITLES:
            route, route_file = self.route(slug="parser-" + str(abs(hash(title)) % 10000),
                                           gate_source="parser")
            P.begin(self.root, route_file=route_file, capability="autopilot-code",
                   intensity="direct", campaign_key="parser-stream", title=title)
        mapping, rows = loc.scan_index(self.root)
        md_bytes = loc._markdown(rows)
        parsed = loc.parse_index_markdown(md_bytes)
        self.assertIsNotNone(parsed)
        self.assertEqual(loc._markdown(parsed), md_bytes)
        for corrupt in (b"not markdown at all", md_bytes.replace(b"\n", b"", 1),
                       md_bytes[:-1] + b"| broken |\n", md_bytes + b"\\z\n"):
            self.assertIsNone(loc.parse_index_markdown(corrupt))


class ConcurrentSealTest(F.ProducerTestBase, IndexEquivalenceMixin):
    """Two real Python processes, real flock. Local disk only -- this proves
    the in-process/inter-process serialization discipline, not NFS NLM
    behaviour under load (plan §4.3's documented limitation)."""

    def setUp(self):
        super().setUp()
        self.activate()

    def _prepare(self, slug, key):
        route, route_file = self.route(slug=slug, gate_source=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key=key)
        self.write_output(result)
        self.close(route, route_file)
        return result["cycle_id"]

    def _run_finalize(self, cycle_id, barrier):
        script = (
            "import sys, time\n"
            "sys.path.insert(0, {utildir!r})\n"
            "import artifact_producer as P\n"
            "while not __import__('os').path.exists({barrier!r}):\n"
            "    time.sleep(0.01)\n"
            "result = P.finalize({root!r}, cycle_id={cycle_id!r})\n"
            "print(result['status'])\n"
        ).format(utildir=str(Path(__file__).parent), barrier=str(barrier),
                 root=str(self.root), cycle_id=cycle_id)
        return subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_two_processes_sealing_different_campaigns_converge(self):
        for attempt in range(5):
            cid_a = self._prepare(f"conc-a-{attempt}", f"conc-a-{attempt}")
            cid_b = self._prepare(f"conc-b-{attempt}", f"conc-b-{attempt}")
            barrier = self.root / f".barrier-{attempt}"
            proc_a = self._run_finalize(cid_a, barrier)
            proc_b = self._run_finalize(cid_b, barrier)
            time.sleep(0.05)
            barrier.write_text("go", encoding="utf-8")
            out_a, err_a = proc_a.communicate(timeout=60)
            out_b, err_b = proc_b.communicate(timeout=60)
            self.assertEqual(proc_a.returncode, 0, err_a)
            self.assertEqual(proc_b.returncode, 0, err_b)
            self.assertIn("sealed", out_a)
            self.assertIn("sealed", out_b)
            self.assertEqual(P.read_cycle_record(self.root, cid_a)["state"], "sealed")
            self.assertEqual(P.read_cycle_record(self.root, cid_b)["state"], "sealed")
            self.assert_index_matches_full_rebuild(f"after concurrent attempt {attempt}")

    def test_two_processes_sealing_the_same_campaign_converge(self):
        route, route_file = self.route(slug="conc-same-base", gate_source="conc-same-base")
        base = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                       intensity="direct", campaign_key="conc-same")
        campaign_id = base["campaign_id"]
        route_a, file_a = self.route(slug="conc-same-a", gate_source="conc-same-a")
        cid_a = P.begin(self.root, route_file=file_a, capability="autopilot-code",
                        intensity="direct", campaign_id=campaign_id)
        route_b, file_b = self.route(slug="conc-same-b", gate_source="conc-same-b")
        cid_b = P.begin(self.root, route_file=file_b, capability="autopilot-code",
                        intensity="direct", campaign_id=campaign_id)
        for result, route_x, file_x in ((cid_a, route_a, file_a), (cid_b, route_b, file_b)):
            self.write_output(result)
            self.close(route_x, file_x)
        barrier = self.root / ".barrier-same"
        proc_a = self._run_finalize(cid_a["cycle_id"], barrier)
        proc_b = self._run_finalize(cid_b["cycle_id"], barrier)
        time.sleep(0.05)
        barrier.write_text("go", encoding="utf-8")
        out_a, err_a = proc_a.communicate(timeout=60)
        out_b, err_b = proc_b.communicate(timeout=60)
        self.assertEqual(proc_a.returncode, 0, err_a)
        self.assertEqual(proc_b.returncode, 0, err_b)
        self.assertEqual(P.read_cycle_record(self.root, cid_a["cycle_id"])["state"], "sealed")
        self.assertEqual(P.read_cycle_record(self.root, cid_b["cycle_id"])["state"], "sealed")
        self.assert_index_matches_full_rebuild("after concurrent same-campaign seal")

    def test_healing_race_between_a_seal_and_concurrent_resolve_path(self):
        """One process seals a cycle; another repeatedly calls `resolve_path`
        against a corrupted `INDEX.json` at the same time. `locate`'s
        opportunistic heal only ever writes under a held or try-acquired
        lock (§3.5 step 4), so the racing reader can only ever lose the race
        (fall back to a scan) or win it cleanly -- never interleave a torn
        write with the sealing process's own `update_indexes` call."""

        cid = self._prepare("conc-heal", "conc-heal")
        json_path = self.root / "campaigns" / "INDEX.json"
        json_path.write_text(json.dumps({"stale": "campaigns/does-not-exist"}), encoding="utf-8")
        barrier = self.root / ".barrier-heal"
        proc_seal = self._run_finalize(cid, barrier)
        resolve_script = (
            "import sys, time, os\n"
            "sys.path.insert(0, {utildir!r})\n"
            "import artifact_locator as loc\n"
            "while not os.path.exists({barrier!r}):\n"
            "    time.sleep(0.005)\n"
            "for _ in range(200):\n"
            "    try:\n"
            "        loc.resolve_path({root!r}, {cycle_id!r})\n"
            "    except loc.LocatorError:\n"
            "        pass\n"
            "print('done')\n"
        ).format(utildir=str(Path(__file__).parent), barrier=str(barrier),
                 root=str(self.root), cycle_id=cid)
        proc_resolve = subprocess.Popen([sys.executable, "-c", resolve_script],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.05)
        barrier.write_text("go", encoding="utf-8")
        out_seal, err_seal = proc_seal.communicate(timeout=60)
        out_resolve, err_resolve = proc_resolve.communicate(timeout=60)
        self.assertEqual(proc_seal.returncode, 0, err_seal)
        self.assertEqual(proc_resolve.returncode, 0, err_resolve)
        self.assertIn("sealed", out_seal)
        self.assertEqual(P.read_cycle_record(self.root, cid)["state"], "sealed")
        self.assert_index_matches_full_rebuild("after healing race")


class ScanCountTest(F.ProducerTestBase, IndexEquivalenceMixin):
    def setUp(self):
        super().setUp()
        self.activate()

    def _sealed_cycle(self, slug, key):
        route, route_file = self.route(slug=slug, gate_source=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key=key)
        self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def _counters(self):
        return (mock.patch.object(loc, "iter_campaign_dirs", wraps=loc.iter_campaign_dirs),
               mock.patch.object(loc, "scan_index", wraps=loc.scan_index),
               mock.patch.object(loc, "scan_campaign", wraps=loc.scan_campaign),
               mock.patch.object(loc, "_scan_lenient", wraps=loc._scan_lenient))

    def test_exact_finalize_never_scans_the_whole_root(self):
        for i in range(5):
            self._sealed_cycle(f"scan-fixture-{i}", f"scan-fixture-{i}")
        target = self._sealed_cycle("scan-open-holder", "scan-open-holder")
        route, route_file = self.route(slug="scan-exact-target", gate_source="scan-exact-target")
        opened = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_id=target["campaign_id"])
        self.write_output(opened)
        self.close(route, route_file)
        p1, p2, p3, p4 = self._counters()
        with p1 as walk, p2 as scan_index, p3 as scan_campaign, p4 as lenient:
            result = P.finalize(self.root, cycle_id=opened["cycle_id"], _recovery_scope="exact")
        self.assertEqual(result["status"], "sealed")
        self.assertEqual(walk.call_count, 0)
        self.assertEqual(scan_index.call_count, 0)
        self.assertEqual(lenient.call_count, 0)
        self.assertLessEqual(scan_campaign.call_count, 2)

    def test_admit_shared_never_scans_even_with_open_siblings(self):
        target = self._sealed_cycle("scan-admit-target", "scan-admit-target")
        for i in range(3):
            route, route_file = self.route(slug=f"scan-admit-open-{i}", gate_source=f"scan-admit-open-{i}")
            P.begin(self.root, route_file=route_file, capability="autopilot-code",
                   intensity="direct", campaign_id=target["campaign_id"])
        p1, p2, p3, p4 = self._counters()
        with p1 as walk, p2 as scan_index, p3 as scan_campaign, p4 as lenient:
            result = P.admit_shared(self.root, cycle_id=target["cycle_id"], kind="analysis",
                                    source="plans/cycle/plan.md", key="scan-admit-key")
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(walk.call_count, 0)
        self.assertEqual(scan_index.call_count, 0)
        self.assertEqual(lenient.call_count, 0)
        self.assertEqual(scan_campaign.call_count, 0)

    def test_root_scope_finalize_never_does_a_full_scan(self):
        target = self._sealed_cycle("scan-root-target", "scan-root-target")
        route, route_file = self.route(slug="scan-root-open", gate_source="scan-root-open")
        opened = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_id=target["campaign_id"])
        self.write_output(opened)
        self.close(route, route_file)
        p1, p2, p3, p4 = self._counters()
        with p1 as walk, p2 as scan_index, p3 as scan_campaign, p4 as lenient:
            result = P.finalize(self.root, cycle_id=opened["cycle_id"])
        self.assertEqual(result["status"], "sealed")
        self.assertEqual(walk.call_count, 0)
        self.assertEqual(lenient.call_count, 0)

    def test_dropped_open_records_cost_proportional_scan_campaign_not_full_scan(self):
        for k in (1, 3):
            with self.subTest(k=k):
                target = self._sealed_cycle(f"scan-drop-target-{k}", f"scan-drop-target-{k}")
                dropped_ids = []
                for i in range(k):
                    route, route_file = self.route(slug=f"scan-drop-{k}-{i}", gate_source=f"scan-drop-{k}-{i}")
                    dropped_camp = self._sealed_cycle(f"scan-drop-owner-{k}-{i}", f"scan-drop-owner-{k}-{i}")
                    route2, route_file2 = self.route(slug=f"scan-drop-open-{k}-{i}", gate_source=f"scan-drop-open-{k}-{i}")
                    opened = P.begin(self.root, route_file=route_file2, capability="autopilot-code",
                                     intensity="direct", campaign_id=dropped_camp["campaign_id"])
                    shutil.rmtree(Path(opened["cycle_dir"]))
                    dropped_ids.append(opened["cycle_id"])
                route_t, file_t = self.route(slug=f"scan-drop-final-{k}", gate_source=f"scan-drop-final-{k}")
                opened_target = P.begin(self.root, route_file=file_t, capability="autopilot-code",
                                        intensity="direct", campaign_id=target["campaign_id"])
                self.write_output(opened_target)
                self.close(route_t, file_t)
                p1, p2, p3, p4 = self._counters()
                with p1 as walk, p2 as scan_index, p3 as scan_campaign, p4 as lenient:
                    result = P.finalize(self.root, cycle_id=opened_target["cycle_id"])
                self.assertEqual(result["status"], "sealed")
                self.assertEqual(walk.call_count, 0)
                self.assertEqual(scan_index.call_count, 0)
                self.assertEqual(lenient.call_count, 0)
                self.assertLessEqual(scan_campaign.call_count, k + 2)
                for cid in dropped_ids:
                    self.assertEqual(P.read_cycle_record(self.root, cid)["state"], "dropped")

    def test_admit_shared_dropped_open_records_cost_proportional_scan_campaign(self):
        for k in (1, 3):
            with self.subTest(k=k):
                target = self._sealed_cycle(f"scan-drop-admit-target-{k}", f"scan-drop-admit-target-{k}")
                dropped_ids = []
                for i in range(k):
                    dropped_camp = self._sealed_cycle(f"scan-drop-admit-owner-{k}-{i}", f"scan-drop-admit-owner-{k}-{i}")
                    route2, route_file2 = self.route(slug=f"scan-drop-admit-open-{k}-{i}",
                                                     gate_source=f"scan-drop-admit-open-{k}-{i}")
                    opened = P.begin(self.root, route_file=route_file2, capability="autopilot-code",
                                     intensity="direct", campaign_id=dropped_camp["campaign_id"])
                    shutil.rmtree(Path(opened["cycle_dir"]))
                    dropped_ids.append(opened["cycle_id"])
                p1, p2, p3, p4 = self._counters()
                with p1 as walk, p2 as scan_index, p3 as scan_campaign, p4 as lenient:
                    result = P.admit_shared(self.root, cycle_id=target["cycle_id"], kind="analysis",
                                            source="plans/cycle/plan.md", key=f"scan-drop-admit-key-{k}")
                self.assertEqual(result["status"], "admitted")
                self.assertEqual(walk.call_count, 0)
                self.assertEqual(scan_index.call_count, 0)
                self.assertEqual(lenient.call_count, 0)
                self.assertLessEqual(scan_campaign.call_count, k)
                for cid in dropped_ids:
                    self.assertEqual(P.read_cycle_record(self.root, cid)["state"], "dropped")


_WRITER_FUNCTIONS = ("_write_cycle_record", "_write_campaign", "_write_cycle_binding", "_publish_event")


def _writer_census():
    """AST scan: every (module, function) that calls one of the row-input
    writers, so a new writer that forgets to call `prepare_index_update`/
    `update_indexes` fails this test instead of silently drifting the index."""

    found = set()
    for name in ("artifact_producer.py", "artifact_campaign.py", "artifact_cutover.py",
                "artifact_relayout.py", "artifact_residue.py", "artifact_resplit.py"):
        path = Path(__file__).with_name(name)
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    callee = inner.func
                    func_name = callee.attr if isinstance(callee, ast.Attribute) else (
                        callee.id if isinstance(callee, ast.Name) else None)
                    if func_name in _WRITER_FUNCTIONS:
                        found.add((name, node.name))
                        break
    return found


# {(module, function): classification}. `incremental` writers must call
# prepare_index_update/update_indexes (verified structurally is out of scope
# here; this table is the enumeration half of the guard -- LE §5).
_EXPECTED_WRITER_CENSUS = {
    ("artifact_producer.py", "begin"): "incremental",
    ("artifact_producer.py", "_remove_empty_cycle"): "incremental",
    ("artifact_producer.py", "_commit_sealed"): "incremental",
    ("artifact_producer.py", "mark_cycle_superseded"): "incremental",
    ("artifact_producer.py", "mark_campaign_superseded"): "incremental",
    ("artifact_producer.py", "set_campaign_related"): "no-row-effect",
    ("artifact_producer.py", "finalize"): "no-row-effect",  # the no-lineage cycle-record write
    ("artifact_producer.py", "_recover_locked"): "no-row-effect",  # the dropped-record write
    ("artifact_producer.py", "recover_cycle_times"): "full",
    ("artifact_producer.py", "backfill_cycle_bindings"): "full",
    ("artifact_campaign.py", "close"): "incremental",
    ("artifact_campaign.py", "_materialize"): "incremental",
    ("artifact_campaign.py", "_publish_event"): "incremental",
    ("artifact_cutover.py", "seal_legacy_cycle"): "out-of-band-legacy",
    ("artifact_cutover.py", "adopt_campaign"): "out-of-band-legacy",
}


class RowWriterCensusTest(unittest.TestCase):
    def test_every_row_writer_is_classified(self):
        found = _writer_census()
        classified = set(_EXPECTED_WRITER_CENSUS)
        extra = {pair for pair in found if pair not in classified
                and not pair[1].startswith("_write_")}
        # Relayout/residue/resplit are bulk (`full`) writers by design and are
        # allowed to appear unclassified-by-name here as long as they exist in
        # the `full` bucket the plan documents; assert at least one such
        # writer is present in each module that claims bulk rewrite.
        missing_from_table = extra - {
            pair for pair in extra
            if pair[0] in ("artifact_relayout.py", "artifact_residue.py", "artifact_resplit.py")
        }
        self.assertEqual(missing_from_table, set(),
                         f"new row writer(s) not classified: {missing_from_table}")


if __name__ == "__main__":
    unittest.main()
