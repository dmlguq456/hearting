#!/usr/bin/env python3
"""Recovery lineage regressions, through the real consumers in temporary state.

Existing falsifiers stay at their owning boundary. This one command runs them
with the shared history/authority/evidence rules and adds the cross-boundary
selection and grant cases. Each group is isolated from the next group's mocks.
No model, GPU workload, installed registry or other project's state is used.
"""
from __future__ import annotations

import copy
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace

import route_authority as AUTHORITY
import dispatch_replacement as REPLACEMENT
from dispatch_contract import DispatchContractError

HERE = Path(__file__).resolve().parent


class RecoveryAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.route = {"route_id": "rt-history", "route_hash": "sha256:history",
                      "artifact_root": str(self.root / ".agent_reports"),
                      "cwd": str(self.root / "worktree"), "nodes": [],
                      "work_request": {"text": "Perform the approved task."}}
        self.history = {"harness": "codex", "jobs": str(self.root / "jobs.log"),
                        "worktree": self.route["cwd"], "launch_home": str(HERE.parent),
                        "task": "Perform the approved task.", "argv": ["--unit", "dev/refactor"],
                        "resolved": {"model_profile": "deep", "model": "old-model", "qa": "standard"},
                        "applied_permissions": {}}

    def test_model_realization_follows_current_selection_without_replaying_original(self):
        before = json.dumps(self.history, sort_keys=True)
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                route = dict(self.route, selection_pins={"contract_version": 1,
                             "owner": {"harness": harness, "model": "current-model", "effort": None}})
                candidate = copy.deepcopy(self.history)
                candidate["harness"] = harness
                candidate["resolved"].update(model="current-model")
                if harness == "opencode":
                    candidate["applied_permissions"] = REPLACEMENT._applied_permissions(
                        Namespace(opencode_config_content=json.dumps({
                            "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
                            "agents": {},
                        })))
                AUTHORITY.require_recovery_work(candidate, self.history, route=route, worker_type="owner")
        self.assertEqual(json.dumps(self.history, sort_keys=True), before)

    def test_cross_harness_legacy_grant_keeps_addresses_and_blocks_expansion(self):
        route = dict(self.route, selection_pins={"contract_version": 1,
                     "owner": {"harness": "opencode", "model": None, "effort": None}})
        configs = (
            {"permission": {
                "external_directory": {"*": "deny", self.route["artifact_root"] + "/**": "allow"},
                "edit": {"*": "deny", self.route["cwd"] + "/**": "allow"},
            }},
            {"permissions": [
                {"action": "external_directory", "resource": "*", "effect": "deny"},
                {"action": "external_directory", "resource": self.route["artifact_root"] + "/**",
                 "effect": "allow"},
                {"action": "edit", "resource": "*", "effect": "deny"},
                {"action": "edit", "resource": self.route["cwd"] + "/**", "effect": "allow"},
            ], "agents": {}},
        )
        for config in configs:
            with self.subTest(generation="v2" if "permissions" in config else "v1"):
                candidate = copy.deepcopy(self.history)
                candidate["harness"] = "opencode"
                candidate["applied_permissions"] = REPLACEMENT._applied_permissions(
                    Namespace(opencode_config_content=json.dumps(config)))
                self.assertTrue(candidate["applied_permissions"].get("opencode_permission"))
                AUTHORITY.require_recovery_work(candidate, self.history, route=route, worker_type="owner")
                for category in ("external_directory", "edit"):
                    forged_config = json.loads(json.dumps(config))
                    outside = str(self.root) + "/**"
                    if "permissions" in forged_config:
                        forged_config["permissions"].append({
                            "action": category, "resource": outside, "effect": "allow"})
                    else:
                        forged_config["permission"].setdefault(category, {})[outside] = "allow"
                    forged = copy.deepcopy(candidate)
                    forged["applied_permissions"] = REPLACEMENT._applied_permissions(
                        Namespace(opencode_config_content=json.dumps(forged_config)))
                    spawn = 0
                    with self.assertRaises(DispatchContractError):
                        AUTHORITY.require_recovery_work(
                            forged, self.history, route=route, worker_type="owner")
                        spawn += 1
                    self.assertEqual(spawn, 0)

    def test_release_move_preserves_portable_profile_and_allows_declared_transition(self):
        candidate = copy.deepcopy(self.history)
        candidate["launch_home"] = str(self.root / "new-release")
        candidate["resolved"].update(model="current-model", model_profile="balanced")
        before = json.dumps(self.history, sort_keys=True)
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(candidate, self.history, route=self.route, worker_type="owner")
        AUTHORITY.require_recovery_work(candidate, self.history, route=self.route, worker_type="owner",
                                        transition={"from": "deep", "to": "balanced"})
        self.assertEqual(json.dumps(self.history, sort_keys=True), before)

    def test_cross_harness_legacy_network_and_claude_unbounded_tools_are_rejected(self):
        candidate = copy.deepcopy(self.history); candidate["harness"] = "claude"
        grants = [{"execution_access": {"network": "granted-unenforced"}},
                  {"nested_headless_network": True}]
        grants += [{"claude": {"allowed_tools": [rule]}}
                   for rule in ("Read(//etc/**)", "Edit", "Write", "Read", "Edit(../**)")]
        for grant in grants:
            with self.subTest(grant=grant), self.assertRaises(DispatchContractError):
                candidate["applied_permissions"] = grant
                AUTHORITY.require_recovery_work(candidate, self.history, route=self.route, worker_type="owner")
        candidate["applied_permissions"] = {"execution_access": {"network": "granted-unenforced"}}
        historical = copy.deepcopy(self.history)
        historical["applied_permissions"] = {"execution_access": {"network": "enforced"}}
        AUTHORITY.require_recovery_work(candidate, historical, route=self.route, worker_type="owner")

    def test_cross_harness_uses_real_opencode_launcher_permissions(self):
        path = HERE.parent / "adapters/opencode/bin/dispatch-headless.py"
        spec = importlib.util.spec_from_file_location("recovery_opencode_launcher", path)
        wrapper = importlib.util.module_from_spec(spec); sys.modules[spec.name] = wrapper
        spec.loader.exec_module(wrapper)
        candidate = copy.deepcopy(self.history); candidate["harness"] = "opencode"
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": ""}):
            scoped = json.loads(wrapper.scoped_external_directory_config(
                self.route["artifact_root"], agent_home=HERE.parent, worktree=self.route["cwd"],
                selected_agent="build"))
        candidate["applied_permissions"] = REPLACEMENT._applied_permissions(
            Namespace(opencode_config_content=json.dumps(scoped), agent="build"))
        self.assertTrue(candidate["applied_permissions"].get("opencode_permission"))
        self.assertTrue(candidate["applied_permissions"].get("opencode_selected_agent_permission"))
        AUTHORITY.require_recovery_work(candidate, self.history, route=self.route, worker_type="owner")
        for category, pattern in (("external_directory", "/etc/**"),
                                  ("edit", str(self.root / "outside") + "/**")):
            forged = copy.deepcopy(candidate)
            if category == "edit":
                selected = forged["applied_permissions"]["opencode_selected_agent_permission"]
                selected["local_policy"].setdefault(category, {})[pattern] = "allow"
                import opencode_runtime
                selected["permission"] = opencode_runtime.opencode_permission_ordered_view(
                    selected["local_policy"])
            else:
                rules = forged["applied_permissions"]["opencode_permission"][category]
                rules.pop(pattern, None); rules[pattern] = "allow"
            with self.subTest(category=category), self.assertRaises(DispatchContractError):
                AUTHORITY.require_recovery_work(forged, self.history, route=self.route, worker_type="owner")

    def test_selected_policy_reseal_and_legacy_absence_recovery_admission(self):
        route = dict(self.route, selection_pins={"contract_version": 1,
                     "owner": {"harness": "opencode", "model": None, "effort": None}})
        global_config = {"permission": {"read": "allow", "bash": "ask"}}

        def candidate_for(agent, selected_definition):
            config = dict(global_config)
            if selected_definition is not None:
                config["agent"] = {agent: selected_definition}
            return {
                **copy.deepcopy(self.history),
                "harness": "opencode",
                "applied_permissions": REPLACEMENT._applied_permissions(Namespace(
                    opencode_config_content=json.dumps(config), agent=agent)),
            }

        selected_policy = {"permission": {"bash": "ask", "edit": {"*": "deny"}}}
        previous = candidate_for("build", selected_policy)
        renamed = candidate_for("builder", selected_policy)
        self.assertTrue(AUTHORITY.same_sealed_work(previous, renamed))
        AUTHORITY.require_recovery_work(
            renamed, previous, route=route, worker_type="owner")

        changed = candidate_for("builder", {"permission": {"bash": "allow",
                                                               "edit": {"*": "deny"}}})
        self.assertFalse(AUTHORITY.same_sealed_work(previous, changed))
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(changed, previous, route=route, worker_type="owner")

        # An immutable pre-field record is not backfilled with a guessed local
        # permission. A current launch with no selected definition or policy
        # remains admissible under the sealed global grant.
        legacy = candidate_for("build", None)
        legacy["applied_permissions"].pop("opencode_selected_agent_permission")
        for definition in (None, {}, {"mode": "primary"}):
            with self.subTest(selected_definition=definition):
                current = candidate_for("build", definition)
                self.assertTrue(AUTHORITY.same_sealed_work(legacy, current))
                AUTHORITY.require_recovery_work(
                    current, legacy, route=route, worker_type="owner")

        outside = candidate_for("build", {"permission": {
            "edit": {str(self.root / "outside") + "/**": "allow"}}})
        self.assertFalse(AUTHORITY.same_sealed_work(legacy, outside))
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(
                outside, legacy, route=route, worker_type="owner")

        # The absent optional record does not invent an old selected policy,
        # but candidate non-file allows remain bounded by the sealed global
        # policy. Equivalent asks and bounded file grants remain recoverable.
        equivalent = candidate_for("build", {"permission": {"bash": "ask"}})
        AUTHORITY.require_recovery_work(
            equivalent, legacy, route=route, worker_type="owner")
        bounded_file = candidate_for("build", {"permission": {
            "edit": {self.route["cwd"] + "/**": "allow"}}})
        AUTHORITY.require_recovery_work(
            bounded_file, legacy, route=route, worker_type="owner")

        widened = candidate_for("build", {"permission": {"bash": "allow"}})
        self.assertFalse(AUTHORITY.same_sealed_work(legacy, widened))
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(
                widened, legacy, route=route, worker_type="owner")

        ordered_global = {"permission": {"bash": {"*": "ask", "git *": "allow"}}}
        ordered_legacy = {
            **copy.deepcopy(self.history), "harness": "opencode",
            "applied_permissions": REPLACEMENT._applied_permissions(Namespace(
                opencode_config_content=json.dumps(ordered_global), agent="build")),
        }
        ordered_legacy["applied_permissions"].pop("opencode_selected_agent_permission")

        def ordered_candidate(selected_policy):
            config = {
                **copy.deepcopy(ordered_global),
                "agent": {"build": {"permission": selected_policy}},
            }
            return {
                **copy.deepcopy(self.history), "harness": "opencode",
                "applied_permissions": REPLACEMENT._applied_permissions(Namespace(
                    opencode_config_content=json.dumps(config), agent="build")),
            }

        # A later global wildcard allow bounds this exact selected resource;
        # the earlier ask remains authoritative outside its resource pattern.
        ordered_equivalent = ordered_candidate({"bash": {"git status": "allow"}})
        AUTHORITY.require_recovery_work(
            ordered_equivalent, ordered_legacy, route=route, worker_type="owner")
        resource_widened = ordered_candidate({"bash": {"*": "allow"}})
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(
                resource_widened, ordered_legacy, route=route, worker_type="owner")
        action_widened = ordered_candidate({"*": {"git status": "allow"}})
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(
                action_widened, ordered_legacy, route=route, worker_type="owner")

    def test_recovery_work_scopes_agent_argv_exception_to_opencode(self):
        claude_history = copy.deepcopy(self.history)
        claude_history.update(
            harness="claude",
            argv=["--unit", "dev/refactor", "--agent", "security-reviewer"],
        )
        claude_candidate = copy.deepcopy(claude_history)
        claude_candidate["argv"][-1] = "general-purpose"
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(
                claude_candidate, claude_history, route=self.route, worker_type="stage")

        def opencode(agent):
            config = {
                "permission": {"read": "allow", "bash": "ask"},
                "agent": {agent: {"permission": {"bash": "ask"}}},
            }
            return {
                **copy.deepcopy(self.history),
                "harness": "opencode",
                "argv": ["--unit", "dev/refactor", "--agent", agent],
                "applied_permissions": REPLACEMENT._applied_permissions(Namespace(
                    opencode_config_content=json.dumps(config), agent=agent)),
            }

        selected_history = opencode("build")
        selected_candidate = opencode("builder")
        AUTHORITY.require_recovery_work(
            selected_candidate, selected_history, route=self.route, worker_type="stage")

    def test_cross_harness_uses_real_claude_and_codex_owner_permissions(self):
        wrappers = {}
        for harness in ("claude", "codex"):
            spec = importlib.util.spec_from_file_location(
                "recovery_" + harness + "_launcher", HERE.parent / "adapters" / harness / "bin/dispatch-headless.py")
            module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
            spec.loader.exec_module(module); wrappers[harness] = module
        candidate = copy.deepcopy(self.history); candidate["harness"] = "claude"
        rules = wrappers["claude"]._allowlist_rules(
            HERE.parent, self.route["cwd"], self.route["artifact_root"], "owner")
        candidate["applied_permissions"] = {"claude": {"allowed_tools": rules}}
        AUTHORITY.require_recovery_work(candidate, self.history, route=self.route, worker_type="owner")
        history = copy.deepcopy(candidate)
        args = argparse.Namespace(dispatch_depth=1, worker_type="owner", intensity="standard",
                                  sandbox="workspace-write", gpu_execution_scope=False)
        history["resolved"].update(dispatch_depth=1, worker_type="owner", intensity="standard",
                                   sandbox="workspace-write")
        candidate = copy.deepcopy(history); candidate["harness"] = "codex"
        candidate["applied_permissions"] = {"nested_headless_network":
                                            wrappers["codex"].nested_headless_network_enabled(args)}
        self.assertTrue(candidate["applied_permissions"]["nested_headless_network"])
        AUTHORITY.require_recovery_work(candidate, history, route=self.route, worker_type="owner")
        candidate["applied_permissions"]["execution_access"] = {"network": "not-requested",
                                                               "request_sha256": "foreign"}
        history["applied_permissions"]["execution_access"] = {"network": "not-requested",
                                                             "request_sha256": "original"}
        with self.assertRaises(DispatchContractError):
            AUTHORITY.require_recovery_work(candidate, history, route=self.route, worker_type="owner")

    def test_preparation_and_claim_do_not_adopt_a_continuation(self):
        for metadata in ({"launch_claimed": "0"}, {"launch_claimed": "1"},
                         {"launch_claimed": "1", "launch_started": "0"},
                         {"launch_claimed": "1", "launch_outcome": "never-launched"}):
            self.assertNotEqual(AUTHORITY.continuation_attempt_state(metadata), "started")
        self.assertEqual(AUTHORITY.continuation_attempt_state({"launch_started": "1"}), "started")
        self.assertEqual(AUTHORITY.continuation_attempt_state({"pid": "1"}), "unknown")

    def test_same_lineage_cannot_change_work_scope(self):
        record = {"route_id": "rt-history", "route_hash": "sha256:history"}
        source = {"attempt_id": "att-old", "route_id": "rt-history", "route_hash": "sha256:history",
                  "parent_sid": "parent", "dispatch_depth": "1", "route_node": "execute",
                  "worker_type": "stage", "fixed_inputs_sha256": "sha256:original"}
        for key, value in (("route_node", "another"), ("fixed_inputs_sha256", "sha256:tampered"),
                           ("session_chain_id", "different-chain"), ("parent_sid", "foreign")):
            candidate = dict(source, **{key: value})
            with self.subTest(key=key), self.assertRaises(DispatchContractError):
                AUTHORITY.require_recovery_binding(record, source, candidate, self.root / "jobs.log")


