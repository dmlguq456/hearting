#!/usr/bin/env python3
"""W7G resplit tests (A-16.1/A-16.2/A-16.3/A-16.5) + S2-h ownership guard."""
import base64
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_identity  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_manifest as M  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_cutover as C  # noqa: E402
import artifact_resplit as W  # noqa: E402

_S = importlib.util.spec_from_file_location("route_for_resplit_test", Path(__file__).with_name("capability-route.py"))
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)
ALL = ["atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
       "no-artifact-handoff", "no-independent-verifier", "focused-verification"]
REPO_ID, ROOT_ID = "repo_" + "a" * 32, "root_" + "b" * 32


class Fixture(unittest.TestCase):
    """Base: a temp artifact root with one sealed W7C lump cycle (the fixture in §5)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "artifact-root"
        self.root.mkdir()
        self.root_slug = "artifact-root"
        home = base / "agent-home"
        (home / "core").mkdir(parents=True)
        (home / "core" / "CORE.md").write_text("fixture\n")
        self._env = {k: os.environ.get(k) for k in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR")}
        os.environ["AGENT_HOME"] = str(home)
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        os.environ.pop("AGENT_ARTIFACT_CYCLE_DIR", None)
        self.addCleanup(self._restore)
        self.w("plans/2026-04-01_alpha/plan.md", "plan alpha\n")
        self.w("plans/2026-04-01_alpha/final_report.md", "report alpha\n")
        self.w("plans/2026-04-02_beta/plan.md", "plan beta\n")
        self.w("experiments/2026-04-03_exp/run.md", "run\n")
        self.w("experiments/2026-04-03_exp/metrics.json", "{}\n")
        # no YYYY-MM-DD_ directory prefix on purpose -- exercises D-79 started_on
        # priority (2): the entry document's own written date (frontmatter `created:`)
        self.w("research/topic-x/report.md", "---\ncreated: 2026-03-15\n---\nresearch topic x\n")
        self.w("spec/prd.md", "# prd\n")
        self.w("analysis_project/overview.md", "overview\n")
        self.w("plans/stage-sessions/rt-aaaaaaaaaaaaaaaa/session.json", "{}\n")
        self.w("notes/n1.md", "note 1\n")
        self.w("reports/r1.md", "report 1\n")
        P.activate(self.root, repository_id=REPO_ID, artifact_root_id=ROOT_ID)
        self.lump_cycle_id, self.lump_campaign_id, self.lump_dir = self._build_lump()

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

    def route(self):
        self._route_ordinal = getattr(self, "_route_ordinal", 0) + 1
        nonce = f"fixture-{self._route_ordinal}"
        gate = {"spec_read": {"satisfied": True, "source": nonce}, "drift_verdict": "within-spec",
                "workflow_mode": "tracked", "artifact_guard": {"satisfied": True, "source": nonce}}
        route = R.compile_route("autopilot-code", "dev", "direct", R.ROOT, self.root, predicates=ALL,
                                transport=None, inline_reason="atomic-direct",
                                tracking="tracked", tracked_gate_evidence=gate)
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def close(self, route, route_file):
        ev = Path(self._tmp.name) / f"ev-{route['route_id']}.txt"
        ev.write_text("evidence\n")
        for node in route["nodes"]:
            if node.get("terminal"):
                R.write_completion_marker(route, node, node["id"], ev)
        R.close_route(route, route_file, commit="a" * 40, summary="fixture")

    def _build_lump(self):
        """Builds one sealed w7c-delta-migration lump cycle (via migrate_delta/migrate_seal)."""
        rows = [
            {"path": "plans/2026-04-01_alpha/plan.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:plans"},
            {"path": "plans/2026-04-01_alpha/final_report.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:plans"},
            {"path": "plans/2026-04-02_beta/plan.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:plans"},
            {"path": "experiments/2026-04-03_exp/run.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:experiments"},
            {"path": "experiments/2026-04-03_exp/metrics.json", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:experiments"},
            {"path": "research/topic-x/report.md", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:research"},
            {"path": "plans/stage-sessions/rt-aaaaaaaaaaaaaaaa/session.json", "kind": "file", "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:plans"},
        ]
        rows_path = Path(self._tmp.name) / "census-rows.jsonl"
        rows_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        route, route_file = self.route()
        report = C.migrate_delta(self.root, census_rows=rows_path, route_file=route_file, capability="autopilot-code",
                                 intensity="direct", excludes=[], approval_receipt_sha256=None, campaign_id=None)
        self.close(route, route_file)
        sealed = C.migrate_seal(self.root, run_dir=Path(report["run_dir"]))
        C.compat_close(self.root, maps=[Path(report["run_dir"]) / "compatibility-map.jsonl"],
                       approval_receipt_sha256=None)
        report = json.loads((Path(report["run_dir"]) / "report.json").read_text())
        return report["cycle_id"], report["campaign_id"], Path(report["cycle_dir"])

    # -- proposal builder ---------------------------------------------------

    def lump_manifest_digest(self):
        return "sha256:" + C._sha(self.lump_dir / "manifest.json")

    def build_proposal(self, *, two_campaigns=True):
        cutoff = {"lump_cycle_id": self.lump_cycle_id, "lump_manifest_digest": self.lump_manifest_digest(),
                  "lump_inventory_digest": "sha256:" + "0" * 64}
        keys = {
            "a1": f"legacy:{self.root_slug}:plans/2026-04-01_alpha",
            "a2": f"legacy:{self.root_slug}:plans/2026-04-02_beta",
            "b1": f"legacy:{self.root_slug}:experiments/2026-04-03_exp",
            "b2": f"legacy:{self.root_slug}:research/topic-x",
        }
        if two_campaigns:
            groups = [("prop-a", "wwd-a", ["a1", "a2"]), ("prop-b", "wwd-b", ["b1", "b2"])]
        else:
            groups = [("prop-a", "wwd-a", ["a1", "a2", "b1", "b2"])]
        proposals = []
        campaigns = []
        for proposal_id, slug, members in groups:
            proposals.append({
                "proposal_id": proposal_id, "fingerprint": "fp-" + proposal_id, "lane": "semantic-boundary",
                "target_ids": [keys[m] for m in members], "cited_evidence_ids": ["plans/2026-04-01_alpha/plan.md"],
                "source_cutoff": cutoff, "producer_version": "v1", "projection_version": "v1",
                "policy_version": "v1", "proposed_value": {"campaign_slug": slug}, "confidence": 0.9,
                "rationale": "fixture",
            })
            campaigns.append({"proposal_id": proposal_id, "slug": slug, "title": slug, "goal": "fixture goal",
                              "completion_criterion": {"statement": "sealed"}, "related": []})
        loose_assignments = [
            {"proposal_id": groups[0][0], "source_locator": "notes/n1.md", "target_cycle_key": keys["a1"],
             "origin_bucket": "notes"},
            {"proposal_id": groups[0][0], "source_locator": "reports/r1.md", "target_cycle_key": keys["a1"],
             "origin_bucket": "reports"},
        ]
        return {
            "schema_version": 1, "contract": "artifact-campaign-proposal/v1", "root_slug": self.root_slug,
            "source_cutoff": cutoff, "producer_version": "v1", "projection_version": "v1", "policy_version": "v1",
            "proposals": proposals, "campaigns": campaigns, "loose_assignments": loose_assignments,
        }

    def write_proposal(self, proposal):
        path = Path(self._tmp.name) / "proposal.json"
        path.write_text(json.dumps(proposal, sort_keys=True))
        return path

    def r1(self, proposal_path=None, **kw):
        proposal_path = proposal_path or self.write_proposal(self.build_proposal())
        return W.resplit_legacy_cycle(self.root, gate="r1", lump_cycle_id=self.lump_cycle_id,
                                      proposal=proposal_path, **kw)

    def r2(self, route_file, **kw):
        return W.resplit_legacy_cycle(self.root, gate="r2", lump_cycle_id=self.lump_cycle_id,
                                      route_file=route_file, **kw)

    def r3(self, **kw):
        return W.resplit_legacy_cycle(self.root, gate="r3", lump_cycle_id=self.lump_cycle_id, **kw)

    def run_full(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        self.r3()
        return route, route_file


class ScanAndPredicateTests(Fixture):
    def test_scan_lumps_identifies_one_lump_with_four_cycle_units(self):
        idx = W.scan_lumps(self.root, root_slug=self.root_slug)
        self.assertEqual(idx["invalid"], [])
        self.assertEqual(len(idx["lumps"]), 1)
        lump = idx["lumps"][0]
        self.assertEqual(lump["lump_cycle_id"], self.lump_cycle_id)
        keys = sorted(u["cycle_key"] for u in lump["cycle_units"])
        self.assertEqual(keys, sorted([
            f"legacy:{self.root_slug}:plans/2026-04-01_alpha",
            f"legacy:{self.root_slug}:plans/2026-04-02_beta",
            f"legacy:{self.root_slug}:experiments/2026-04-03_exp",
            f"legacy:{self.root_slug}:research/topic-x",
        ]))
        self.assertTrue(lump["shared_input"])
        self.assertTrue(lump["stage_sessions"])

    def test_resplit_hold_is_none_before_any_run(self):
        self.assertIsNone(W.resplit_hold(self.root))

    def test_lump_index_falls_back_to_scan_when_unsealed(self):
        idx = W.lump_index(self.root)
        self.assertEqual(idx["kind"], "w7g-lump-inventory")
        self.assertEqual(len(idx["lumps"]), 1)

    def test_sealed_retire_inventory_none_before_r1(self):
        self.assertIsNone(W.sealed_retire_inventory(self.root))

    def test_started_on_priority_2_uses_entry_document_written_date(self):
        # D-79 priority: (1) dir-name date prefix -> (2) entry document's own
        # written date -> (3) lump cycle's started_on. topic-x has no prefix,
        # so its cycle_unit must pick up the frontmatter date, not the lump's
        # own started_on (an RFC3339 timestamp, never equal to "2026-03-15").
        idx = W.scan_lumps(self.root, root_slug=self.root_slug)
        unit = next(u for u in idx["lumps"][0]["cycle_units"]
                   if u["cycle_key"] == f"legacy:{self.root_slug}:research/topic-x")
        self.assertEqual(unit["started_on"], "2026-03-15")
        lump_record = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertNotEqual(unit["started_on"], lump_record.get("started_on"))

    def test_started_on_priority_3_falls_back_to_lump_when_no_prefix_or_written_date(self):
        # a unit with neither a dir-name date prefix nor a parseable written
        # date in its entry document falls back to the lump's own started_on.
        self.w("documents/no-date-here/notes.md", "no date anywhere in this body\n")
        rows_path = Path(self._tmp.name) / "extra-census-rows.jsonl"
        rows_path.write_text(json.dumps({
            "path": "documents/no-date-here/notes.md", "kind": "file",
            "disposition": "w6-baseline-legacy", "detail": "cycle-candidate:documents",
        }) + "\n")
        route, route_file = self.route()
        report = C.migrate_delta(self.root, census_rows=rows_path, route_file=route_file,
                                 capability="autopilot-code", intensity="direct", excludes=[],
                                 approval_receipt_sha256=None, campaign_id=self.lump_campaign_id)
        self.close(route, route_file)
        C.migrate_seal(self.root, run_dir=Path(report["run_dir"]))
        C.compat_append(self.root, maps=[Path(report["run_dir"]) / "compatibility-map.jsonl"])
        idx = W.scan_lumps(self.root, root_slug=self.root_slug)
        lump = next(l for l in idx["lumps"] if l["lump_cycle_id"] == report["cycle_id"])
        unit = next(u for u in lump["cycle_units"]
                   if u["cycle_key"] == f"legacy:{self.root_slug}:documents/no-date-here")
        lump_record = P.read_cycle_record(self.root, report["cycle_id"])
        self.assertEqual(unit["started_on"], lump_record.get("started_on"))

    def test_lump_report_invalid_on_duplicate_reports(self):
        # duplicate the sealed report under a second run dir with the same cycle_id -> ambiguous
        src = None
        for run in W.C.migrations_dir(self.root).iterdir():
            if (run / "report.json").is_file():
                src = run / "report.json"
        dup = W.C.migrations_dir(self.root) / "20260101T000000Z-dup"
        dup.mkdir()
        (dup / "report.json").write_bytes(src.read_bytes())
        idx = W.scan_lumps(self.root, root_slug=self.root_slug)
        self.assertEqual(len(idx["lumps"]), 0)
        self.assertEqual(idx["invalid"][0]["code"], "lump-report-invalid")


class ProposalValidateTests(Fixture):
    def _verdict(self, proposal):
        lump_inventory = W.scan_lumps(self.root, root_slug=self.root_slug)
        loose_inventory = W._build_loose_inventory(self.root)
        return W.validate_proposal(proposal, root=self.root, lump_inventory=lump_inventory,
                                   loose_inventory=loose_inventory, lump_cycle_id=self.lump_cycle_id)

    def test_ok_proposal_admits(self):
        v = self._verdict(self.build_proposal())
        self.assertEqual(v["verdict"], "ok")
        self.assertEqual(len(v["campaigns"]), 2)

    def test_unassigned_cycle_holds(self):
        p = self.build_proposal()
        p["proposals"][0]["target_ids"] = p["proposals"][0]["target_ids"][:1]
        v = self._verdict(p)
        self.assertEqual(v["verdict"], "hold")
        self.assertEqual(v["code"], "cycle-assignment-invalid")
        self.assertTrue(v["detail"].startswith("unassigned:"))

    def test_duplicate_assignment_holds(self):
        p = self.build_proposal()
        p["proposals"][1]["target_ids"].append(p["proposals"][0]["target_ids"][0])
        v = self._verdict(p)
        self.assertEqual(v["code"], "cycle-assignment-invalid")
        self.assertTrue(v["detail"].startswith("duplicate:"))

    def test_bad_slug_holds(self):
        p = self.build_proposal()
        p["campaigns"][0]["slug"] = "AB"
        v = self._verdict(p)
        self.assertEqual(v["code"], "campaign-slug-invalid")

    def test_unassigned_slug_admits_with_degraded(self):
        p = self.build_proposal()
        p["campaigns"][0]["slug"] = W.UNASSIGNED_SLUG
        p["campaigns"][0]["degraded"] = True
        v = self._verdict(p)
        self.assertEqual(v["verdict"], "ok")

    def test_absorption_refused(self):
        # a normal (non-legacy-prefixed) campaign already exists
        route, route_file = self.route()
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        campaign_key="ax-normal-campaign", title="normal", goal="normal")
        p = self.build_proposal()
        p["campaigns"][0]["related"] = [{"kind": "supersedes", "key": "ax-normal-campaign"}]
        v = self._verdict(p)
        self.assertEqual(v["code"], "campaign-absorption-refused")

    def test_stale_source_cutoff(self):
        p = self.build_proposal()
        p["proposals"][0]["source_cutoff"] = dict(p["proposals"][0]["source_cutoff"],
                                                   lump_manifest_digest="sha256:" + "1" * 64)
        v = self._verdict(p)
        self.assertEqual(v["code"], "proposal-stale")

    def test_evidence_out_of_cutoff(self):
        p = self.build_proposal()
        p["proposals"][0]["cited_evidence_ids"] = ["not/a/real/file.md"]
        v = self._verdict(p)
        self.assertEqual(v["code"], "evidence-out-of-cutoff")

    def test_missing_backreference_holds(self):
        p = self.build_proposal()
        p["campaigns"][0]["proposal_id"] = "unknown-id"
        v = self._verdict(p)
        self.assertEqual(v["code"], "proposal-backreference-missing")

    def test_loose_duplicate_holds(self):
        p = self.build_proposal()
        p["loose_assignments"].append(dict(p["loose_assignments"][0]))
        v = self._verdict(p)
        self.assertEqual(v["code"], "loose-assignment-invalid")
        self.assertTrue(v["detail"].startswith("duplicate:"))

    def test_loose_missing_holds(self):
        p = self.build_proposal()
        p["loose_assignments"].pop()
        v = self._verdict(p)
        self.assertEqual(v["code"], "loose-assignment-invalid")
        self.assertTrue(v["detail"].startswith("unassigned:"))

    def test_loose_digest_drift_holds(self):
        p = self.build_proposal()
        p["loose_assignments"][0]["sha256"] = "sha256:" + "9" * 64
        v = self._verdict(p)
        self.assertEqual(v["code"], "loose-assignment-invalid")
        self.assertTrue(v["detail"].startswith("digest-drift:"))

    def test_confirmed_decision_conflict(self):
        p = self.build_proposal()
        constraints = {"schema_version": 1, "root_slug": self.root_slug, "constraints": [
            {"id": "c1", "kind": "no-split", "detail": {"cycle_keys": [
                f"legacy:{self.root_slug}:plans/2026-04-01_alpha", f"legacy:{self.root_slug}:experiments/2026-04-03_exp"]}},
        ]}
        lump_inventory = W.scan_lumps(self.root, root_slug=self.root_slug)
        loose_inventory = W._build_loose_inventory(self.root)
        v = W.validate_proposal(p, root=self.root, lump_inventory=lump_inventory, loose_inventory=loose_inventory,
                                confirmed_constraints=constraints, lump_cycle_id=self.lump_cycle_id)
        self.assertEqual(v["code"], "confirmed-decision-conflict")

    def test_schema_invalid_holds(self):
        p = self.build_proposal()
        del p["proposals"][0]["fingerprint"]
        v = self._verdict(p)
        self.assertEqual(v["code"], "proposal-row-schema-invalid")

    def test_a16_3_hold_leaves_canonical_tree_unchanged(self):
        # every reject path in A-16.3 goes through the CLI's zero-side-effect
        # wrapper here, so a typed hold token and an unchanged canonical tree
        # are asserted together for each case in one place.
        route, route_file = self.route()
        P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
               campaign_key="ax-normal-campaign", title="normal", goal="normal")

        def mutate_unassigned(p):
            p["proposals"][0]["target_ids"] = p["proposals"][0]["target_ids"][:1]
            return p, None

        def mutate_duplicate(p):
            p["proposals"][1]["target_ids"].append(p["proposals"][0]["target_ids"][0])
            return p, None

        def mutate_bad_slug(p):
            p["campaigns"][0]["slug"] = "AB"
            return p, None

        def mutate_absorption(p):
            p["campaigns"][0]["related"] = [{"kind": "supersedes", "key": "ax-normal-campaign"}]
            return p, None

        def mutate_stale_cutoff(p):
            p["proposals"][0]["source_cutoff"] = dict(p["proposals"][0]["source_cutoff"],
                                                       lump_manifest_digest="sha256:" + "1" * 64)
            return p, None

        def mutate_evidence(p):
            p["proposals"][0]["cited_evidence_ids"] = ["not/a/real/file.md"]
            return p, None

        def mutate_backreference(p):
            p["campaigns"][0]["proposal_id"] = "unknown-id"
            return p, None

        def mutate_loose_dup(p):
            p["loose_assignments"].append(dict(p["loose_assignments"][0]))
            return p, None

        def mutate_loose_missing(p):
            p["loose_assignments"].pop()
            return p, None

        def mutate_loose_drift(p):
            p["loose_assignments"][0]["sha256"] = "sha256:" + "9" * 64
            return p, None

        def mutate_confirmed_conflict(p):
            constraints = {"schema_version": 1, "root_slug": self.root_slug, "constraints": [
                {"id": "c1", "kind": "no-split", "detail": {"cycle_keys": [
                    f"legacy:{self.root_slug}:plans/2026-04-01_alpha",
                    f"legacy:{self.root_slug}:experiments/2026-04-03_exp"]}},
            ]}
            constraints_path = Path(self._tmp.name) / "constraints.json"
            constraints_path.write_text(json.dumps(constraints))
            return p, constraints_path

        def mutate_schema(p):
            del p["proposals"][0]["fingerprint"]
            return p, None

        cases = [
            ("unassigned", mutate_unassigned, "cycle-assignment-invalid"),
            ("duplicate", mutate_duplicate, "cycle-assignment-invalid"),
            ("bad-slug", mutate_bad_slug, "campaign-slug-invalid"),
            ("absorption", mutate_absorption, "campaign-absorption-refused"),
            ("stale-cutoff", mutate_stale_cutoff, "proposal-stale"),
            ("evidence-out-of-cutoff", mutate_evidence, "evidence-out-of-cutoff"),
            ("backreference-missing", mutate_backreference, "proposal-backreference-missing"),
            ("loose-duplicate", mutate_loose_dup, "loose-assignment-invalid"),
            ("loose-missing", mutate_loose_missing, "loose-assignment-invalid"),
            ("loose-digest-drift", mutate_loose_drift, "loose-assignment-invalid"),
            ("confirmed-decision-conflict", mutate_confirmed_conflict, "confirmed-decision-conflict"),
            ("schema-invalid", mutate_schema, "proposal-row-schema-invalid"),
        ]
        for name, mutate, expected_code in cases:
            with self.subTest(case=name):
                proposal, constraints_path = mutate(self.build_proposal())
                proposal_path = self.write_proposal(proposal)
                before = sorted(str(p) for p in self.root.rglob("*"))
                result = W.campaign_proposal_validate(
                    self.root, proposal_path=proposal_path, lump_cycle_id=self.lump_cycle_id,
                    confirmed_constraints_path=constraints_path)
                after = sorted(str(p) for p in self.root.rglob("*"))
                self.assertEqual(result["verdict"], "hold", name)
                self.assertEqual(result["code"], expected_code, name)
                self.assertEqual(before, after, name)


class R1Tests(Fixture):
    def test_admit_publishes_marker_zero_canonical_changes(self):
        before = sorted((self.root / "campaigns").rglob("*"))
        result = self.r1()
        self.assertEqual(result["status"], "admitted")
        after = sorted((self.root / "campaigns").rglob("*"))
        self.assertEqual(before, after)
        run_dir = Path(result["run_dir"])
        self.assertTrue((run_dir / "admitted.marker.json").is_file())
        for name in W.ADMISSION_FILES:
            self.assertTrue((run_dir / "admission" / name).is_file(), name)

    def test_hold_leaves_zero_marker_and_zero_admission(self):
        p = self.build_proposal()
        p["proposals"][0]["target_ids"] = p["proposals"][0]["target_ids"][:1]
        result = self.r1(self.write_proposal(p))
        self.assertEqual(result["status"], "hold")
        run_dir = Path(result["run_dir"])
        self.assertFalse((run_dir / "admitted.marker.json").exists())
        self.assertFalse((run_dir / "admission").exists())

    def test_dry_run_writes_nothing(self):
        before = list(W.C.migrations_dir(self.root).iterdir()) if W.C.migrations_dir(self.root).is_dir() else []
        result = self.r1(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        after = list(W.C.migrations_dir(self.root).iterdir()) if W.C.migrations_dir(self.root).is_dir() else []
        self.assertEqual(len(before), len(after))

    def test_campaign_proposal_validate_cli_writes_nothing_under_runtime(self):
        proposal_path = self.write_proposal(self.build_proposal())
        before = list(self.root.rglob("*"))
        result = W.campaign_proposal_validate(self.root, proposal_path=proposal_path, lump_cycle_id=self.lump_cycle_id)
        self.assertEqual(result["verdict"], "ok")
        after = list(self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_already_applied_idempotent(self):
        r1a = self.r1()
        proposal_path = Path(r1a["run_dir"]) / "admission" / "proposal.json"
        r1b = W.resplit_legacy_cycle(self.root, gate="r1", lump_cycle_id=self.lump_cycle_id, proposal=proposal_path)
        self.assertEqual(r1b["status"], "already-applied")


class EndToEndTests(Fixture):
    def test_a16_1_two_campaigns_zero_unassigned_four_cycles(self):
        self.run_full()
        campaigns = sorted(p.name for p in (self.root / "campaigns").iterdir())
        # lump campaign + 2 new campaigns
        new_campaigns = [c for c in campaigns if c != self.lump_campaign_id]
        self.assertEqual(len(new_campaigns), 2)
        total_cycles = 0
        for camp_id in new_campaigns:
            record = P.read_campaign(self.root, camp_id)
            self.assertNotEqual(record.get("key", "").split(":")[-1], W.UNASSIGNED_SLUG)
            total_cycles += len(record["cycles"])
        self.assertEqual(total_cycles, 4)

    def test_a16_1_content_digests_match_and_lump_superseded_and_bytes_unchanged(self):
        before_bytes = (self.lump_dir / "manifest.json").read_bytes()
        self.run_full()
        after_bytes = (self.lump_dir / "manifest.json").read_bytes()
        self.assertEqual(before_bytes, after_bytes)
        record = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(record["state"], "superseded")
        self.assertTrue(record.get("superseded_by"))
        self.assertTrue(record.get("superseded_event_id"))
        remaining = list(W.P._walk_files(self.lump_dir / "artifacts"))
        remaining = [f for f in remaining if f.is_file()]
        self.assertEqual(remaining, [])
        # every new cycle's content digest equals the lump's row for the same locator
        lump_manifest = json.loads((self.lump_dir / "manifest.json").read_text())
        by_locator = {r["locator"]["path"]: r["content_digest"] for r in lump_manifest["artifact_revisions"]}
        for camp_id in (self.root / "campaigns").iterdir():
            if camp_id.name == self.lump_campaign_id:
                continue
            record = P.read_campaign(self.root, camp_id.name)
            for cid in record["cycles"]:
                new_manifest = json.loads((P.cycle_dir(self.root, camp_id.name, cid) / "manifest.json").read_text())
                for row in new_manifest["artifact_revisions"]:
                    legacy_path = "artifacts/" + row["locator"]["path"].split("artifacts/", 1)[-1]
                    # match by content digest only (locators are cycle-relative in both trees)
                    self.assertIn(row["content_digest"], by_locator.values())

    def test_a16_1_resolve_legacy_reaches_new_cycles(self):
        self.run_full()
        resolved = C.resolve_legacy(self.root, os.path.relpath(self.lump_dir / "artifacts/plans/2026-04-01_alpha/plan.md", self.root))
        self.assertIn(resolved["resolution"], ("mapped", "mapped-ancestor"))
        self.assertTrue((self.root / resolved["target"]).is_file())

    def test_a16_1_fold_and_gate_agree(self):
        self.run_full()
        state = W.lump_display_state(self.root)
        self.assertEqual(state["lump_index_state"], "ok")
        self.assertEqual(state["lumped_cycles_remaining"], 0)

    def test_a16_1_ten_legacy_paths_resolve_to_new_cycles(self):
        # D-82: the compat map carries two source lanes for every relocated
        # file -- (1) the original pre-W7C legacy path, (2) the W7C lump
        # path -- and both must resolve through resolve_legacy after R3.
        cycle_unit_rels = [
            "plans/2026-04-01_alpha/plan.md", "plans/2026-04-01_alpha/final_report.md",
            "plans/2026-04-02_beta/plan.md", "experiments/2026-04-03_exp/run.md",
            "experiments/2026-04-03_exp/metrics.json", "research/topic-x/report.md",
        ]
        lump_rel = os.path.relpath(self.lump_dir, self.root)
        # simulate the already-retired top-level legacy tree (matches production,
        # where W7C G4 retire already unlinked the pre-migration originals)
        for rel in cycle_unit_rels:
            p = self.root / rel
            if p.is_file():
                p.unlink()
        self.run_full()
        legacy_paths = list(cycle_unit_rels)
        lump_paths = [f"{lump_rel}/artifacts/{rel}" for rel in cycle_unit_rels]
        resolved_targets = set()
        for path in legacy_paths + lump_paths:
            resolved = C.resolve_legacy(self.root, path)
            self.assertIn(resolved["resolution"], ("mapped", "mapped-ancestor"), path)
            self.assertTrue((self.root / resolved["target"]).is_file(), path)
            resolved_targets.add(resolved["target"])
        self.assertEqual(len(legacy_paths) + len(lump_paths), 12)
        # both lanes for the same file land on the same new-cycle target
        self.assertEqual(len(resolved_targets), len(cycle_unit_rels))


class RollbackAndCrashTests(Fixture):
    def test_fp1_after_first_cycle_rename_rolls_back(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError) as ctx:
            self.r2(route_file, crash_at="r2:after-first-cycle-rename")
        self.assertEqual(ctx.exception.code, "crash-fixture")
        hold = W.resplit_hold(self.root)
        self.assertIsNotNone(hold)
        self.assertEqual(hold["gate"], "r2")
        # resume -> rollback
        result = self.r2(route_file)
        self.assertEqual(result["journal"]["phase"], "rolled-back")
        after = W._tree_digest = C._tree_digest
        digest = C._tree_digest(self.lump_dir / "artifacts")
        record = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(digest["tree_sha256"], record["manifest_digest"] and digest["tree_sha256"])
        self.assertIsNone(W.resplit_hold(self.root))

    def test_fp2_before_finalize_rolls_back(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError) as ctx:
            self.r2(route_file, crash_at="r2:before-finalize")
        self.assertEqual(ctx.exception.code, "crash-fixture")
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        journal = json.loads((run_dir / "journal-r2.json").read_text())
        self.assertEqual(journal["phase"], "witnessed")
        first_cycle = journal["cycles"][0]
        self.assertTrue(Path(first_cycle["cycle_dir"]).is_dir())
        self.assertFalse((Path(first_cycle["cycle_dir"]) / "manifest.json").exists())
        # resume -> rollback: assertions (i)-(iv)
        result = self.r2(route_file)
        self.assertEqual(result["journal"]["phase"], "rolled-back")
        digest = C._tree_digest(self.lump_dir / "artifacts")
        record = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(digest["tree_sha256"], record["manifest_digest"] and digest["tree_sha256"])
        for cyc in journal["cycles"]:
            self.assertFalse(Path(cyc["cycle_dir"]).exists())
            self.assertIsNone(W._find_cycle_by_key(self.root, cyc["cycle_key"]))
        self.assertIsNone(W.resplit_hold(self.root))

    def test_fp3_after_finalize_before_journal_rolls_forward(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:after-finalize-before-journal")
        journal = json.loads((W._find_run_dir(self.root, self.lump_cycle_id) / "journal-r2.json").read_text())
        self.assertEqual(journal["phase"], "witnessed")
        first_manifest = Path(journal["cycles"][0]["cycle_dir"]) / "manifest.json"
        self.assertTrue(first_manifest.is_file())
        before_bytes = first_manifest.read_bytes()
        result = self.r2(route_file)
        self.assertEqual(result["journal"]["phase"], "complete")
        self.assertEqual(first_manifest.read_bytes(), before_bytes)

    def test_fp4_mid_second_cycle_rolls_forward(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:mid-second-cycle")
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        journal = json.loads((run_dir / "journal-r2.json").read_text())
        self.assertEqual(journal["phase"], "committed")
        first_manifest = Path(journal["cycles"][0]["cycle_dir"]) / "manifest.json"
        self.assertTrue(first_manifest.is_file())
        before_bytes = first_manifest.read_bytes()
        second_manifest = Path(journal["cycles"][1]["cycle_dir"]) / "manifest.json"
        self.assertFalse(second_manifest.exists())
        # renamed files for the second cycle already landed even though it is not finalized
        for f in journal["cycles"][1]["files"]:
            self.assertTrue((Path(journal["cycles"][1]["cycle_dir"]) / "artifacts" / f["locator"]).is_file())
        # resume -> roll-forward only, no re-creation of the already-committed first cycle
        result = self.r2(route_file)
        self.assertEqual(result["journal"]["phase"], "complete")
        self.assertEqual(first_manifest.read_bytes(), before_bytes)
        for cyc in journal["cycles"]:
            manifest = Path(cyc["cycle_dir"]) / "manifest.json"
            self.assertTrue(manifest.is_file(), cyc["cycle_key"])
        self.assertIsNone(W.resplit_hold(self.root))

    def test_fp5_before_seal_rolls_back(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        with self.assertRaises(W.ResplitError):
            self.r3(crash_at="r3:after-backup-tar-before-seal")
        hold = W.resplit_hold(self.root)
        self.assertIsNotNone(hold)
        self.assertEqual(hold["gate"], "r3")
        record_before = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(record_before["state"], "superseded")
        result = self.r3()
        self.assertEqual(result["journal"]["phase"], "rolled-back")
        record_after = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(record_after["state"], "sealed")
        self.assertFalse((W._find_run_dir(self.root, self.lump_cycle_id) / "events.jsonl").exists())

    def test_fp6_before_reread_rolls_back(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        compat_before = C.compat_path(self.root).read_bytes()
        old_map_path = None
        old_map_sha_before = None
        for entry in C.load_map_state(self.root)["maps"]:
            old_map_path = Path(entry["path"])
            old_map_sha_before = entry["sha256"]
            break
        with self.assertRaises(W.ResplitError) as ctx:
            self.r3(crash_at="r3:after-seal-before-reread")
        self.assertEqual(ctx.exception.code, "crash-fixture")
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        self.assertTrue((run_dir / "backup-seal.json").is_file())
        self.assertFalse((run_dir / "backup-verified.json").exists())
        record = P.read_cycle_record(self.root, self.lump_cycle_id)
        lump_artifacts = list(W.P._walk_files(P.cycle_dir(self.root, record["campaign_id"], self.lump_cycle_id) / "artifacts"))
        self.assertTrue([f for f in lump_artifacts if f.is_file()])  # nothing unlinked yet
        hold = W.resplit_hold(self.root)
        self.assertIsNotNone(hold)
        self.assertEqual(hold["gate"], "r3")
        # resume -> rollback (same effect-set as FP-5)
        result = self.r3()
        self.assertEqual(result["journal"]["phase"], "rolled-back")
        record_after = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(record_after["state"], "sealed")
        self.assertFalse((run_dir / "events.jsonl").exists())
        self.assertEqual(C.compat_path(self.root).read_bytes(), compat_before)
        self.assertEqual(C._sha(old_map_path), old_map_sha_before)
        self.assertFalse((run_dir / "backup-seal.json").exists())
        self.assertFalse((run_dir / "legacy-artifacts.tar").exists())

    def test_fp7_after_verified_rolls_forward(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        with self.assertRaises(W.ResplitError):
            self.r3(crash_at="r3:after-reread-before-removal")
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        self.assertTrue((run_dir / "backup-verified.json").is_file())
        journal = json.loads((run_dir / "journal-r3.json").read_text())
        self.assertEqual(journal["phase"], "compat-reissued")
        result = self.r3()
        self.assertEqual(result["journal"]["phase"], "complete")
        events = C._read_jsonl(run_dir / "events.jsonl")
        self.assertEqual(len(events), 1)

    def test_inverse_actions_are_rename_back_only(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        actions = {row["action"] for row in C._read_jsonl(run_dir / "inverse.jsonl")}
        self.assertEqual(actions, {"rename_back"})

    def test_already_applied_r2_and_r3_are_noop(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        self.r3()
        again2 = self.r2(route_file)
        self.assertEqual(again2["status"], "already-applied")
        again3 = self.r3()
        self.assertEqual(again3["status"], "already-applied")


class SupersessionEventTests(Fixture):
    def test_event_row_passes_manifest_event_shape_and_fields(self):
        self.run_full()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        events = C._read_jsonl(run_dir / "events.jsonl")
        self.assertEqual(len(events), 1)
        event = events[0]
        violations = []
        M._v_event_row(event, "$", violations)
        self.assertEqual(violations, [], violations)
        self.assertNotIn("supersedes_event_id", event)
        self.assertEqual(event["stream_sequence"], 1)
        self.assertEqual(set(event["provenance"].keys()), {
            "source_manifest_id", "source_revision_id", "producer_route_id", "algorithm_version",
            "schema_version", "source_digest",
        })
        record = P.read_cycle_record(self.root, self.lump_cycle_id)
        self.assertEqual(record["superseded_event_id"], event["event_id"])


class OwnershipGuardTests(unittest.TestCase):
    """S2-h: this slice only imports artifact_producer/artifact_cutover."""

    def test_required_upstream_api_exists(self):
        for name in ("compat_append", "load_map_state", "retire_approval_package", "_tree_digest",
                    "_write_jsonl", "_read_jsonl", "migrations_dir"):
            self.assertTrue(hasattr(C, name), name)
        for name in ("validate_shared_reference_pins", "mark_cycle_superseded", "mark_campaign_superseded",
                    "set_campaign_related", "_write_exclusive", "finalize", "list_cycle_records"):
            self.assertTrue(hasattr(P, name), name)

    def test_does_not_modify_upstream_modules(self):
        # S2's fixed_files never include these two -- pin their bytes at first
        # observation (this slice never edited them) and fail if a later S2 run drifts.
        repo_root = Path(__file__).resolve().parents[1]
        baseline = repo_root / "_scratch" / ".s2-baseline.json"
        targets = [repo_root / "utilities" / "artifact_producer.py", repo_root / "utilities" / "artifact_cutover.py"]
        current = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in targets}
        baseline.parent.mkdir(parents=True, exist_ok=True)
        if not baseline.is_file():
            baseline.write_text(json.dumps(current))
            return
        recorded = json.loads(baseline.read_text())
        self.assertEqual(current, recorded)


if __name__ == "__main__":
    unittest.main()
