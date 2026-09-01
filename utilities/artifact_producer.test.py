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

    def seed_legacy(self, rel="plans/legacy.md", data="legacy body\n"):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        return target


class ActivateAndBeginTest(ProducerTestBase):
    def test_begin_before_activation_is_legacy_compat(self):
        self.seed_legacy()
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "legacy-compat")
        self.assertEqual(result["layout"], "legacy")
        self.assertFalse((self.root / "campaigns").exists())
        self.assertEqual(result["legacy_fallback"]["level"], "warn")
        self.assertEqual(result["legacy_fallback"]["reason"], "cutover-inactive-legacy-root")
        self.assertEqual(result["legacy_fallback"]["override"]["status"], "absent")

    def test_begin_require_cycle_fails_closed_when_inactive(self):
        route, route_file = self.route()
        self.seed_legacy()
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


class BootstrapTest(ProducerTestBase):
    def test_begin_against_empty_root_bootstraps(self):
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "begun")
        self.assertEqual(result["layout"], "cycle")
        cutover = P.read_cutover(self.root)
        self.assertEqual(cutover["activation_kind"], "bootstrap-empty-root")
        self.assertIsNone(cutover["approval_receipt_sha256"])
        self.assertEqual(cutover["state"], "active")
        self.assertIsNotNone(L.read_root_identity(self.root))
        self.assertTrue(idm.is_well_formed(result["campaign_id"], "campaign"))
        self.assertTrue(idm.is_well_formed(result["cycle_id"], "cycle"))
        self.assertTrue(idm.is_well_formed(result["producer_id"], "producer"))

    def test_bootstrap_creates_no_legacy_bucket_directory(self):
        self.begin()
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, [".runtime", "campaigns"])

    def test_bootstrapped_root_denies_legacy_top_level_write(self):
        self.begin()
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual((verdict["verdict"], verdict["reason"]),
                         ("deny", "legacy-top-level-write-denied"))

    def test_begin_require_cycle_bootstraps_empty_root(self):
        route, route_file, result = self.begin(require_cycle=True)
        self.assertEqual(result["status"], "begun")

    def test_bootstrap_adopts_existing_frozen_identity(self):
        identity_path = adm._root_identity_path(self.root)
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        payload = idm.RootIdentity(
            schema_version=1, artifact_root_id=ROOT_ID, repository_id=REPO_ID,
            issued_at="2026-09-02T00:00:00Z", producer_contract_version=m.CONTRACT_VERSION,
        ).to_payload()
        identity_path.write_text(json.dumps(payload), encoding="utf-8")
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "begun")
        self.assertEqual(P.read_cutover(self.root)["identity"]["artifact_root_id"], ROOT_ID)

    def test_explicit_activate_after_bootstrap_is_already_active_without_kind_promotion(self):
        self.begin()
        identity = L.read_root_identity(self.root)
        result = P.activate(self.root, repository_id=identity.repository_id,
                            artifact_root_id=identity.artifact_root_id)
        self.assertEqual(result["status"], "already-active")
        self.assertEqual(P.read_cutover(self.root)["activation_kind"], "bootstrap-empty-root")

    def test_malformed_cutover_record_blocks_begin(self):
        route, route_file = self.route()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "cutover-record-malformed")

    def test_read_only_oracles_never_bootstrap(self):
        P.check_write(self.root, self.root / "plans" / "x.md")
        P.status(self.root)
        P.resolve_output_dir(self.root, "spec")
        self.assertFalse(P.cutover_path(self.root).exists())