GROUPS = (
    ("recovery_history.test.py", ()),
    ("recovery_evidence.test.py", ()),
    ("dispatch_replacement.test.py", ()),
    ("dispatch_replacement_batch.test.py", ()),
    ("dispatch-batch.test.py", ()),
    ("resource_sequence.test.py", ()),
    ("owner_continuation_supervision.test.py", ()),
    ("owner_route_binding.test.py", ()),
    ("route_authority.test.py", ()),
    ("artifact_producer.test.py", ("RouteLineageBindingTest",)),
    ("capability_route.test.py", ("TestContinuation", "FrameSummaryContractTest",)),
    ("dispatch_contract.test.py", ()),
    ("dispatch_registry.test.py", ()),
    ("capacity_resume.test.py", ()),
    ("review_input_preview.test.py", ()),
    ("dispatch_harvest.test.py", ()),
    ("dispatch_liveness_matrix.test.py", ()),
    ("dispatch_completion_marker.test.py", (
        "CompletionMarkerTest.test_a_sd154_dry_run_unobserved_review_preserves_marker_and_row",
        "CompletionMarkerTest.test_complete_unwritable_jobs_marker_preserved_then_reconcile_repairs")),
    ("stage_dispatch_fallback.test.py", (
        "FallbackTest.test_registry_prevents_explicitly_classified_tuple_retry",
        "FallbackTest.test_registry_worker_deaths_do_not_spend_a_launch_tuple",
        "FallbackTest.test_review_round_cap_correction_round_carries_protocol_block_with_prompt_file",
        "FallbackTest.test_review_round_cap_correction_round_carries_protocol_block_without_prompt_file")),
    ("../tools/fleet/tests/test_f28_route.py", ("OrphanConductorAnnotationTest",)),
)


