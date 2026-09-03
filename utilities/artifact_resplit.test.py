#!/usr/bin/env python3
"""W7G resplit tests (A-16.1/A-16.2/A-16.3/A-16.5) + S2-h ownership guard."""
import base64
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def build_proposal(self, *, two_campaigns=True, unassigned_loose=False):
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
        if unassigned_loose:
            # Mirrors the real hearting proposal: one degraded `_unassigned` campaign
            # backed by a semantic-boundary row with no cycle targets, and a loose row
            # pointing at it because no cycle could own the file.
            proposals.append({
                "proposal_id": "prop-u", "fingerprint": "fp-prop-u", "lane": "semantic-boundary",
                "target_ids": [], "cited_evidence_ids": [],
                "source_cutoff": cutoff, "producer_version": "v1", "projection_version": "v1",
                "policy_version": "v1", "proposed_value": {"campaign_slug": W.UNASSIGNED_SLUG},
                "confidence": 0.1, "rationale": "no cycle could own this residue",
            })
            campaigns.append({"proposal_id": "prop-u", "slug": W.UNASSIGNED_SLUG,
                              "title": "unassigned", "goal": "degraded fallback", "degraded": True,
                              "completion_criterion": {"statement": "sealed"}, "related": []})
            loose_assignments[1] = {
                "proposal_id": "prop-u", "source_locator": "reports/r1.md",
                "target_cycle_key": f"legacy:{self.root_slug}:{W.UNASSIGNED_SLUG}",
                "origin_bucket": "reports",
            }
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

    def backup_root(self):
        """D-84's rule reaches R3: the tar lives outside the artifact root, so every
        real R3 in these tests names one (`base/backup-root`, a sibling of `self.root`)."""
        path = Path(self._tmp.name) / "backup-root"
        path.mkdir(exist_ok=True)
        return path

    def r3(self, **kw):
        kw.setdefault("backup_root", self.backup_root())
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
        # D-6 wants an RFC3339 instant; D-79 yields a date. The date is widened to
        # that day's UTC midnight rather than being sealed as an invalid timestamp.
        self.assertEqual(unit["started_on"], "2026-03-15T00:00:00Z")
        self.assertEqual(unit["started_on_source"], "entry-document-date")
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
        # D-78: locator-keyed exact-set comparison -- (path, content_digest, byte_size)
        # for the lump's resplit population (shared-input/ and plans/stage-sessions/
        # excluded, matching scan_lumps) must equal the union across the new cycles
        # exactly, not "any value happens to be present" (that would pass even if two
        # files' digests were swapped).
        lump_manifest = json.loads((self.lump_dir / "manifest.json").read_text())

        def _eligible(path):
            if not path.startswith("artifacts/"):
                return None
            rel = path[len("artifacts/"):]
            parts = rel.split("/")
            if parts and parts[0] == "shared-input":
                return None
            # D-79 loose residue is relocated *into* a new cycle from the root's own
            # top level, so it was never part of the lump's population and must be
            # excluded from the D-78 conservation comparison on the new-cycle side.
            # Named exactly, so a lump-origin file that somehow landed under
            # `_internal/` would still fail this comparison.
            if rel in (f"{W.LOOSE_PREFIX}/notes/n1.md", f"{W.LOOSE_PREFIX}/reports/r1.md"):
                return None
            if len(parts) > 1 and parts[0] == "plans" and parts[1] == "stage-sessions":
                return None
            return rel

        lump_triples = set()
        for row in lump_manifest["artifact_revisions"]:
            rel = _eligible(row["locator"]["path"])
            if rel is None:
                continue
            lump_triples.add((rel, row["content_digest"], row["byte_size"]))
        new_triples = set()
        for camp_id in (self.root / "campaigns").iterdir():
            if camp_id.name == self.lump_campaign_id:
                continue
            record = P.read_campaign(self.root, camp_id.name)
            for cid in record["cycles"]:
                new_manifest = json.loads((P.cycle_dir(self.root, camp_id.name, cid) / "manifest.json").read_text())
                for row in new_manifest["artifact_revisions"]:
                    rel = _eligible(row["locator"]["path"])
                    if rel is None:
                        continue
                    new_triples.add((rel, row["content_digest"], row["byte_size"]))
        self.assertEqual(lump_triples, new_triples)
        self.assertEqual(len(lump_triples), len({t[0] for t in lump_triples}))
        self.assertEqual(len(new_triples), len({t[0] for t in new_triples}))
        self.assertEqual(len(lump_triples), len(new_triples))
        self.assertEqual(sum(t[2] for t in lump_triples), sum(t[2] for t in new_triples))

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

    def test_lane1_omission_surfaced_in_lump_display_state(self):
        # 🟡2: when lane① (the original pre-W7C path) cannot be derived for any
        # file, the omission must surface through the read-only
        # lump_display_state projection, not stay journal-only where an operator
        # could mistake an incomplete-lane resplit for a normal one. Mutation
        # count is unaffected by forcing the omission.
        with mock.patch.object(W, "_original_legacy_sources", return_value={}):
            self.run_full()
        state = W.lump_display_state(self.root)
        entry = next(l for l in state["lumps"] if l["lump_cycle_id"] == self.lump_cycle_id)
        self.assertTrue(entry["compat_lane1_incomplete"])
        self.assertEqual(entry["compat_lane1_omitted_count"], 6)

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