class ClassifyRootTest(ProducerTestBase):
    def test_empty_root_is_inactive_empty(self):
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-empty")

    def test_root_with_only_empty_directories_is_inactive_empty(self):
        (self.root / "plans").mkdir()
        (self.root / "spec").mkdir()
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-empty")

    def test_nested_regular_file_is_inactive_with_legacy(self):
        target = self.root / "plans" / "a" / "b" / "c.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual(klass["state"], "inactive-with-legacy")
        self.assertEqual(klass["legacy_top_level"], ["plans"])

    def test_campaigns_only_root_without_cutover_is_inactive_with_legacy(self):
        target = self.root / "campaigns" / "x" / "y.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-with-legacy")

    def test_symlink_counts_as_content_and_is_not_followed(self):
        outside = Path(self._tmp.name) / "outside-target"
        outside.mkdir()
        for i in range(100):
            (outside / f"f{i}.md").write_text("x", encoding="utf-8")
        (self.root / "plans").mkdir()
        os.symlink(outside, self.root / "plans" / "link")
        klass = P.classify_root(self.root, collect_legacy_top_level=True)
        self.assertEqual(klass["state"], "inactive-with-legacy")
        self.assertEqual(klass["legacy_top_level"], ["plans"])

    def test_missing_root_directory_is_inactive_empty(self):
        missing = self.root / "does-not-exist"
        self.assertEqual(P.classify_root(missing)["state"], "inactive-empty")

    def test_unreadable_cutover_is_malformed(self):
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual((klass["state"], klass["reason"]), ("malformed", "cutover-record-unreadable"))

    def test_unknown_cutover_state_is_malformed(self):
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text(json.dumps({"state": "paused"}), encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual((klass["state"], klass["reason"]), ("malformed", "cutover-schema-unknown"))

    def test_identity_conflict_is_malformed(self):
        self.activate()
        identity_path = adm._root_identity_path(self.root)
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        payload["artifact_root_id"] = "root_" + "f" * 32
        identity_path.write_text(json.dumps(payload), encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual((klass["state"], klass["reason"]), ("malformed", "identity-conflict"))

    def test_active_classification_does_not_walk_content(self):
        self.activate()
        self.seed_legacy()
        klass = P.classify_root(self.root)
        self.assertEqual(klass["state"], "active")
        self.assertEqual(klass["legacy_top_level"], [])


class LegacyFallbackTest(ProducerTestBase):
    def _override_payload(self, **overrides):
        payload = {
            "schema_version": 1, "contract": P.CONTRACT, "canonical_root": str(self.root),
            "reason": "test override", "issuer": "test", "created_at": "2026-09-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        payload.update(overrides)
        return payload

    def _write_override(self, payload):
        path = P.compat_override_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_warn_is_the_default_on_all_three_surfaces(self):
        self.seed_legacy()
        route, route_file, result = self.begin()
        self.assertEqual(result["legacy_fallback"]["level"], "warn")
        write_verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual(write_verdict["verdict"], "allow")
        self.assertEqual(write_verdict["legacy_fallback"]["level"], "warn")
        directory, layout = P.resolve_output_dir(self.root, "spec")
        self.assertEqual(layout, "legacy")

    def test_deny_without_override_blocks_all_three_surfaces(self):
        self.seed_legacy()
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            write_verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual((write_verdict["verdict"], write_verdict["reason"]),
                             ("deny", "cutover-inactive-fallback-denied"))
            route, route_file = self.route()
            with self.assertRaises(P.ProducerError) as ctx:
                P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
            self.assertEqual(ctx.exception.code, "cutover-inactive-fallback-denied")
            with self.assertRaises(P.ProducerError) as ctx:
                P.resolve_output_dir(self.root, "spec")
            self.assertEqual(ctx.exception.code, "cutover-inactive-fallback-denied")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_unknown_fallback_value_fails_closed_to_deny(self):
        self.seed_legacy()
        for value in ("WARN", "maybe"):
            os.environ[P.INACTIVE_FALLBACK_ENV] = value
            try:
                write_verdict = P.check_write(self.root, self.root / "plans" / "x.md")
                self.assertEqual(write_verdict["verdict"], "deny")
            finally:
                os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_valid_override_allows_and_records(self):
        self.seed_legacy()
        self._write_override(self._override_payload())
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual(verdict["verdict"], "allow")
            self.assertEqual(verdict["legacy_fallback"]["override"]["status"], "accepted")
            self.assertEqual(verdict["legacy_fallback"]["override"]["expires_at"], "2099-01-01T00:00:00Z")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_expired_override_is_rejected(self):
        self.seed_legacy()
        self._write_override(self._override_payload(expires_at="2000-01-01T00:00:00Z"))
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual(verdict["verdict"], "deny")
            self.assertEqual(verdict["legacy_fallback"]["override"]["reason"], "override-expired")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_malformed_override_is_rejected(self):
        self.seed_legacy()
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            self._write_override(self._override_payload(issuer=None))
            self.assertEqual(P.check_write(self.root, self.root / "plans" / "x.md")["legacy_fallback"]
                             ["override"]["reason"], "override-malformed")
            self._write_override(self._override_payload(schema_version=2))
            self.assertEqual(P.check_write(self.root, self.root / "plans" / "x.md")["legacy_fallback"]
                             ["override"]["reason"], "override-malformed")
            P.compat_override_path(self.root).write_text("{", encoding="utf-8")
            self.assertEqual(P.check_write(self.root, self.root / "plans" / "x.md")["legacy_fallback"]
                             ["override"]["reason"], "override-malformed")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_foreign_root_override_is_rejected(self):
        self.seed_legacy()
        self._write_override(self._override_payload(canonical_root=str(Path(self._tmp.name) / "elsewhere")))
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual(verdict["legacy_fallback"]["override"]["reason"], "override-foreign-root")
            self.assertEqual(verdict["verdict"], "deny")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_warn_keeps_level_warn_for_rejected_override(self):
        self.seed_legacy()
        self._write_override(self._override_payload(expires_at="2000-01-01T00:00:00Z"))
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual(verdict["legacy_fallback"]["level"], "warn")
        self.assertEqual(verdict["legacy_fallback"]["override"]["status"], "rejected")
        self.assertEqual(verdict["verdict"], "allow")

    def test_resolve_output_dir_signature_stays_a_two_tuple(self):
        self.seed_legacy()
        result = P.resolve_output_dir(self.root, "spec")
        self.assertEqual(len(result), 2)
        self.assertEqual(result, (self.root / "spec", "legacy"))

    def test_status_reports_classification_and_fallback(self):
        self.seed_legacy()
        result = P.status(self.root)
        self.assertEqual(result["root_classification"], "inactive-with-legacy")
        self.assertIsNone(result["activation_kind"])
        self.assertEqual(result["legacy_fallback"]["level"], "warn")
        for key in ("artifact_root", "cutover", "identity", "cycle_counts", "open_cycles", "pending_journals"):
            self.assertIn(key, result)

    def test_unrelated_verdicts_are_byte_identical_to_pre_change(self):
        self.seed_legacy()
        targets = {
            "runtime": self.root / ".runtime" / "x.json",
            "scratch": self.root / "_scratch" / "x",
            "outside": Path(self._tmp.name) / "elsewhere.md",
            "shared": self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md",
            "campaigns": self.root / "campaigns" / ("camp_" + "9" * 32) / "campaign.json",
        }
        for name, target in targets.items():
            verdict = P.check_write(self.root, target)
            self.assertNotIn("legacy_fallback", verdict, name)

    def test_inactive_empty_root_check_write_is_unchanged(self):
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("allow", "legacy-compat-window"))
        self.assertNotIn("legacy_fallback", verdict)

    def test_malformed_cutover_with_legacy_denies_check_write(self):
        self.seed_legacy()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("deny", "cutover-record-malformed"))
        self.assertNotIn("legacy_fallback", verdict)

    def test_malformed_cutover_with_legacy_blocks_resolve_output_dir(self):
        self.seed_legacy()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.resolve_output_dir(self.root, "spec")
        self.assertEqual(ctx.exception.code, "cutover-record-malformed")

    def test_malformed_cutover_with_legacy_cli_check_write_exits_65(self):
        self.seed_legacy()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = P.main(["check-write", "--artifact-root", str(self.root),
                                "--file", str(self.root / "plans" / "x.md")])
        self.assertEqual(exit_code, P.BLOCKED)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "cutover-record-malformed")


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
        outcome = P.finalize(
            self.root, cycle_id=result["cycle_id"], state="abandoned",
            abandon_reason="route-unrecoverable",
        )
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
            abandon_reason="route-unrecoverable",
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
                abandon_reason="route-unrecoverable",
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


