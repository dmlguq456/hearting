#!/usr/bin/env python3
"""W7C producer lifecycle tests for `artifact_producer.py`.

Every fixture uses an isolated temporary artifact root and `AGENT_HOME`; the
real canonical root, registry, and routes directory are never touched.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_identity as idm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_manifest as m  # noqa: E402
import artifact_producer as P  # noqa: E402

_P = Path(__file__).with_name("capability-route.py")
_S = importlib.util.spec_from_file_location("route_for_producer_test", _P)
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)

ALL = [
    "atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
    "no-artifact-handoff", "no-independent-verifier", "focused-verification",
]
REPO_ID = "repo_" + "a" * 32
ROOT_ID = "root_" + "b" * 32


def gate_evidence():
    return {
        "spec_read": {"satisfied": True, "source": "fixture"},
        "drift_verdict": "within-spec", "workflow_mode": "tracked",
        "artifact_guard": {"satisfied": True, "source": "fixture"},
    }


def registered_headless():
    return {"candidates": [{
        "harness": "codex", "transport": "headless", "surface": "registered-headless",
        "status": "supported", "probe_source": "fixture-probe", "probe_time": "2026-07-20T00:00:00Z",
    }]}


def nested(parent="codex", child="codex"):
    sandbox = R.WRAPPER_PARENT_SANDBOXES[parent][0] if parent in R.WRAPPER_PARENT_SANDBOXES else "workspace-write"
    return {
        "parent_harness": parent, "parent_transport": "headless", "parent_sandbox": sandbox,
        "child_harness": child, "launch_authority": "conductor", "status": "supported",
        "probe_source": "fixture-probe", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
        "checked_worktree": str(R.ROOT.resolve()), "failure_scope": "none",
        "codex_command": "ok" if child == "codex" else "not-applicable", "retry_on_isolated_worktree": 0,
    }


def dispatch_evidence():
    return {"tuples": [nested()], "native_subagent": [{
        "harness": "codex", "transport": "headless", "execution_surface": "codex-native-subagent",
        "registered_worker": False, "status": "supported", "check_source": "fixture-native-check",
    }]}


def compile_for(intensity, root, capability="autopilot-code", mode="dev"):
    common = dict(cwd=R.ROOT, artifact_root=root, tracking="tracked", tracked_gate_evidence=gate_evidence())
    if intensity == "direct":
        return R.compile_route(capability, mode, "direct", predicates=ALL, transport=None,
                               inline_reason="atomic-direct", **common)
    if intensity == "quick":
        return R.compile_route(capability, mode, "quick", predicates=[], transport=None,
                               registered_headless_evidence=registered_headless(), **common)
    return R.compile_route(capability, mode, intensity, predicates=[], transport="headless",
                           dispatch_evidence=dispatch_evidence(), **common)


class ProducerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "artifact-root"
        self.root.mkdir(parents=True, exist_ok=True)
        home = Path(self._tmp.name) / "agent-home"
        (home / "core").mkdir(parents=True, exist_ok=True)
        (home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self._env = {k: os.environ.get(k) for k in (
            "AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_ROOT")}
        os.environ["AGENT_HOME"] = str(home)
        for key in ("AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_ROOT"):
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def activate(self):
        return P.activate(self.root, repository_id=REPO_ID, artifact_root_id=ROOT_ID,
                          w7={"campaign_id": "camp_" + "c" * 32})

    def route(self, intensity="direct", capability="autopilot-code", mode="dev"):
        route = compile_for(intensity, self.root, capability, mode)
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def close(self, route, route_file):
        evidence = Path(self._tmp.name) / f"evidence-{route['route_id']}.txt"
        evidence.write_text("terminal evidence\n", encoding="utf-8")
        for node in route["nodes"]:
            if node.get("terminal") is not True:
                continue
            if node.get("dispatch_depth", 0) == 0:
                R.write_completion_marker(route, node, node["id"], evidence)
                continue
            metadata = {
                "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": "1", "fallback_hop": "same-harness-headless",
            }
            R.write_completion_marker(route, node, node["id"], evidence,
                                      attempt_id=f"att-fixture-{node['id']}", attempt_metadata=metadata)
        outcome, _ = R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        self.assertTrue(outcome["terminal_gate_proven"], outcome)

    def begin(self, intensity="direct", capability="autopilot-code", mode="dev", **kw):
        route, route_file = self.route(intensity, capability, mode)
        result = P.begin(self.root, route_file=route_file, capability=capability, intensity=intensity, **kw)
        return route, route_file, result

    def write_output(self, result, rel="plans/cycle/plan.md", data=b"plan body\n"):
        target = Path(result["cycle_dir"]) / "artifacts" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target


class ActivateAndBeginTest(ProducerTestBase):
    def test_begin_before_activation_is_legacy_compat(self):
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "legacy-compat")
        self.assertEqual(result["layout"], "legacy")
        self.assertFalse((self.root / "campaigns").exists())

    def test_begin_require_cycle_fails_closed_when_inactive(self):
        route, route_file = self.route()
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                    require_cycle=True)
        self.assertEqual(ctx.exception.code, "cutover-inactive")

    def test_activate_freezes_identity_and_is_idempotent(self):
        first = self.activate()
        self.assertEqual(first["status"], "activated")
        self.assertEqual(first["identity"], "created")
        identity = L.read_root_identity(self.root)
        self.assertEqual((identity.repository_id, identity.artifact_root_id), (REPO_ID, ROOT_ID))
        again = self.activate()
        self.assertEqual(again["status"], "already-active")
        with self.assertRaises(P.ProducerError) as ctx:
            P.activate(self.root, repository_id=REPO_ID, artifact_root_id="root_" + "f" * 32)
        self.assertEqual(ctx.exception.code, "identity-conflict")

    def test_begin_issues_ids_before_first_write(self):
        self.activate()
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "begun")
        self.assertTrue(idm.is_well_formed(result["campaign_id"], "campaign"))
        self.assertTrue(idm.is_well_formed(result["cycle_id"], "cycle"))
        self.assertTrue(idm.is_well_formed(result["producer_id"], "producer"))
        cycle_dir = Path(result["cycle_dir"])
        self.assertEqual(cycle_dir, self.root / "campaigns" / result["campaign_id"] / "cycles" / result["cycle_id"])
        self.assertTrue((cycle_dir / "artifacts").is_dir())
        self.assertEqual(sorted(os.listdir(cycle_dir)), ["artifacts"])
        self.assertEqual(result["env"]["AGENT_ARTIFACT_OUTPUT_DIR"], str(cycle_dir / "artifacts"))
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(record["state"], "open")
        self.assertEqual(record["route_id"], route["route_id"])
        campaign = P.read_campaign(self.root, result["campaign_id"])
        self.assertEqual(campaign["cycles"], [result["cycle_id"]])

    def test_begin_is_idempotent_per_route(self):
        self.activate()
        route, route_file, first = self.begin()
        second = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(second["status"], "resumed")
        self.assertEqual(second["cycle_id"], first["cycle_id"])

    def test_begin_rejects_capability_and_intensity_mismatch(self):
        self.activate()
        route, route_file = self.route("direct")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-spec", intensity="direct")
        self.assertEqual(ctx.exception.code, "route-capability-mismatch")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="quick")
        self.assertEqual(ctx.exception.code, "route-intensity-mismatch")

    def test_begin_rejects_closed_route(self):
        self.activate()
        route, route_file = self.route()
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "route-already-closed")

    def test_stage_worker_joins_owner_cycle_by_node(self):
        self.activate()
        route, route_file, owner = self.begin("standard")
        node_id = route["nodes"][0]["id"]
        stage_capability = "code-plan"
        stage = P.begin(self.root, route_file=route_file, capability=stage_capability, intensity="standard",
                        node_id=node_id)
        # Same route => same open cycle; the stage worker never issues a second lineage.
        self.assertEqual(stage["status"], "resumed")
        self.assertEqual(stage["cycle_id"], owner["cycle_id"])
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability=stage_capability, intensity="standard",
                    node_id="no-such-node")
        self.assertEqual(ctx.exception.code, "route-node-unknown")

    def test_campaign_key_reuse_and_parent_cycle(self):
        self.activate()
        route, route_file, first = self.begin(campaign_key="w7c-key")
        self.write_output(first)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        route2, route_file2 = self.route("quick")
        second = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="quick",
                         campaign_key="w7c-key", parent_cycle_id=first["cycle_id"])
        self.assertEqual(second["campaign_id"], first["campaign_id"])
        self.assertFalse(second["campaign_created"])
        self.assertEqual(P.read_cycle_record(self.root, second["cycle_id"])["parent_cycle_id"], first["cycle_id"])
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route("direct", mode="debug")[1], capability="autopilot-code",
                    intensity="direct", parent_cycle_id=second["cycle_id"])
        self.assertEqual(ctx.exception.code, "parent-cycle-not-sealed")


class CheckWriteTest(ProducerTestBase):
    def test_legacy_allowed_in_compat_window_and_denied_when_active(self):
        target = self.root / "plans" / "2026-08-25_x" / "plan.md"
        before = P.check_write(self.root, target)
        self.assertEqual((before["verdict"], before["reason"]), ("allow", "legacy-compat-window"))
        self.activate()
        after = P.check_write(self.root, target)
        self.assertEqual((after["verdict"], after["reason"]), ("deny", "legacy-top-level-write-denied"))
        self.assertEqual(after["bucket"], "plans")

    def test_runtime_and_outside_are_never_gated(self):
        self.activate()
        self.assertEqual(P.check_write(self.root, self.root / ".runtime" / "x.json")["verdict"], "allow")
        self.assertEqual(P.check_write(self.root, self.root / "_scratch" / "x")["verdict"], "allow")
        outside = P.check_write(self.root, Path(self._tmp.name) / "elsewhere.md")
        self.assertEqual((outside["verdict"], outside["reason"]), ("allow", "outside-artifact-root"))

    def test_shared_is_immutable_in_both_states(self):
        target = self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md"
        self.assertEqual(P.check_write(self.root, target)["reason"], "shared-revision-immutable")
        self.activate()
        self.assertEqual(P.check_write(self.root, target)["verdict"], "deny")

    def test_cycle_paths(self):
        self.activate()
        route, route_file, result = self.begin()
        camp, cyc = result["campaign_id"], result["cycle_id"]
        base = self.root / "campaigns" / camp
        self.assertEqual(P.check_write(self.root, base / "campaign.json")["reason"], "campaign-record-machine-managed")
        self.assertEqual(P.check_write(self.root, base / "cycles" / cyc / "manifest.json")["reason"], "outside-cycle-artifacts")
        ok = P.check_write(self.root, base / "cycles" / cyc / "artifacts" / "plans" / "plan.md")
        self.assertEqual((ok["verdict"], ok["reason"], ok["bucket"]), ("allow", "open-cycle-artifacts", "plans"))
        unknown = P.check_write(self.root, base / "cycles" / ("cyc_" + "9" * 32) / "artifacts" / "x.md")
        self.assertEqual(unknown["reason"], "cycle-unknown")
        self.assertEqual(P.cycle_bucket(self.root, base / "cycles" / cyc / "artifacts" / "spec" / "prd.md"), ("spec", cyc))

    def test_sealed_cycle_denies_new_writes(self):
        self.activate()
        route, route_file, result = self.begin()
        target = self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        verdict = P.check_write(self.root, target)
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("deny", "cycle-not-open"))

    def test_resolve_output_dir(self):
        self.assertEqual(P.resolve_output_dir(self.root, "spec"), (self.root / "spec", "legacy"))
        self.activate()
        with self.assertRaises(P.ProducerError) as ctx:
            P.resolve_output_dir(self.root, "spec")
        self.assertEqual(ctx.exception.code, "legacy-top-level-write-denied")
        route, route_file, result = self.begin()
        directory, layout = P.resolve_output_dir(self.root, "spec", cycle_dir_hint=result["cycle_dir"])
        self.assertEqual((directory, layout), (Path(result["cycle_dir"]) / "artifacts" / "spec", "cycle"))
        os.environ["AGENT_ARTIFACT_CYCLE_DIR"] = result["cycle_dir"]
        self.assertEqual(P.resolve_output_dir(self.root, "experiments")[1], "cycle")


class FinalizeTest(ProducerTestBase):
    def test_finalize_seals_manifest_index_and_record(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result, "plans/cycle/plan.md")
        self.write_output(result, "plans/cycle/final_report.md", b"report\n")
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["artifact_count"], 2)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(m.validate(document).ok)
        self.assertEqual(document["cycle"]["state"], "completed")
        self.assertEqual(document["cycle"]["cycle_id"], result["cycle_id"])
        self.assertEqual(document["campaign"]["campaign_id"], result["campaign_id"])
        self.assertEqual(document["artifact_root_id"], ROOT_ID)
        roles = {row["title"]: row["role"] for row in document["artifacts"]}
        self.assertEqual(roles["plans/cycle/final_report.md"], "primary")
        self.assertEqual(roles["plans/cycle/plan.md"], "output")
        self.assertNotEqual(document["routes"][0]["terminal_marker"], "pending")
        completion = L.evaluate_cycle_completion(
            document, content_root=Path(result["cycle_dir"]), route_file=route_file, expected_root_id=ROOT_ID)
        self.assertEqual(completion.status, "complete", completion.to_payload())
        index = adm.load_index(self.root)
        self.assertIn(result["cycle_id"], index.manifests)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(record["manifest_digest"], sealed["manifest_digest"])
        self.assertFalse(P.journal_path(self.root, result["cycle_id"]).exists())
        again = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(P.status(self.root)["cycle_counts"], {"sealed": 1})

    def test_finalize_requires_closed_route_unless_allowed(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "route-not-closed")
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"], allow_open_route=True)
        self.assertEqual(sealed["status"], "sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(document["routes"][0]["terminal_marker"], "pending")
        self.assertEqual(document["cycle"]["state"], "active")
        self.assertEqual(sealed["cycle_state"], "active")

    def test_empty_output_leaves_no_lineage(self):
        self.activate()
        route, route_file, result = self.begin()
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "no-lineage")
        self.assertFalse(Path(result["cycle_dir"]).exists())
        self.assertFalse((self.root / "campaigns" / result["campaign_id"]).exists())
        self.assertNotIn(result["cycle_id"], adm.load_index(self.root).manifests)
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "no-lineage")

    def test_abandoned_state_is_recorded(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        outcome, created = R.close_route(
            route, route_file, commit="a" * 40, summary="abandoned fixture"
        )
        self.assertTrue(created)
        self.assertFalse(outcome["terminal_gate_proven"])
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"], state="abandoned")
        self.assertEqual(outcome["cycle_state"], "abandoned")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(document["routes"][0]["terminal_marker"], "pending")
        self.assertEqual(document["routes"][0]["terminal_evidence_id"], "")
        self.assertFalse(any(e["event_type"] == "route.terminal.recorded" for e in document["events"]))

    def test_completed_still_rejects_closed_route_without_terminal_evidence(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        outcome, _ = R.close_route(route, route_file, commit="a" * 40, summary="incomplete fixture")
        self.assertFalse(outcome["terminal_gate_proven"])
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(caught.exception.code, "completion-terminal-marker-unverified")

    def test_abandoned_finalize_can_adopt_an_explicit_legacy_root_output(self):
        self.activate()
        route, route_file, result = self.begin()
        legacy = Path(result["cycle_dir"]) / "owner_brief.md"
        legacy.write_text("owner brief\n", encoding="utf-8")
        R.close_route(route, route_file, commit="a" * 40, summary="abandoned fixture")
        outcome = P.finalize(
            self.root,
            cycle_id=result["cycle_id"],
            state="abandoned",
            adopt_root_outputs=["owner_brief.md"],
        )
        self.assertEqual(outcome["adopted_root_outputs"], ["owner_brief.md"])
        self.assertFalse(legacy.exists())
        self.assertTrue((Path(result["cycle_dir"]) / "artifacts" / "owner_brief.md").is_file())

    def test_root_output_adoption_validates_all_sources_before_moving(self):
        self.activate()
        route, route_file, result = self.begin()
        first = Path(result["cycle_dir"]) / "first.md"
        first.write_text("first\n", encoding="utf-8")
        R.close_route(route, route_file, commit="a" * 40, summary="abandoned fixture")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(
                self.root,
                cycle_id=result["cycle_id"],
                state="abandoned",
                adopt_root_outputs=["first.md", "missing.md"],
            )
        self.assertEqual(caught.exception.code, "root-output-adoption-source-invalid")
        self.assertTrue(first.is_file())
        self.assertFalse((Path(result["cycle_dir"]) / "artifacts" / "first.md").exists())

    def test_finalize_rejects_symlink_and_out_of_artifacts_files(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        (Path(result["cycle_dir"]) / "stray.md").write_text("x", encoding="utf-8")
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "output-invalid")
        self.assertIn("file-outside-artifacts:stray.md", ctx.exception.detail)

    def test_every_intensity_shares_one_lifecycle(self):
        self.activate()
        for intensity in ("direct", "quick", "standard"):
            with self.subTest(intensity=intensity):
                route, route_file, result = self.begin(intensity)
                self.assertEqual(result["status"], "begun")
                self.write_output(result, f"plans/{intensity}/plan.md")
                self.close(route, route_file)
                sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
                self.assertEqual(sealed["status"], "sealed")


class RecoveryTest(ProducerTestBase):
    def _sealing_crash(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=result["cycle_id"], crash_after_manifest=True)
        return result

    def test_crash_after_manifest_rolls_forward(self):
        result = self._sealing_crash()
        self.assertTrue((Path(result["cycle_dir"]) / "manifest.json").is_file())
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")
        self.assertEqual(P.status(self.root)["pending_journals"], [result["cycle_id"]])
        recovered = P.recover(self.root)
        self.assertEqual(recovered["producer"]["rolled_forward"], [result["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "sealed")
        self.assertIn(result["cycle_id"], adm.load_index(self.root).manifests)
        self.assertEqual(P.status(self.root)["pending_journals"], [])

    def test_crash_before_manifest_rolls_back_and_cycle_stays_open(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        P._write_journal(self.root, result["cycle_id"], state="sealing", manifest_digest="sha256:" + "0" * 64,
                         cycle_path=os.path.relpath(result["cycle_dir"], self.root))
        recovered = P.recover(self.root)
        self.assertEqual(recovered["producer"]["rolled_back"], [result["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")
        self.assertEqual(P.check_write(self.root, Path(result["cycle_dir"]) / "artifacts" / "a.md")["verdict"], "allow")

    def test_missing_cycle_dir_is_dropped(self):
        self.activate()
        route, route_file, result = self.begin()
        import shutil
        shutil.rmtree(result["cycle_dir"])
        recovered = P.recover(self.root)
        self.assertEqual(recovered["producer"]["dropped"], [result["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "dropped")

    def test_finalize_runs_recovery_first(self):
        result = self._sealing_crash()
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "already-sealed")


class SharedAdmissionTest(ProducerTestBase):
    def _sealed_cycle(self, capability="autopilot-spec", mode="update", extra=()):
        self.activate()
        route, route_file, result = self.begin("direct", capability, mode)
        self.write_output(result, "spec/prd.md", b"# PRD\n")
        for rel, data in extra:
            self.write_output(result, rel, data)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def test_admit_spec_creates_immutable_revision(self):
        result = self._sealed_cycle()
        admitted = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="prd")
        self.assertEqual(admitted["status"], "admitted")
        self.assertTrue(admitted["reference_created"])
        revision_dir = Path(admitted["revision_dir"])
        self.assertTrue((revision_dir / "prd.md").is_file())
        revision = json.loads((revision_dir / "revision.json").read_text())
        self.assertEqual(revision["source"]["cycle_id"], result["cycle_id"])
        self.assertEqual(revision["sequence"], 1)
        reference = json.loads((revision_dir.parent.parent / "reference.json").read_text())
        self.assertEqual(reference["latest_revision_id"], admitted["shared_reference_revision_id"])
        self.assertEqual(P.check_write(self.root, revision_dir / "prd.md")["reason"], "shared-revision-immutable")
        # Second admission under the same key appends a revision; never rewrites.
        second = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="prd")
        self.assertFalse(second["reference_created"])
        self.assertEqual(second["shared_reference_id"], admitted["shared_reference_id"])
        self.assertNotEqual(second["shared_reference_revision_id"], admitted["shared_reference_revision_id"])
        self.assertEqual(json.loads((Path(second["revision_dir"]) / "revision.json").read_text())["sequence"], 2)

    def test_admit_requires_sealed_cycle(self):
        self.activate()
        route, route_file, result = self.begin("direct", "autopilot-spec", "update")
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec")
        self.assertEqual(ctx.exception.code, "cycle-not-sealed")

    def test_research_requires_explicit_promotion(self):
        result = self._sealed_cycle("autopilot-research", "academic",
                                    extra=[("research/topic/report.md", b"r\n"),
                                           ("research/topic/promotion.md", b"approved\n")])
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="research", source="research/topic")
        self.assertEqual(ctx.exception.code, "research-promotion-required")
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="research", source="research/topic",
                           promote_research=True)
        self.assertEqual(ctx.exception.code, "research-promotion-evidence-required")
        admitted = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="research", source="research/topic",
                                  promote_research=True, promotion_evidence="research/topic/promotion.md")
        self.assertEqual(admitted["promotion"]["kind"], "explicit")
        self.assertTrue(admitted["promotion"]["evidence_digest"].startswith("sha256:"))

    def test_only_declared_kinds_are_admissible(self):
        result = self._sealed_cycle()
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="plans", source="spec")
        self.assertEqual(ctx.exception.code, "shared-kind-not-admissible")


class CliTest(ProducerTestBase):
    def run_cli(self, *argv):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = P.main(list(argv))
        return code, json.loads(buffer.getvalue().strip().splitlines()[-1])

    def test_check_write_exit_codes(self):
        code, payload = self.run_cli("check-write", "--artifact-root", str(self.root), "--file", str(self.root / "plans" / "x.md"))
        self.assertEqual((code, payload["verdict"]), (P.OK, "allow"))
        self.activate()
        code, payload = self.run_cli("check-write", "--artifact-root", str(self.root), "--file", str(self.root / "plans" / "x.md"))
        self.assertEqual((code, payload["reason"]), (P.BLOCKED, "legacy-top-level-write-denied"))

    def test_begin_env_file(self):
        self.activate()
        route, route_file = self.route()
        env_file = Path(self._tmp.name) / "env"
        code, payload = self.run_cli("begin", "--artifact-root", str(self.root), "--route", str(route_file),
                                     "--capability", "autopilot-code", "--intensity", "direct",
                                     "--env-file", str(env_file))
        self.assertEqual(code, P.OK)
        lines = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        self.assertEqual(lines["AGENT_ARTIFACT_CYCLE_ID"], payload["cycle_id"])
        self.assertEqual(lines["AGENT_ARTIFACT_CYCLE_DIR"], payload["cycle_dir"])


if __name__ == "__main__":
    unittest.main()