class LooseRelocationTests(Fixture):
    """D-79 "잔여 loose 파일" -- A-16.1's `loose 2` actually leaving the top level."""

    def _new_cycle_dirs(self):
        out = []
        for camp in (self.root / "campaigns").iterdir():
            if camp.name == self.lump_campaign_id:
                continue
            for cid in P.read_campaign(self.root, camp.name)["cycles"]:
                out.append(P.cycle_dir(self.root, camp.name, cid))
        return out

    def test_loose_two_are_relocated_under_internal_with_origin_bucket_preserved(self):
        self.assertTrue((self.root / "notes/n1.md").is_file())
        self.assertTrue((self.root / "reports/r1.md").is_file())
        self.run_full()
        target_key = f"legacy:{self.root_slug}:plans/2026-04-01_alpha"
        record = W._find_cycle_by_key(self.root, target_key)
        cdir = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])
        moved = {
            "_internal/notes/n1.md": "note 1\n",
            "_internal/reports/r1.md": "report 1\n",
        }
        for locator, body in moved.items():
            path = cdir / "artifacts" / locator
            self.assertTrue(path.is_file(), locator)
            self.assertEqual(path.read_text(), body)
        # same-filesystem rename, not a copy: the originals are gone, and no third
        # copy exists anywhere else in the root
        self.assertFalse((self.root / "notes/n1.md").exists())
        self.assertFalse((self.root / "reports/r1.md").exists())
        for name in ("n1.md", "r1.md"):
            found = [f for f in P._walk_files(self.root) if f.name == name]
            self.assertEqual(len(found), 1, name)

    def test_loose_rows_are_sealed_with_role_support_and_never_primary(self):
        self.run_full()
        target_key = f"legacy:{self.root_slug}:plans/2026-04-01_alpha"
        record = W._find_cycle_by_key(self.root, target_key)
        cdir = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])
        manifest = json.loads((cdir / "manifest.json").read_text())
        by_title = {a["title"]: a for a in manifest["artifacts"]}
        for locator in ("_internal/notes/n1.md", "_internal/reports/r1.md"):
            self.assertEqual(by_title[locator]["role"], "support", locator)
        # the cycle's own output rows keep their roles, and the primary is still a
        # lump-origin artifact
        self.assertEqual(by_title["plans/2026-04-01_alpha/final_report.md"]["role"], "primary")
        self.assertEqual(by_title["plans/2026-04-01_alpha/plan.md"]["role"], "output")

    def test_loose_files_are_covered_by_the_cycle_witness(self):
        # D-78 dual witness: a loose file that silently failed to move must fail the
        # pre-finalize witness, not slip through because only lump rows were counted.
        real_move = W._r2_move_loose

        def skip_one(root, run_dir, cyc, inverse_rows):
            cyc = dict(cyc, loose_files=[])
            return real_move(root, run_dir, cyc, inverse_rows)

        self.r1()
        route, route_file = self.route()
        with mock.patch.object(W, "_r2_move_loose", side_effect=skip_one):
            with self.assertRaises(W.ResplitError) as ctx:
                self.r2(route_file)
        self.assertEqual(ctx.exception.code, "resplit-stale")
        self.assertIn("witness-mismatch", ctx.exception.detail)

    def test_loose_original_path_resolves_to_the_new_cycle(self):
        self.run_full()
        for source in ("notes/n1.md", "reports/r1.md"):
            resolved = C.resolve_legacy(self.root, source)
            self.assertIn(resolved["resolution"], ("mapped", "mapped-ancestor"), source)
            target = self.root / resolved["target"]
            self.assertTrue(target.is_file(), source)
            self.assertIn(f"/{W.LOOSE_PREFIX}/{source}", resolved["target"])

    def test_loose_rows_are_excluded_from_the_sealed_retire_inventory(self):
        result = self.r1()
        self.assertEqual(sorted(result["loose_relocating"]), ["notes/n1.md", "reports/r1.md"])
        inv = W.sealed_retire_inventory(self.root)
        self.assertIsNotNone(inv)
        sources = {e["source_locator"] for e in inv["entries"]}
        self.assertNotIn("notes/n1.md", sources)
        self.assertNotIn("reports/r1.md", sources)
        for rel in ("notes/n1.md", "reports/r1.md"):
            self.assertIn(rel, inv["excludes"])

    def test_rollback_restores_relocated_loose_files(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:before-finalize")
        self.r2(route_file)  # retry rolls the uncommitted batch back
        journal = P._read_json(W._find_run_dir(self.root, self.lump_cycle_id) / "journal-r2.json")
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertEqual((self.root / "notes/n1.md").read_text(), "note 1\n")
        self.assertEqual((self.root / "reports/r1.md").read_text(), "report 1\n")

    def test_inverse_rows_for_loose_files_are_rename_back_only(self):
        # W7G forbids the `remove_file` inverse family (D-77-a): replaying it on a
        # rename would delete the only copy.
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:before-finalize")
        rows = C._read_jsonl(W._find_run_dir(self.root, self.lump_cycle_id) / "inverse.jsonl")
        self.assertTrue(rows)
        self.assertEqual({r["action"] for r in rows}, {"rename_back"})
        loose_rows = [r for r in rows if r["source_locator"] in ("notes/n1.md", "reports/r1.md")]
        self.assertEqual(len(loose_rows), 2)


class EmptyCampaignDeferralTests(Fixture):
    """A campaign no cycle unit was assigned to gets no record; its loose rows are
    reported as `loose-deferred` and left in place."""

    def _run(self):
        proposal = self.write_proposal(self.build_proposal(unassigned_loose=True))
        self.r1(proposal_path=proposal)
        route, route_file = self.route()
        return self.r2(route_file)

    def test_validate_still_passes_with_an_unassigned_targeted_loose_row(self):
        proposal = self.build_proposal(unassigned_loose=True)
        verdict = W.validate_proposal(
            proposal, root=self.root, lump_inventory=W.scan_lumps(self.root, root_slug=self.root_slug),
            loose_inventory=W._build_loose_inventory(self.root), lump_cycle_id=self.lump_cycle_id)
        self.assertEqual(verdict["verdict"], "ok")
        self.assertIn("7", verdict["rules_checked"])

    def test_no_campaign_record_is_created_for_the_cycleless_unassigned_campaign(self):
        self._run()
        keys = set()
        for camp in (self.root / "campaigns").iterdir():
            record = P.read_campaign(self.root, camp.name)
            if record:
                keys.add(record.get("key"))
        self.assertNotIn(f"legacy:{self.root_slug}:{W.UNASSIGNED_SLUG}", keys)
        new_campaigns = [c for c in (self.root / "campaigns").iterdir() if c.name != self.lump_campaign_id]
        self.assertEqual(len(new_campaigns), 2)

    def test_deferred_loose_row_is_typed_and_the_file_is_untouched(self):
        before = (self.root / "reports/r1.md").read_bytes()
        result = self._run()
        deferred = result["loose_deferred"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["status"], "loose-deferred")
        self.assertEqual(deferred[0]["source_locator"], "reports/r1.md")
        self.assertEqual(deferred[0]["reason"], "unassigned-campaign-has-no-cycle")
        self.assertEqual(deferred[0]["origin_bucket"], "reports")
        self.assertEqual((self.root / "reports/r1.md").read_bytes(), before)
        # the assigned sibling still moved
        self.assertFalse((self.root / "notes/n1.md").exists())

    def test_deferred_campaign_is_reported_and_journalled(self):
        result = self._run()
        campaigns = result["deferred_campaigns"]
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(campaigns[0]["slug"], W.UNASSIGNED_SLUG)
        self.assertEqual(campaigns[0]["reason"], "no-cycle-units")
        self.assertTrue(campaigns[0]["degraded"])
        journal = P._read_json(W._find_run_dir(self.root, self.lump_cycle_id) / "journal-r2.json")
        self.assertEqual(journal["deferred_campaigns"], campaigns)
        self.assertEqual(journal["loose_deferred"], result["loose_deferred"])

    def test_deferred_loose_row_stays_in_the_retire_inventory(self):
        # it was not relocated, so it is still a retirable top-level source
        proposal = self.write_proposal(self.build_proposal(unassigned_loose=True))
        result = self.r1(proposal_path=proposal)
        self.assertEqual(result["loose_relocating"], ["notes/n1.md"])
        self.assertEqual([d["source_locator"] for d in result["loose_deferred"]], ["reports/r1.md"])
        inv = W.sealed_retire_inventory(self.root)
        self.assertIn("notes/n1.md", inv["excludes"])
        self.assertNotIn("reports/r1.md", inv["excludes"])


class LooseGuardTests(Fixture):
    """Round-1 impl-review blockers: an unproduced target and an unmanifestable
    locator must both be refused *before* R1 seals anything."""

    def test_target_claimed_by_a_campaignless_row_is_refused_at_validate(self):
        # rule 1 counts `target_ids`, so this proposal used to reach an "ok" verdict
        # under which two cycle units were never produced and the lump was never
        # fully resplit.
        proposal = self.build_proposal()
        proposal["campaigns"] = [c for c in proposal["campaigns"] if c["proposal_id"] != "prop-b"]
        verdict = W.validate_proposal(
            proposal, root=self.root, lump_inventory=W.scan_lumps(self.root, root_slug=self.root_slug),
            loose_inventory=W._build_loose_inventory(self.root), lump_cycle_id=self.lump_cycle_id)
        self.assertEqual(verdict["verdict"], "hold")
        self.assertEqual(verdict["code"], "cycle-assignment-invalid")
        self.assertTrue(verdict["detail"].startswith("unplaced:"), verdict["detail"])

    def test_loose_row_pointing_at_an_unproduced_cycle_is_deferred_not_dropped(self):
        # `loose_plan` classifies against the produced plan, not the lump inventory:
        # a target that will not be created must be deferred (and therefore stay in
        # the D-84 retire inventory), never silently skipped.
        verdict = {
            "root_slug": self.root_slug,
            "assignments": {f"legacy:{self.root_slug}:plans/2026-04-01_alpha": "wwd-a"},
            "lump": {"cycle_units": [{"cycle_key": f"legacy:{self.root_slug}:experiments/2026-04-03_exp"}]},
            "loose": [{"source_locator": "reports/r1.md",
                      "target_cycle_key": f"legacy:{self.root_slug}:experiments/2026-04-03_exp",
                      "origin_bucket": "reports"}],
        }
        assigned, deferred = W.loose_plan(verdict, W._build_loose_inventory(self.root))
        self.assertEqual(assigned, {})
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["reason"], "target-cycle-not-produced")

    def test_unmanifestable_loose_locator_is_deferred_before_r1_seals(self):
        # A hidden component cannot be a D-6 locator. Renaming it in and only
        # finding out at `finalize` is unrecoverable past the batch's first commit,
        # so the row is refused here and the file stays where it is.
        self.w("notes/.hidden.md", "hidden\n")
        proposal = self.build_proposal()
        proposal["loose_assignments"].append({
            "proposal_id": "prop-a", "source_locator": "notes/.hidden.md",
            "target_cycle_key": f"legacy:{self.root_slug}:plans/2026-04-01_alpha",
            "origin_bucket": "notes"})
        result = self.r1(proposal_path=self.write_proposal(proposal))
        self.assertEqual(result["status"], "admitted")
        self.assertNotIn("notes/.hidden.md", result["loose_relocating"])
        bad = [d for d in result["loose_deferred"] if d["source_locator"] == "notes/.hidden.md"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["reason"], "unmanifestable-locator")
        self.assertEqual(bad[0]["detail"], "hidden-component")
        # it stays retirable precisely because it was not relocated
        self.assertNotIn("notes/.hidden.md", W.sealed_retire_inventory(self.root)["excludes"])

    def test_unmanifestable_loose_locator_does_not_wedge_r2(self):
        self.w("notes/.hidden.md", "hidden\n")
        proposal = self.build_proposal()
        proposal["loose_assignments"].append({
            "proposal_id": "prop-a", "source_locator": "notes/.hidden.md",
            "target_cycle_key": f"legacy:{self.root_slug}:plans/2026-04-01_alpha",
            "origin_bucket": "notes"})
        self.r1(proposal_path=self.write_proposal(proposal))
        route, route_file = self.route()
        self.assertEqual(self.r2(route_file)["status"], "complete")
        self.assertEqual((self.root / "notes/.hidden.md").read_text(), "hidden\n")
        self.assertIsNone(W.resplit_hold(self.root))

    def test_r1_replay_still_reports_the_loose_split(self):
        path = self.write_proposal(self.build_proposal())
        first = self.r1(proposal_path=path)
        replay = self.r1(proposal_path=path)
        self.assertEqual(replay["status"], "already-applied")
        self.assertEqual(replay["loose_relocating"], first["loose_relocating"])
        self.assertEqual(replay["loose_deferred"], first["loose_deferred"])

    def test_r2_replay_still_reports_the_deferrals(self):
        self.r1(proposal_path=self.write_proposal(self.build_proposal(unassigned_loose=True)))
        route, route_file = self.route()
        first = self.r2(route_file)
        replay = self.r2(route_file)
        self.assertEqual(replay["status"], "already-applied")
        self.assertEqual(replay["loose_deferred"], first["loose_deferred"])
        self.assertEqual(replay["deferred_campaigns"], first["deferred_campaigns"])

    def test_r2_dry_run_does_not_call_a_rolled_back_journal_resumable(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:before-finalize")
        self.r2(route_file)  # rolls back
        result = self.r2(route_file, dry_run=True)
        self.assertFalse(result["resumes_existing_journal"])
        self.assertIsNone(result["journal"])
        self.assertEqual(len(result["cycles"]), 4)


class RootSlugTests(unittest.TestCase):
    """D-79 `<root-slug>`: the roster `repo_path` basename, not the artifact-root
    container directory name."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_agent_reports_root_slugs_from_the_repo_directory(self):
        root = self.base / "hearting" / ".agent_reports"
        root.mkdir(parents=True)
        self.assertEqual(W._default_root_slug(root), "hearting")

    def test_legacy_claude_reports_root_slugs_from_the_repo_directory(self):
        root = self.base / "SR_CorrNet_DSC" / ".claude_reports"
        root.mkdir(parents=True)
        self.assertEqual(W._default_root_slug(root), "sr-corrnet-dsc")

    def test_symlinked_repo_slugs_from_its_realpath(self):
        real = self.base / "BC_ResNet"
        (real / ".agent_reports").mkdir(parents=True)
        link = self.base / "BC_ResNet_tf"
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(W._default_root_slug(link / ".agent_reports"), "bc-resnet")

    def test_a_slug_that_cannot_fill_the_cycle_key_field_is_refused(self):
        long_name = "x" * 65
        root = self.base / long_name / ".agent_reports"
        root.mkdir(parents=True)
        with self.assertRaises(W.ResplitError) as ctx:
            W._default_root_slug(root)
        self.assertEqual(ctx.exception.code, "root-slug-invalid")

    def test_a_name_with_no_ascii_alphanumerics_is_refused_not_collapsed_to_root(self):
        root = self.base / "한글저장소" / ".agent_reports"
        root.mkdir(parents=True)
        with self.assertRaises(W.ResplitError) as ctx:
            W._default_root_slug(root)
        self.assertEqual(ctx.exception.code, "root-slug-invalid")
        self.assertIn("underivable", ctx.exception.detail)

    def test_an_explicit_root_slug_is_validated_too(self):
        root = self.base / "hearting" / ".agent_reports"
        root.mkdir(parents=True)
        with self.assertRaises(W.ResplitError) as ctx:
            W.scan_lumps(root, root_slug="Not A Slug")
        self.assertEqual(ctx.exception.code, "root-slug-invalid")

    def test_every_roster_repo_yields_a_valid_cycle_key_field(self):
        # D-79: `<root-slug>`는 roster에서 결정론적으로 유도한 소문자 slug
        for name in ("BC_ResNet", "Stream_Diar_Baselines", "SR_CorrNet", "SR_CorrNet_DSC",
                     "SR_CorrNet_SD", "TF_Restormer_release", "TTS_Baselines", "cairn",
                     "home-os", "hearting"):
            root = self.base / name / ".agent_reports"
            root.mkdir(parents=True)
            slug = W._default_root_slug(root)
            self.assertTrue(W.ROOT_SLUG_RE.match(slug), (name, slug))
            self.assertTrue(W.CYCLE_KEY_RE.match(f"legacy:{slug}:plans/2026-04-01_alpha"), (name, slug))

    def test_non_container_root_keeps_its_own_name(self):
        root = self.base / "artifact-root"
        root.mkdir()
        self.assertEqual(W._default_root_slug(root), "artifact-root")

    def test_scan_lumps_uses_the_derived_slug_not_agent_reports(self):
        root = self.base / "hearting" / ".agent_reports"
        root.mkdir(parents=True)
        self.assertEqual(W.scan_lumps(root)["root_slug"], "hearting")
        self.assertEqual(W.scan_lumps(root, root_slug="override")["root_slug"], "override")


class RootSlugCliTests(Fixture):
    def test_lump_index_accepts_an_explicit_root_slug(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = W.main(["--artifact-root", str(self.root), "lump-index", "--root-slug", "hearting"])
        self.assertEqual(rc, W.OK)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["root_slug"], "hearting")
        self.assertTrue(payload["lumps"][0]["cycle_units"][0]["cycle_key"].startswith("legacy:hearting:"))

    def test_lump_index_default_slug_is_the_root_name_for_a_non_container_root(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            W.main(["--artifact-root", str(self.root), "lump-index"])
        self.assertEqual(json.loads(out.getvalue())["root_slug"], self.root_slug)

    def test_r1_root_slug_flag_is_a_fallback_the_proposal_overrides(self):
        # the proposal declares `artifact-root`; the flag must not silently re-slug
        # a package whose campaign keys were sealed against the declared slug
        path = self.write_proposal(self.build_proposal())
        result = W.resplit_legacy_cycle(self.root, gate="r1", lump_cycle_id=self.lump_cycle_id,
                                        proposal=path, dry_run=True, root_slug="hearting")
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["root_slug"], self.root_slug)


class CanonicalDigestTests(Fixture):
    def test_inventory_digests_carry_exactly_one_sha256_prefix(self):
        self.r1()
        for inv in (W.lump_index(self.root), W.sealed_loose_inventory(self.root),
                    W.sealed_retire_inventory(self.root)):
            self.assertIsNotNone(inv)
            self.assertTrue(inv["digest"].startswith("sha256:"), inv["kind"])
            self.assertNotIn("sha256:sha256:", inv["digest"])
            self.assertEqual(len(inv["digest"].split(":")[1]), 64)
        marker = P._read_json(W._marker_path(W._find_run_dir(self.root, self.lump_cycle_id)))
        self.assertNotIn("sha256:sha256:", marker["bundle_digest"])

    def test_a_legacy_double_prefixed_seal_is_not_trusted_and_r1_reseals_it(self):
        # The double-prefix fix changes every W7G inventory digest. No root has an
        # admitted resplit run yet, so the only compatibility question is what
        # happens if one did: it is refused, never silently accepted, and a fresh
        # R1 re-seal restores a fully usable admission bundle.
        fixed_digest, fixed_bundle = W._canonical_digest, W._bundle_digest

        def legacy(body):
            return "sha256:" + fixed_digest(body)

        def legacy_bundle(admission_dir):
            return "sha256:" + fixed_bundle(admission_dir)

        with mock.patch.object(W, "_canonical_digest", side_effect=legacy), \
                mock.patch.object(W, "_bundle_digest", side_effect=legacy_bundle):
            self.r1()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        sealed = P._read_json(W._admission_dir(run_dir) / "lump-inventory.json")
        self.assertIn("sha256:sha256:", sealed["digest"])
        self.assertIsNone(W._valid_admitted_run(self.root, run_dir))
        self.assertIsNone(W.sealed_loose_inventory(self.root))
        self.assertIsNone(W.sealed_retire_inventory(self.root))
        # a stale seal is not mistaken for the index -- `lump_index` falls back to a
        # fresh, single-prefixed scan
        self.assertNotIn("sha256:sha256:", W.lump_index(self.root)["digest"])
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError) as ctx:
            self.r2(route_file)
        self.assertEqual(ctx.exception.code, "admission-marker-missing")
        # R1 re-seals: same proposal, new run, everything trusted again
        shutil.rmtree(run_dir)
        self.r1()
        self.assertIsNotNone(W._valid_admitted_run(self.root, W._find_run_dir(self.root, self.lump_cycle_id)))
        self.assertNotIn("sha256:sha256:", W.sealed_loose_inventory(self.root)["digest"])
        route, route_file = self.route()
        self.assertEqual(self.r2(route_file)["status"], "complete")


class DryRunNoTraceTests(Fixture):
    """A dry run answers "what would happen" and writes nothing -- not the canonical
    tree, not `.runtime`, not the root-wide resplit lock."""

    def _snapshot(self):
        out = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                out[str(path.relative_to(self.root))] = "symlink"
            elif path.is_file():
                out[str(path.relative_to(self.root))] = C._sha(path)
            elif path.is_dir():
                out[str(path.relative_to(self.root))] = "dir"
        return out

    def test_r1_dry_run_leaves_no_trace(self):
        before = self._snapshot()
        result = self.r1(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["loose_deferred"], [])
        self.assertEqual(self._snapshot(), before)
        self.assertIsNone(W.resplit_hold(self.root))

    def test_r2_dry_run_leaves_no_trace_and_reports_the_plan(self):
        self.r1()
        route, route_file = self.route()
        before = self._snapshot()
        result = self.r2(route_file, dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertFalse(result["resumes_existing_journal"])
        self.assertEqual(len(result["cycles"]), 4)
        alpha = next(c for c in result["cycles"] if c["depth1_name"] == "2026-04-01_alpha")
        self.assertEqual(alpha["loose_file_count"], 2)
        self.assertEqual(sorted(alpha["loose_locators"]),
                         ["_internal/notes/n1.md", "_internal/reports/r1.md"])
        self.assertEqual(self._snapshot(), before)
        # no campaign record, no cycle record, no journal, no lock, no hold
        self.assertIsNone(W.resplit_hold(self.root))
        self.assertFalse(W._resplit_lock_path(self.root).exists())
        self.assertFalse((W._find_run_dir(self.root, self.lump_cycle_id) / "journal-r2.json").exists())

    def test_r2_dry_run_reports_deferrals_without_creating_anything(self):
        proposal = self.write_proposal(self.build_proposal(unassigned_loose=True))
        self.r1(proposal_path=proposal)
        route, route_file = self.route()
        before = self._snapshot()
        result = self.r2(route_file, dry_run=True)
        self.assertEqual([c["slug"] for c in result["deferred_campaigns"]], [W.UNASSIGNED_SLUG])
        self.assertEqual([d["source_locator"] for d in result["loose_deferred"]], ["reports/r1.md"])
        self.assertNotIn(W.UNASSIGNED_SLUG, [c["key"].split(":")[-1] for c in result["campaigns"]])
        self.assertEqual(self._snapshot(), before)

    def test_r3_dry_run_leaves_no_trace(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        before = self._snapshot()
        result = self.r3(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(len(result["new_cycle_ids"]), 4)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((W._find_run_dir(self.root, self.lump_cycle_id) / "journal-r3.json").exists())
        self.assertIsNone(W.resplit_hold(self.root))

    def test_r2_dry_run_after_a_real_run_reports_that_run(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:before-finalize")
        before = self._snapshot()
        result = self.r2(route_file, dry_run=True)
        self.assertTrue(result["resumes_existing_journal"])
        self.assertEqual(result["journal"]["phase"], "witnessed")
        self.assertEqual(self._snapshot(), before)


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

    def test_fp4b_mid_cycle_file_rename_rolls_forward_no_half_moved_set(self):
        # 🟡1: FP-4's plan wording ("2번째 사이클 rename 진행 중, 절반 이동") describes
        # file-level granularity, not the whole-cycle boundary the FP-4 test above
        # exercises. This crashes strictly between two files of the same cycle's
        # rename loop, so the crash-time state genuinely has some (not all, not
        # zero) of that cycle's files renamed.
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError) as ctx:
            self.r2(route_file, crash_at="r2:mid-cycle-file")
        self.assertEqual(ctx.exception.code, "crash-fixture")
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        journal = json.loads((run_dir / "journal-r2.json").read_text())
        self.assertEqual(journal["phase"], "committed")
        target_cyc = next(c for c in journal["cycles"] if len(c["files"]) > 1 and c is not journal["cycles"][0])
        moved = [f for f in target_cyc["files"]
                if (Path(target_cyc["cycle_dir"]) / "artifacts" / f["locator"]).is_file()]
        self.assertTrue(0 < len(moved) < len(target_cyc["files"]), "expected a genuinely half-moved file set")
        self.assertFalse((Path(target_cyc["cycle_dir"]) / "manifest.json").exists())
        # resume -> roll-forward completes the batch; no half-moved set survives
        result = self.r2(route_file)
        self.assertEqual(result["journal"]["phase"], "complete")
        for cyc in journal["cycles"]:
            manifest = Path(cyc["cycle_dir"]) / "manifest.json"
            self.assertTrue(manifest.is_file(), cyc["cycle_key"])
            for f in cyc["files"]:
                self.assertTrue((Path(cyc["cycle_dir"]) / "artifacts" / f["locator"]).is_file())
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

    def test_backup_archive_tamper_after_seal_is_typed_failure_zero_unlinks(self):
        # 🔴1 (R3 half): the re-read must recompute the archive digest and
        # compare per-entry path/size/content-digest against the sealed
        # inventory, not just check tar member names. Simulates the archive
        # changing between the seal write and the re-read (the exact window
        # `backup-seal.json` -> re-read covers) by making the second
        # `C._sha(archive)` call (the re-read) disagree with the first (the
        # one recorded in the seal).
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        record = P.read_cycle_record(self.root, self.lump_cycle_id)
        lump_artifacts_dir = P.cycle_dir(self.root, record["campaign_id"], self.lump_cycle_id) / "artifacts"
        before = sorted(str(p) for p in W.P._walk_files(lump_artifacts_dir) if p.is_file())
        self.assertTrue(before)
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        backup_root = self.backup_root()
        identity = P.artifact_lifecycle.read_root_identity(self.root)
        archive_path = W._r3_backup_dir(self.root, run_dir, backup_root, identity) / "legacy-artifacts.tar"
        real_sha = C._sha
        calls = {"n": 0}

        def tamper_reread(path):
            if Path(path) == archive_path:
                calls["n"] += 1
                if calls["n"] == 2:  # the re-read call
                    return "0" * 64
            return real_sha(path)

        with mock.patch.object(W.C, "_sha", side_effect=tamper_reread):
            with self.assertRaises(W.ResplitError) as ctx:
                self.r3(backup_root=backup_root)
        self.assertEqual(ctx.exception.code, "backup-incomplete")
        self.assertEqual(calls["n"], 2)
        self.assertTrue((run_dir / "backup-seal.json").is_file())
        self.assertFalse((run_dir / "backup-verified.json").exists())
        after = sorted(str(p) for p in W.P._walk_files(lump_artifacts_dir) if p.is_file())
        self.assertEqual(before, after, "zero unlinks after a tampered re-read")

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


class ResplitLockTests(Fixture):
    """🔴2: D-77-a root-wide resplit lock, acquired atomically at R2 start and
    held through R3 terminal, distinct from the read-only `resplit_hold`
    journal predicate."""

    def test_concurrent_run_only_one_mutates_other_gets_typed_hold(self):
        self.r1()
        route, route_file = self.route()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        # Simulate a second, concurrent resplit attempt (a different lump/run)
        # that has already claimed the root-wide lock.
        other_run_dir = run_dir.parent / "other-resplit-run"
        other_run_dir.mkdir(parents=True)
        W._acquire_resplit_lock(self.root, lump_cycle_id="cyc_other", run_dir=other_run_dir)
        before = sorted(str(p) for p in self.root.rglob("*"))
        with self.assertRaises(W.ResplitError) as ctx:
            self.r2(route_file)
        self.assertEqual(ctx.exception.code, "resplit-in-progress")
        after = sorted(str(p) for p in self.root.rglob("*"))
        self.assertEqual(before, after, "the blocked run must not mutate canonical state")
        # The other run finishes (its journal reaches terminal) -> the lock is
        # reclaimable and the previously-blocked run proceeds and mutates.
        (other_run_dir / "journal-r2.json").write_text(json.dumps({"phase": "rolled-back"}))
        result = self.r2(route_file)
        self.assertIn(result["journal"]["phase"], ("witnessed", "committed", "complete"))

    def test_lock_reclaimed_when_holder_journal_is_terminal(self):
        self.r1()
        route, route_file = self.route()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        other_run_dir = run_dir.parent / "other-terminal-run"
        other_run_dir.mkdir(parents=True)
        (other_run_dir / "journal-r2.json").write_text(json.dumps({"phase": "complete"}))
        (other_run_dir / "journal-r3.json").write_text(json.dumps({"phase": "complete"}))
        W._acquire_resplit_lock(self.root, lump_cycle_id="cyc_other", run_dir=other_run_dir)
        # the holder's own journals are both fully terminal -> a stale lock,
        # not a valid hold, so a fresh acquire reclaims it rather than blocking.
        W._acquire_resplit_lock(self.root, lump_cycle_id=self.lump_cycle_id, run_dir=run_dir)
        lock = json.loads(W._resplit_lock_path(self.root).read_text())
        self.assertEqual(lock["run_dir"], str(run_dir))

    def test_lock_released_only_after_r3_complete(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        self.assertTrue(W._resplit_lock_path(self.root).is_file(), "lock held across R2 -> R3")
        self.r3()
        self.assertFalse(W._resplit_lock_path(self.root).is_file(), "lock released once R3 completes")

    def test_lock_survives_a_synthetic_crash(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_at="r2:before-finalize")
        self.assertTrue(W._resplit_lock_path(self.root).is_file(), "a crash must not release the lock")


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


HEARTING_CANARY_ROOT = Path("/home/nas/user/Uihyeop/personal/hearting/.agent_reports")
HEARTING_CANARY_LUMP = "cyc_7d129f5a4b4059fdc8ff3005333e1668"


class StartedOnTests(Fixture):
    """(a) D-79: the cycle's own date, and the display-quality row that corrects it."""

    def cycles(self):
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        journal = json.loads((run_dir / "journal-r2.json").read_text())
        return {c["cycle_key"]: c for c in journal["cycles"]}

    def test_started_on_is_the_work_date_not_the_resplit_run_clock(self):
        self.run_full()
        expected = {
            f"legacy:{self.root_slug}:plans/2026-04-01_alpha": "2026-04-01T00:00:00Z",
            f"legacy:{self.root_slug}:plans/2026-04-02_beta": "2026-04-02T00:00:00Z",
            f"legacy:{self.root_slug}:experiments/2026-04-03_exp": "2026-04-03T00:00:00Z",
            f"legacy:{self.root_slug}:research/topic-x": "2026-03-15T00:00:00Z",
        }
        cycles = self.cycles()
        self.assertEqual(set(cycles), set(expected))
        for key, want in expected.items():
            cyc = cycles[key]
            record = P.read_cycle_record(self.root, cyc["cycle_id"])
            manifest = json.loads((Path(cyc["cycle_dir"]) / "manifest.json").read_text())
            self.assertEqual(record["started_on"], want, key)
            self.assertEqual(manifest["cycle"]["started_on"], want, key)
            # the run's own clock stays, under a name that says what it is
            self.assertNotIn("resplit_started_on", record)
            self.assertTrue(record["resplit_run_at"].endswith("Z"))
            self.assertNotEqual(record["resplit_run_at"], record["started_on"])

    def test_started_on_source_records_which_priority_decided(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        cycles = self.cycles()
        self.assertEqual(
            cycles[f"legacy:{self.root_slug}:plans/2026-04-01_alpha"]["started_on_source"],
            "directory-date-prefix")
        self.assertEqual(
            cycles[f"legacy:{self.root_slug}:research/topic-x"]["started_on_source"],
            "entry-document-date")

    def display_quality_proposal(self, *, value="2026-02-01", target=None):
        proposal = self.build_proposal()
        target = target or f"legacy:{self.root_slug}:research/topic-x"
        proposal["proposals"].append({
            "proposal_id": "prop-dq", "fingerprint": "fp-prop-dq", "lane": "display-quality",
            "target_ids": [target], "cited_evidence_ids": ["research/topic-x/report.md"],
            "source_cutoff": proposal["source_cutoff"], "producer_version": "v1",
            "projection_version": "v1", "policy_version": "v1",
            "proposed_value": {"started_on": value}, "confidence": 0.8,
            "rationale": "the entry document's date is the review date, not the start date",
        })
        return proposal

    def test_display_quality_started_on_is_applied_at_r1_and_used_by_r2(self):
        self.r1(self.write_proposal(self.display_quality_proposal()))
        route, route_file = self.route()
        self.r2(route_file)
        cyc = self.cycles()[f"legacy:{self.root_slug}:research/topic-x"]
        record = P.read_cycle_record(self.root, cyc["cycle_id"])
        manifest = json.loads((Path(cyc["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(record["started_on"], "2026-02-01T00:00:00Z")
        self.assertEqual(manifest["cycle"]["started_on"], "2026-02-01T00:00:00Z")
        self.assertEqual(record["started_on_source"], "display-quality-proposal")
        # the sealed inventory keeps the derived value; only the verdict carries the fix
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        inv = json.loads((W._admission_dir(run_dir) / "lump-inventory.json").read_text())
        unit = next(u for u in inv["lumps"][0]["cycle_units"] if u["bucket"] == "research")
        self.assertEqual(unit["started_on"], "2026-03-15T00:00:00Z")

    def test_display_quality_started_on_malformed_value_is_typed_hold(self):
        campaigns_before = sorted(p.name for p in (self.root / "campaigns").iterdir())
        cycles_before = sorted(r["cycle_id"] for r in P.list_cycle_records(self.root))
        result = self.r1(self.write_proposal(self.display_quality_proposal(value="last April")))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["code"], "display-quality-invalid")
        # A-16.3: a hold changes the canonical artifact tree not at all
        self.assertEqual(sorted(p.name for p in (self.root / "campaigns").iterdir()), campaigns_before)
        self.assertEqual(sorted(r["cycle_id"] for r in P.list_cycle_records(self.root)), cycles_before)

    def test_display_quality_started_on_unknown_target_is_typed_hold(self):
        proposal = self.display_quality_proposal(target=f"legacy:{self.root_slug}:plans/not-a-unit")
        result = self.r1(self.write_proposal(proposal))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["code"], "display-quality-invalid")
        self.assertTrue(result["detail"].startswith("unknown-target:"))

    def test_display_quality_started_on_conflicting_rows_are_typed_hold(self):
        proposal = self.display_quality_proposal(value="2026-02-01")
        conflict = dict(proposal["proposals"][-1])
        conflict["proposal_id"] = "prop-dq2"
        conflict["proposed_value"] = {"started_on": "2026-02-02"}
        proposal["proposals"].append(conflict)
        result = self.r1(self.write_proposal(proposal))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["code"], "display-quality-invalid")
        self.assertTrue(result["detail"].startswith("conflict:"))

    def test_dry_run_preview_reports_the_derived_date_capability_and_priority(self):
        self.r1(self.write_proposal(self.display_quality_proposal()))
        route, route_file = self.route()
        preview = self.r2(route_file, dry_run=True)
        by_key = {c["cycle_key"]: c for c in preview["cycles"]}
        research = by_key[f"legacy:{self.root_slug}:research/topic-x"]
        self.assertEqual(research["started_on"], "2026-02-01T00:00:00Z")
        self.assertEqual(research["started_on_source"], "display-quality-proposal")
        self.assertEqual(research["capability"], "autopilot-research")
        self.assertEqual(research["intensity"], W.RESPLIT_CYCLE_INTENSITY)
        alpha = by_key[f"legacy:{self.root_slug}:plans/2026-04-01_alpha"]
        self.assertEqual(alpha["started_on_source"], "directory-date-prefix")
        self.assertEqual(alpha["capability"], "autopilot-code")


class PerCycleRouteClosureTests(Fixture):
    """(b) D-10/D-77-b: the retro seal reports `completed`, on a ledger-admitted route."""

    def cycles(self):
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        return json.loads((run_dir / "journal-r2.json").read_text())["cycles"]

    def test_new_cycles_seal_as_completed(self):
        self.run_full()
        for cyc in self.cycles():
            manifest = json.loads((Path(cyc["cycle_dir"]) / "manifest.json").read_text())
            self.assertEqual(manifest["cycle"]["state"], "completed", cyc["cycle_key"])
            terminal = [e for e in manifest["events"] if e["event_type"] == "route.terminal.recorded"]
            self.assertEqual(len(terminal), 1, cyc["cycle_key"])

    def test_per_cycle_route_is_admitted_to_the_canonical_ledger_and_closed(self):
        self.run_full()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        for cyc in self.cycles():
            record = P.read_cycle_record(self.root, cyc["cycle_id"])
            route_file = Path(record["route_file"])
            self.assertEqual(route_file, L.canonical_route_path(self.root, record["route_id"]))
            self.assertTrue(route_file.is_file())
            self.assertTrue(L.canonical_outcome_path(self.root, record["route_id"]).is_file())
            route = json.loads(route_file.read_text())
            self.assertEqual(route["resplit_cycle_key"], cyc["cycle_key"])
            self.assertTrue(P.route_is_closed(self.root, route))
            # the run_dir copy survives as an execution log, not as identity
            self.assertTrue((run_dir / "routes" / f"{record['route_id']}.json").is_file())

    def test_route_id_is_not_derived_from_the_cycle_key_or_caller_route(self):
        self.run_full()
        for cyc in self.cycles():
            record = P.read_cycle_record(self.root, cyc["cycle_id"])
            seeded = "rt-" + __import__("hashlib").sha256(
                f"{json.loads(Path(record['route_file']).read_text())['route_id']}:{cyc['cycle_key']}"
                .encode()).hexdigest()[:16]
            self.assertNotEqual(record["route_id"], seeded)
            self.assertTrue(W.P.artifact_lifecycle._ROUTE_ID_RE.fullmatch(record["route_id"]))

    def test_rerun_reuses_the_route_the_cycle_key_already_maps_to(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        first = {c["cycle_key"]: P.read_cycle_record(self.root, c["cycle_id"])["route_id"]
                 for c in self.cycles()}
        again = self.r2(route_file)
        self.assertEqual(again["status"], "already-applied")
        second = {c["cycle_key"]: P.read_cycle_record(self.root, c["cycle_id"])["route_id"]
                  for c in self.cycles()}
        self.assertEqual(first, second)
        for cycle_key, route_id in first.items():
            found = W._find_ledger_route_by_cycle_key(self.root, cycle_key)
            self.assertEqual(found["route_id"], route_id)

    def test_rollback_releases_every_route_it_admitted(self):
        self.r1()
        route, route_file = self.route()
        with self.assertRaises(W.ResplitError):
            self.r2(route_file, crash_after_phase="prepared")
        admitted = [json.loads(p.read_text()) for p in
                    sorted((self.root / ".runtime" / "routes").glob("*.json"))
                    if not p.name.endswith(".outcome.json")]
        self.assertTrue([r for r in admitted if "resplit_cycle_key" in r])
        self.r2(route_file)  # the retry rolls the prepared batch back
        left = [json.loads(p.read_text()) for p in
                sorted((self.root / ".runtime" / "routes").glob("*.json"))
                if not p.name.endswith(".outcome.json")]
        self.assertEqual([r for r in left if "resplit_cycle_key" in r], [])


class BucketCapabilityTests(Fixture):
    """(c) D-7/D-79: capability follows the bucket, not the route that ran the resplit."""

    def test_capability_and_route_capability_come_from_the_bucket(self):
        self.run_full()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        cycles = json.loads((run_dir / "journal-r2.json").read_text())["cycles"]
        want = {
            f"legacy:{self.root_slug}:plans/2026-04-01_alpha": "autopilot-code",
            f"legacy:{self.root_slug}:plans/2026-04-02_beta": "autopilot-code",
            f"legacy:{self.root_slug}:experiments/2026-04-03_exp": "autopilot-lab",
            f"legacy:{self.root_slug}:research/topic-x": "autopilot-research",
        }
        for cyc in cycles:
            record = P.read_cycle_record(self.root, cyc["cycle_id"])
            expected = want[cyc["cycle_key"]]
            self.assertEqual(record["capability"], expected, cyc["cycle_key"])
            self.assertEqual(record["route_capability"], expected, cyc["cycle_key"])
            self.assertEqual(record["intensity"], W.RESPLIT_CYCLE_INTENSITY)
            manifest = json.loads((Path(cyc["cycle_dir"]) / "manifest.json").read_text())
            self.assertEqual({a["capability"] for a in manifest["artifacts"]}, {expected})

    def test_caller_route_capability_is_not_inherited(self):
        self.run_full()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        cycles = json.loads((run_dir / "journal-r2.json").read_text())["cycles"]
        research = next(c for c in cycles if c["bucket"] == "research")
        record = P.read_cycle_record(self.root, research["cycle_id"])
        # the caller route that ran R2 is `autopilot-code`; the research cycle is not
        self.assertEqual(record["capability"], "autopilot-research")
        self.assertEqual(W.BUCKET_CAPABILITY["research"], "autopilot-research")


class CompatSupersessionTests(Fixture):
    """(d) D-82/A-16.4: an overridden map entry is marked, and its file is untouched."""

    def test_superseded_by_and_at_are_written_on_the_overridden_map(self):
        state_before = C.load_map_state(self.root)
        old_paths = [e["path"] for e in state_before["maps"]]
        old_bytes = {p: Path(p).read_bytes() for p in old_paths}
        self.run_full()
        compat = json.loads(C.compat_path(self.root).read_text())
        self.assertEqual(len(compat["maps"]), len(old_paths) + 1)
        new_row = compat["maps"][-1]
        for row in compat["maps"][:-1]:
            self.assertEqual(row["superseded_by"], new_row["sha256"], row["path"])
            self.assertTrue(row["superseded_at"].endswith("Z"))
            # the old map file's bytes and its recorded digest are unchanged
            self.assertEqual(Path(row["path"]).read_bytes(), old_bytes[row["path"]])
            self.assertEqual(row["sha256"], next(
                e["sha256"] for e in state_before["maps"] if e["path"] == row["path"]))
        self.assertNotIn("superseded_by", new_row)

    def test_load_map_state_exposes_the_supersession(self):
        self.run_full()
        state = C.load_map_state(self.root)
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["drifted"], [])
        self.assertTrue(all(e["superseded_by"] for e in state["maps"][:-1]))
        self.assertIsNone(state["maps"][-1]["superseded_by"])


class BackupRootTests(Fixture):
    """(e) D-84: R3's tar lives outside the artifact root, run_dir keeps the pointer."""

    def backup_dir(self):
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        identity = P.artifact_lifecycle.read_root_identity(self.root)
        return W._r3_backup_dir(self.root, run_dir, self.backup_root(), identity)

    def test_archive_lands_under_backup_root_and_run_dir_keeps_only_the_pointer(self):
        self.run_full()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        backup_dir = self.backup_dir()
        archive = backup_dir / "legacy-artifacts.tar"
        self.assertTrue(archive.is_file())
        self.assertEqual(backup_dir.parent.name, ROOT_ID)
        self.assertFalse((run_dir / "legacy-artifacts.tar").exists())
        location = json.loads((run_dir / "backup-location.json").read_text())
        self.assertEqual(location["archive"], str(archive))
        self.assertEqual(location["archive_sha256"], "sha256:" + C._sha(archive))
        # the seal copy in the run_dir is byte-identical to the sealed one
        self.assertEqual((run_dir / "backup-seal.json").read_bytes(),
                         (backup_dir / "backup-seal.json").read_bytes())
        self.assertFalse(W._is_within(self.root, str(archive)))

    def test_backup_root_inside_the_artifact_root_is_refused(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        inside = self.root / "_scratch" / "backups"
        inside.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(W.ResplitError) as ctx:
            self.r3(backup_root=inside)
        self.assertEqual(ctx.exception.code, "backup-root-inside-artifact-root")
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        self.assertFalse((run_dir / "journal-r3.json").exists())

    def test_backup_root_is_required(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        with self.assertRaises(W.ResplitError) as ctx:
            W.resplit_legacy_cycle(self.root, gate="r3", lump_cycle_id=self.lump_cycle_id)
        self.assertEqual(ctx.exception.code, "backup-root-required")

    def test_dry_run_needs_no_backup_root_and_writes_nothing(self):
        self.r1()
        route, route_file = self.route()
        self.r2(route_file)
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        before = sorted(p.name for p in run_dir.iterdir())
        result = W.resplit_legacy_cycle(self.root, gate="r3", lump_cycle_id=self.lump_cycle_id,
                                        dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()), before)


class DeviationAuditTests(Fixture):
    """The read-only regression surface: what a finished run actually produced."""

    def test_a_fixed_run_reports_zero_deviations(self):
        self.run_full()
        report = W.resplit_deviations(self.root, lump_cycle_id=self.lump_cycle_id)
        self.assertEqual(report["deviations"], [], report["checks"])
        self.assertEqual(report["status"], "ok")
        self.assertEqual({c["status"] for c in report["checks"]}, {"ok"})

    def test_audit_is_read_only(self):
        self.run_full()
        run_dir = W._find_run_dir(self.root, self.lump_cycle_id)
        before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in P._walk_files(run_dir) if p.is_file()}
        W.resplit_deviations(self.root, lump_cycle_id=self.lump_cycle_id)
        after = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                 for p in P._walk_files(run_dir) if p.is_file()}
        self.assertEqual(before, after)

    def test_no_run_is_reported_as_no_run_not_as_clean(self):
        report = W.resplit_deviations(self.root)
        self.assertEqual(report["status"], "no-run")
        self.assertEqual(report["checks"], [])


@unittest.skipUnless(
    (HEARTING_CANARY_ROOT / ".runtime" / "artifact-producer" / "v1" / "migrations").is_dir(),
    "hearting canary artifact root not present on this machine")
class HeartingCanaryRegressionTests(unittest.TestCase):
    """The 2026-09-04 canary is a frozen read-only regression subject.

    Its 16 cycles were sealed by the pre-hotfix code and stay exactly as sealed
    (D-6/D-11 forbid rewriting them; `canary-deviations.md` in the run_dir is the
    authorized record). What this asserts is that the audit still *sees* all five
    deviations there -- if a later change made the audit report that run clean, the
    audit stopped measuring the thing it exists to measure.
    """

    def test_canary_run_still_reports_all_five_recorded_deviations(self):
        report = W.resplit_deviations(HEARTING_CANARY_ROOT, lump_cycle_id=HEARTING_CANARY_LUMP)
        self.assertEqual(sorted(report["deviations"]), sorted(W.DEVIATION_CHECKS))
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(len(by_id["cycle-state"]["rows"]), 16)
        self.assertEqual(len(by_id["started-on"]["rows"]), 16)
        self.assertTrue(all(r["observed"] == "active" for r in by_id["cycle-state"]["rows"]))
        self.assertTrue(all(r["observed"] == "autopilot-code" and r["expected"] != "autopilot-code"
                            for r in by_id["capability"]["rows"]))

    def test_auditing_the_canary_mutates_nothing(self):
        run_dir = HEARTING_CANARY_ROOT / ".runtime" / "artifact-producer" / "v1" / "migrations" / (
            "20260903T151118Z-resplit-" + HEARTING_CANARY_LUMP)
        before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in P._walk_files(run_dir) if p.is_file()}
        W.resplit_deviations(HEARTING_CANARY_ROOT, lump_cycle_id=HEARTING_CANARY_LUMP)
        after = {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                 for p in P._walk_files(run_dir) if p.is_file()}
        self.assertEqual(before, after)


class OwnershipGuardTests(unittest.TestCase):
    """S2-h: this slice only imports artifact_producer/artifact_cutover."""

    def test_required_upstream_api_exists(self):
        for name in ("compat_append", "load_map_state", "retire_approval_package", "_tree_digest",
                    "_write_jsonl", "_read_jsonl", "migrations_dir"):
            self.assertTrue(hasattr(C, name), name)
        for name in ("validate_shared_reference_pins", "mark_cycle_superseded", "mark_campaign_superseded",
                    "set_campaign_related", "_write_exclusive", "finalize", "list_cycle_records"):
            self.assertTrue(hasattr(P, name), name)


if __name__ == "__main__":
    unittest.main()