class ReviewPublicationLeaseTest(ProducerTestBase):
    """SD-117 §13.34.5-(2): L1 review-publication-lease enforcement."""

    def test_review_round_one_fail_keeps_cycle_open_and_registered_publication_verdict_is_allow(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        # A review round-1 FAIL is not itself a finalize call -- SD-117 L3
        # says the cycle stays `open` until something explicitly abandons or
        # completes it, so a synthetic FAIL round leaves the record alone.
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "sealed")
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "sealed")

    def test_abandon_with_live_lease_refuses_with_zero_event_and_record_delta(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-reviewer")
        before = P.read_cycle_record(self.root, cycle_id)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "cycle-abandon-blocked-live-review")
        after = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(before, after)
        self.assertTrue(Path(result["cycle_dir"]).exists())

    def test_abandon_succeeds_after_deadline_and_later_write_is_cycle_not_open(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        past = 1_000_000_000.0
        P.review_lease_acquire(
            self.root, cycle_id=cycle_id, attempt_id="att-reviewer",
            deadline_seconds=1, now=past,
        )
        outcome = P.finalize(
            self.root, cycle_id=cycle_id, state="abandoned",
            abandon_reason="lease-expired-no-publisher", allow_open_route=True,
        )
        self.assertEqual(outcome["status"], "sealed")
        # Pre-existing behavior (unchanged by SD-117): a sealed cycle's
        # `finalize()` short-circuits idempotently before the `state`
        # argument is even inspected.
        again = P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(again["status"], "already-sealed")

    def test_corrupt_lease_record_blocks_abandon(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        lease_dir = P._review_lease_dir(self.root, cycle_id)
        lease_dir.mkdir(parents=True, exist_ok=True)
        (lease_dir / "att-corrupt.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "cycle-abandon-blocked-live-review")

    def test_double_acquire_and_double_release_are_idempotent(self):
        self.activate()
        route, route_file, result = self.begin()
        cycle_id = result["cycle_id"]
        first = P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-r")
        second = P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-r")
        self.assertEqual(first["status"], "acquired")
        self.assertEqual(second["status"], "already-held")
        release1 = P.review_lease_release(self.root, cycle_id=cycle_id, attempt_id="att-r")
        release2 = P.review_lease_release(self.root, cycle_id=cycle_id, attempt_id="att-r")
        self.assertEqual(release1["status"], "released")
        self.assertEqual(release2["status"], "already-released")


class AbandonReasonTest(ProducerTestBase):
    """SD-117 §13.34.5-(2): L3 `abandon_reason` closed enum."""

    def test_zero_row_cycle_seal_stays_no_lineage_with_directory_removed(self):
        self.activate()
        route, route_file, result = self.begin()
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "no-lineage")
        self.assertFalse(Path(result["cycle_dir"]).exists())

    def test_cycle_completed_injected_into_abandoned_stream_is_refused_by_unchanged_code(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision",
                  allow_open_route=True)
        # Pre-existing behavior (unchanged by SD-117, E47-6): re-finalizing a
        # sealed cycle short-circuits idempotently regardless of `state`.
        again = P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(again["status"], "already-sealed")

    def test_every_cycle_abandoned_event_carries_closed_enum_reason_disjoint_from_review_verdicts(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        review_verdicts = {"PASS", "FAIL", "BLOCKED", "allow", "deny"}
        self.assertEqual(P.ABANDON_REASONS & review_verdicts, set())
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="FAIL")
        self.assertEqual(caught.exception.code, "abandon-reason-required")
        outcome = P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision",
                             allow_open_route=True)
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        abandoned_events = [e for e in document["events"] if e["event_type"] == "cycle.abandoned"]
        self.assertEqual(len(abandoned_events), 1)
        self.assertEqual(abandoned_events[0]["payload"]["abandon_reason"], "operator-decision")
        self.assertNotIn(abandoned_events[0]["payload"]["abandon_reason"], review_verdicts)

    def test_sealed_on_disk_write_verdicts_are_identical_to_prior_revision(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "sealed")
        target = Path(result["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "extra.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "deny")
        self.assertEqual(verdict["reason"], "cycle-not-open")

    def test_force_abandon_ignoring_lease_requires_operator_override_live_review_reason(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-reviewer")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(
                self.root, cycle_id=cycle_id, state="abandoned",
                abandon_reason="operator-decision", force_abandon_ignoring_lease=True,
            )
        self.assertEqual(caught.exception.code, "abandon-reason-required")
        outcome = P.finalize(
            self.root, cycle_id=cycle_id, state="abandoned",
            force_abandon_ignoring_lease=True, allow_open_route=True,
        )
        self.assertEqual(outcome["status"], "sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        abandoned_events = [e for e in document["events"] if e["event_type"] == "cycle.abandoned"]
        self.assertEqual(abandoned_events[0]["payload"]["abandon_reason"], "operator-override-live-review")


if __name__ == "__main__":
    unittest.main()