def main():
    runner = unittest.TextTestRunner(verbosity=1)
    local = runner.run(unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryAuthorityTest))
    total = local.testsRun
    skipped = len(local.skipped)
    failures = not local.wasSuccessful()
    child = """import importlib.util,sys,unittest
from pathlib import Path
path=Path(sys.argv[1]); sys.path.insert(0,str(path.parent))
spec=importlib.util.spec_from_file_location('lineage_group',path)
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
loader=unittest.defaultTestLoader
suite=unittest.TestSuite(loader.loadTestsFromName(name,module) for name in sys.argv[2:]) if len(sys.argv)>2 else loader.loadTestsFromModule(module)
result=unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
"""
    # Gate-sensitive falsifiers run under their own test environment. This does
    # not alter the person's disabled gates or any installed runtime setting.
    environment = dict(os.environ)
    for filename, classes in GROUPS:
        result = subprocess.run([sys.executable, "-c", child, str(HERE / filename), *classes],
                                env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = result.stdout + result.stderr
        match = re.search(r"Ran (\d+) tests?", output)
        count = int(match.group(1)) if match else 0
        skip_match = re.search(r"skipped=(\d+)", output)
        group_skipped = int(skip_match.group(1)) if skip_match else 0
        skipped += group_skipped
        total += count
        print(f"{filename}: {'PASS' if result.returncode == 0 else 'FAIL'} ({count} tests, {group_skipped} skipped)", flush=True)
        if result.returncode:
            failures = True
            print(output[-12000:], file=sys.stderr)
    print(f"recovery-lineage: {'FAIL' if failures else 'PASS'} ({total} tests, {skipped} skipped)")
    return int(failures)


if __name__ == "__main__":
    raise SystemExit(main())
