#!/usr/bin/env python3
"""The helpers the three dispatch wrappers shared letter for letter now live once (audit §4 #10)."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import fcntl
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_wrapper_common as C  # noqa: E402
import dispatch_resource_wait as WAIT  # noqa: E402

_fixture_spec = importlib.util.spec_from_file_location(
    "wrapper_resource_workflow_fixture", ROOT / "utilities" / "workflow_supervisor.test.py")
FIX = importlib.util.module_from_spec(_fixture_spec)
sys.modules[_fixture_spec.name] = FIX
_fixture_spec.loader.exec_module(FIX)

ALIASES = {
    "fail": "fail", "jobs_lock": "jobs_lock", "process_start_ticks": "process_start_ticks",
    "read_launch_fence_failure": "read_launch_fence_failure", "resolve_artifact_root": "resolve_artifact_root",
    "_is_report_bundle_publish_stage": "is_report_bundle_publish_stage",
    "resolve_report_bundle_root": "resolve_report_bundle_root", "seed_launch_heartbeat": "seed_launch_heartbeat",
    "_route_node_leg_fields": "route_node_leg_fields", "_supervisor_route": "supervisor_route",
    "prepare_review_output_request": "prepare_review_output_request",
    "watch_early_death": "watch_early_death",
    "write_reset_cache": "write_reset_cache",
    "diff_attribution_prompt": "diff_attribution_prompt",
}


def load(harness: str):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_dispatch_headless_common", ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WrapperCommonTest(unittest.TestCase):
    def test_pair_hint_without_route_evidence_keeps_the_sealed_profile(self):
        args = argparse.Namespace(model_profile="deep", pair_model_profile="balanced",
                                  route_file=None, owner_route_binding=None, model=None)
        context = C.prepare_pair_model_rotation(args, harness="claude")
        self.assertEqual(context["status"], "none")
        self.assertEqual(context["profile"], "deep")
        self.assertEqual(args.model_profile, "deep")

    def _pair_args(self, base, hint="balanced"):
        from model_profile import resolve_profile_demand
        route = {"route_id": "rt-pair", "route_hash": "sha256:pair", "nodes": [
            {"id": "plan-a", "parallel_group": "plan", "model_profile": "deep",
             "profile_demand": None, "profile_selection": resolve_profile_demand(None, explicit_profile="deep")},
            {"id": "plan-b", "parallel_group": "plan", "model_profile": "balanced-deep",
             "profile_demand": None, "profile_selection": resolve_profile_demand(None, explicit_profile="balanced-deep")},
        ]}
        route["profile_selection_contract_version"] = 1
        route_path = Path(base) / "route.json"
        route_path.write_text(json.dumps(route))
        jobs = Path(base) / "jobs.log"
        jobs.write_text("")
        return argparse.Namespace(
            model_profile="balanced-deep", pair_model_profile=hint,
            route_file=str(route_path), owner_route_binding=None, route_node="plan-b",
            action="start", attempt_id="att-current", parent_attempt_id="att-parent",
            fallback_hop="same-harness-headless", fallback_ordinal=1,
            artifact_root=str(base), agent_home=str(ROOT), jobs=str(jobs),
            worker_type="stage", model=None, registered_worker=1, dispatch_depth=2,
            inherit_model_settings=False, model_role="fast implementer", effort=None,
            capacity_retry=0,
        )

    def test_pair_hint_is_rederived_from_batch_reservation_and_shared_models(self):
        manifest = {"members": [
            {"route_node": "plan-a", "model_profile": "deep", "harness": "claude"},
            {"route_node": "plan-b", "model_profile": "balanced-deep", "harness": "claude"},
        ]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN": "token-fixture"}), \
             mock.patch("dispatch_contract.replica_batch_expectation", return_value={}), \
             mock.patch("dispatch_contract.resolve_model_governor_root", return_value=Path(td) / "governor"), \
             mock.patch("dispatch_contract._governor_json", return_value={
                 "state": "unclaimed", "batch_manifest": manifest}), \
             mock.patch("route_authority.require_batch_reservation"), \
             mock.patch("replica_batch_contract.verify_manifest", return_value=(manifest, "digest", {})), \
             mock.patch("dispatch_capacity_evidence.usage_states", return_value={"claude": "ok"}):
            args = self._pair_args(td)
            context = C.prepare_pair_model_rotation(args, harness="claude")
            wrapper = load("claude")
            settings = wrapper.resolve_model_settings(args)
            args.resolved_model_settings = settings
            from model_profile import selection_receipt
            receipt = selection_receipt(args)
        self.assertEqual(context["status"], "rotated", context)
        self.assertEqual(context["profile"], "balanced")
        self.assertEqual(args.model_profile, "balanced-deep")
        self.assertEqual(settings["profile"], "balanced")
        self.assertNotEqual(settings["model"], context["sibling_model"])
        self.assertEqual(receipt["model_diversity"], "rotated")
        self.assertEqual(receipt["model_source"], "profile+diversity")

    def test_cross_harness_manifest_keeps_the_sealed_profile_despite_pair_hint(self):
        manifest = {"members": [
            {"route_node": "plan-a", "model_profile": "deep", "harness": "codex"},
            {"route_node": "plan-b", "model_profile": "balanced-deep", "harness": "claude"},
        ]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN": "token-fixture"}), \
             mock.patch("dispatch_contract.replica_batch_expectation", return_value={}), \
             mock.patch("dispatch_contract.resolve_model_governor_root", return_value=Path(td) / "governor"), \
             mock.patch("dispatch_contract._governor_json", return_value={
                 "state": "unclaimed", "batch_manifest": manifest}), \
             mock.patch("route_authority.require_batch_reservation"), \
             mock.patch("replica_batch_contract.verify_manifest", return_value=(manifest, "digest", {})):
            args = self._pair_args(td)
            context = C.prepare_pair_model_rotation(args, harness="claude")
            wrapper = load("claude")
            settings = wrapper.resolve_model_settings(args)
        self.assertEqual(context["status"], "unverified-kept-sealed")
        self.assertEqual(context["profile"], "balanced-deep")
        self.assertEqual(settings["profile"], "balanced-deep")
        self.assertEqual(args.model_profile, "balanced-deep")

    def test_unmatched_pair_hint_keeps_the_sealed_profile(self):
        manifest = {"members": [
            {"route_node": "plan-a", "model_profile": "deep", "harness": "claude"},
            {"route_node": "plan-b", "model_profile": "balanced-deep", "harness": "claude"},
        ]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN": "token-fixture"}), \
             mock.patch("dispatch_contract.replica_batch_expectation", return_value={}), \
             mock.patch("dispatch_contract.resolve_model_governor_root", return_value=Path(td) / "governor"), \
             mock.patch("dispatch_contract._governor_json", return_value={
                 "state": "unclaimed", "batch_manifest": manifest}), \
             mock.patch("route_authority.require_batch_reservation"), \
             mock.patch("replica_batch_contract.verify_manifest", return_value=(manifest, "digest", {})), \
             mock.patch("dispatch_capacity_evidence.usage_states", return_value={"claude": "ok"}):
            args = self._pair_args(td, hint="light")
            context = C.prepare_pair_model_rotation(args, harness="claude")
        self.assertEqual(context["status"], "unverified-kept-sealed")
        self.assertEqual(context["profile"], "balanced-deep")
        self.assertEqual(args.model_profile, "balanced-deep")

    def test_frame_pair_uses_the_first_registry_rows_actual_model(self):
        from model_config import resolve_config
        from model_profile import resolve_profile_demand, resolve_profile_values, selection_receipt
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config, _ = resolve_config("claude", source_root=ROOT)
            top_model = resolve_profile_values("claude", config, "top")["model"]
            route = {"route_id": "rt-frame-pair", "route_hash": "sha256:frame-pair",
                     "profile_selection_contract_version": 1,
                     "nodes": [{"id": name, "worker_type": "frame", "model_profile": "top",
                                "profile_demand": None,
                                "profile_selection": resolve_profile_demand(None, explicit_profile="top")}
                               for name in ("frame", "frame-alternative")]}
            route_path = base / "route.json"
            route_path.write_text(json.dumps(route))
            jobs = base / "jobs.log"
            jobs.write_text("now\trunning\t/repo\t/tree\tframe\t"
                f"route_id={route['route_id']},route_hash={route['route_hash']},route_node=frame,"
                f"worker_type=frame,harness=claude,model={top_model},attempt_id=att-first\n")
            args = argparse.Namespace(
                model_profile="top", pair_model_profile="deep", route_file=str(route_path),
                owner_route_binding=None, route_node="frame-alternative", worker_type="frame",
                action="start", artifact_root=str(base), agent_home=str(ROOT), jobs=str(jobs), model=None,
                registered_worker=1, dispatch_depth=1, inherit_model_settings=False,
                model_role="deep maker", effort=None, variant=None, capacity_retry=0,
            )
            with mock.patch("dispatch_capacity_evidence.usage_states", return_value={"claude": "ok"}):
                context = C.prepare_pair_model_rotation(args, harness="claude")
                wrapper = load("claude")
                settings = wrapper.resolve_model_settings(args)
                args.resolved_model_settings = settings
                receipt = selection_receipt(args)
        self.assertEqual(context["status"], "rotated")
        self.assertEqual(context["profile"], "deep")
        self.assertEqual(context["sibling_model"], top_model)
        self.assertEqual(settings["profile"], "deep")
        self.assertEqual(args.model_profile, "top")
        self.assertEqual(receipt["model_diversity"], "rotated")

    def test_opencode_frame_pair_skips_collapsed_profiles_and_receipts_balanced_rotation(self):
        from model_config import resolve_config
        from model_profile import resolve_profile_demand, resolve_profile_values, selection_receipt
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config, _ = resolve_config("opencode", source_root=ROOT)
            top_model = resolve_profile_values("opencode", config, "top")["model"]
            route = {"route_id": "rt-opencode-frame", "route_hash": "sha256:opencode-frame",
                     "profile_selection_contract_version": 1,
                     "nodes": [{"id": name, "worker_type": "frame", "model_profile": "top",
                                "profile_demand": None,
                                "profile_selection": resolve_profile_demand(None, explicit_profile="top")}
                               for name in ("frame", "frame-alternative")]}
            route_path = base / "route.json"
            route_path.write_text(json.dumps(route))
            jobs = base / "jobs.log"
            jobs.write_text("now\trunning\t/repo\t/tree\tframe\t"
                f"route_id={route['route_id']},route_hash={route['route_hash']},route_node=frame,"
                f"worker_type=frame,harness=opencode,model={top_model},attempt_id=att-first\n")
            args = argparse.Namespace(
                model_profile="top", pair_model_profile="balanced", route_file=str(route_path),
                owner_route_binding=None, route_node="frame-alternative", worker_type="frame",
                action="start", artifact_root=str(base), agent_home=str(ROOT), jobs=str(jobs), model=None,
                registered_worker=1, dispatch_depth=1, inherit_model_settings=False,
                model_role="deep maker", effort=None, variant=None, capacity_retry=0,
            )
            with mock.patch("dispatch_capacity_evidence.usage_states", return_value={"opencode": "ok"}):
                context = C.prepare_pair_model_rotation(args, harness="opencode")
                wrapper = load("opencode")
                settings = wrapper.resolve_model_settings(args)
                args.resolved_model_settings = settings
                receipt = selection_receipt(args)
        self.assertEqual(context["status"], "rotated")
        self.assertEqual(context["profile"], "balanced")
        self.assertEqual(args.model_profile, "top")
        self.assertEqual(settings["profile"], "balanced")
        self.assertNotEqual(settings["model"], top_model)
        self.assertEqual(receipt["model_diversity"], "rotated")
        self.assertEqual(receipt["model_source"], "profile+diversity")

    def test_missing_model_alternative_is_unavailable_and_target_limit_keeps_seal(self):
        from model_config import resolve_config
        from model_profile import resolve_profile_values
        config, receipt = resolve_config("claude", source_root=ROOT)
        config = dict(config)
        for profile in ("balanced-deep", "balanced", "light"):
            config["CFG_MODEL_PROFILE_" + profile.upper().replace("-", "_")] = "deep:medium"
        manifest = {"members": [
            {"route_node": "plan-a", "model_profile": "deep", "harness": "claude"},
            {"route_node": "plan-b", "model_profile": "balanced-deep", "harness": "claude"},
        ]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN": "token-fixture"}), \
             mock.patch("model_config.resolve_config", return_value=(config, receipt)), \
             mock.patch("dispatch_contract.replica_batch_expectation", return_value={}), \
             mock.patch("dispatch_contract.resolve_model_governor_root", return_value=Path(td) / "governor"), \
             mock.patch("dispatch_contract._governor_json", return_value={
                 "state": "unclaimed", "batch_manifest": manifest}), \
             mock.patch("route_authority.require_batch_reservation"), \
             mock.patch("replica_batch_contract.verify_manifest", return_value=(manifest, "digest", {})):
            args = self._pair_args(td, hint=None)
            unavailable = C.prepare_pair_model_rotation(args, harness="claude")
            wrapper = load("claude")
            settings = wrapper.resolve_model_settings(args)
            args.resolved_model_settings = settings
            from model_profile import selection_receipt
            receipt = selection_receipt(args)
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["profile"], "balanced-deep")
        self.assertEqual(settings["profile"], "balanced-deep")
        self.assertEqual(settings["model"], resolve_profile_values("claude", config, "balanced-deep")["model"])
        self.assertEqual(receipt["model_diversity"], "unavailable")
        self.assertEqual(settings["source"], "profile")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN": "token-fixture"}), \
             mock.patch("dispatch_contract.replica_batch_expectation", return_value={}), \
             mock.patch("dispatch_contract.resolve_model_governor_root", return_value=Path(td) / "governor"), \
             mock.patch("dispatch_contract._governor_json", return_value={
                 "state": "unclaimed", "batch_manifest": manifest}), \
             mock.patch("route_authority.require_batch_reservation"), \
             mock.patch("replica_batch_contract.verify_manifest", return_value=(manifest, "digest", {})), \
             mock.patch("dispatch_capacity_evidence.usage_states", return_value={"claude": "limited(target)"}):
            args = self._pair_args(td)
            limited = C.prepare_pair_model_rotation(args, harness="claude")
        self.assertEqual(limited["status"], "target-limited")
        self.assertEqual(limited["profile"], "balanced-deep")

    def make_release(self, root: Path):
        root.mkdir(parents=True)
        (root / ".hearting-release.json").write_text(
            '{"schema":1,"version":"fixture"}\n', encoding="utf-8"
        )
        for harness in ("claude", "codex", "opencode"):
            target = root / "adapters" / harness / "bin" / "dispatch-headless.py"
            target.parent.mkdir(parents=True)
            source = ROOT / "adapters" / harness / "bin" / "dispatch-headless.py"
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def test_guard_refusal_keeps_typed_reason_and_bounded_detail(self):
        stderr = '{"status":"blocked","reason":"unsafe-git-operation","detail":"cherry-pick"}\n'
        self.assertEqual(C._route_guard_refusal(stderr),
                         ("unsafe-git-operation", "cherry-pick"))
        self.assertEqual(C._route_guard_refusal('{"reason":{},"detail":"x"}'),
                         ("worker-route-validation-failed", "guard output lacked a typed refusal"))
        self.assertEqual(C._route_guard_refusal('broken'),
                         ("worker-route-validation-failed", "guard output lacked a typed refusal"))
        self.assertEqual(C._route_guard_refusal('{"status":"blocked","reason":"safe","detail":"x\\ny"}'),
                         ("safe", "x y"))

    def test_guard_detail_cannot_inject_receipt_fields_or_terminal_controls(self):
        detail = 'cherry-pick\u2028reason=forged\x1b[31m\x00\x85started=1'
        reason, bounded = C._route_guard_refusal(json.dumps(
            dict(status='blocked', reason='unsafe-git-operation', detail=detail)))
        self.assertEqual(reason, 'unsafe-git-operation')
        self.assertEqual(len(bounded.splitlines()), 1)
        self.assertNotIn('\x1b', bounded)
        self.assertNotIn('\x00', bounded)

    def test_guard_parser_rejects_mixed_output_and_non_ascii_reasons(self):
        refusal = dict(status='blocked', reason='unsafe-git-operation', detail='cherry-pick')
        generic = ('worker-route-validation-failed', 'guard output lacked a typed refusal')
        for value in [json.dumps(refusal)+'\nraw log',
                      json.dumps(refusal)+'\n'+json.dumps(refusal),
                      json.dumps({**refusal, 'reason': 'unsaf\u00e9-git-operation'})]:
            with self.subTest(value=value):
                self.assertEqual(C._route_guard_refusal(value), generic)

    def test_guard_parser_checks_exit_and_existing_identity_without_new_inputs(self):
        refusal = dict(status='blocked', reason='future-refusal', detail='future detail',
                       route_id='rt-other')
        generic = ('worker-route-validation-failed', 'guard output lacked a typed refusal')
        self.assertEqual(C._route_guard_refusal(json.dumps(refusal), returncode=1), generic)
        self.assertEqual(C._route_guard_refusal(json.dumps(refusal), route_id='rt-current'), generic)
        refusal['route_id'] = 'unknown'
        self.assertEqual(C._route_guard_refusal(json.dumps(refusal), route_id='rt-current'),
                         ('future-refusal', 'future detail'))

    def test_route_validation_receipt_uses_guard_reason(self):
        from unittest import mock
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            route = Path(temp) / 'route.json'
            route.write_text(json.dumps({'schema_version': 2}))
            args = argparse.Namespace(route_id='rt-fixture', route_hash='sha256:fixture',
                route_node='execute', registry_digest='fixture', write_scope='source/**',
                route_file=str(route), worktree=temp, artifact_root=temp,
                capability='autopilot-code', intensity='standard', unit='dev/backend',
                action='start', model_role='fast implementer', model_profile='balanced',
                attempt_id=None)
            result = subprocess.CompletedProcess([], 65, '',
                '{"status":"blocked","reason":"unsafe-git-operation","detail":"cherry-pick"}\n')
            out = io.StringIO()
            with mock.patch.object(C, 'validate_runtime_requirements'), \
                 mock.patch.object(C, 'validate_route_mode_axes'), \
                 mock.patch.object(C.subprocess, 'run', return_value=result), \
                 redirect_stdout(out):
                self.assertEqual(C.validate_route_record(args, marker_gate=lambda *_: None), 65)
            self.assertIn('reason=unsafe-git-operation\n', out.getvalue())
            self.assertIn('detail=cherry-pick\n', out.getvalue())
            self.assertNotIn('reason=worker-route-validation-failed', out.getvalue())

    def test_every_wrapper_keeps_the_old_names_as_the_shared_functions(self):
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            for old, new in ALIASES.items():
                with self.subTest(harness=harness, name=old):
                    self.assertIs(getattr(wrapper, old), getattr(C, new))

    def test_three_wrapper_route_start_settles_resource_and_readers_stay_read_only(self):
        import dispatch_contract as DC
        import dispatch_owner_input as INPUT
        import route_authority as RA
        from unittest import mock

        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture = FIX.TestSupervisorAdvance()
                fixture.setUp()
                lease_context = owner_input = None
                try:
                    route, route_file, jobs, registry, output = fixture._resume_fixture(ordinary=True)
                    supervisor = FIX.SUP
                    ledger = supervisor.ledger_for(route, jobs)
                    wrapper = load(harness)
                    owner_attempt = "att-owner-" + harness
                    owner_slug = "fixture-owner"
                    identity = supervisor.RR.proc_identity(os.getpid())
                    lease_path = DC.supervisor_lease_path(jobs, owner_attempt)
                    owner_metadata = {
                        "attempt_schema_version": "2", "dispatch_depth": "1",
                        "transport": "headless", "execution_surface": "registered-headless",
                        "registered_worker": "1", "fallback_hop": "same-harness-headless",
                        "worker_type": "owner", "unit": "_kernel/owner", "harness": "codex",
                        "runtime_sandbox": "workspace-write", "completion_delivery": "app-server-supervised",
                        "supervisor_lease": DC.SUPERVISOR_LEASE_KIND,
                        "supervisor_lease_file": str(lease_path),
                        "supervisor_lease_nonce": "d" * 64,
                        "attempt_id": owner_attempt, "pid": str(identity["pid"]),
                        "pid_start": identity["starttime"],
                        "owner_route_id": route["route_id"], "owner_route_hash": route["route_hash"],
                        "route_id": route["route_id"], "route_hash": route["route_hash"],
                        "parent_sid": "fixture-parent-session",
                        "capability": route["capability"], "capability_mode": route["capability_mode"],
                        "intensity": route["effective_intensity"],
                    }
                    owner_row = "\t".join(("2026-10-20T00:00:00Z", "open", str(fixture.base),
                        str(fixture.base), owner_slug,
                        ",".join(f"{key}={value}" for key, value in owner_metadata.items())))
                    with jobs.open("a", encoding="utf-8") as stream:
                        stream.write(owner_row + "\n")
                    lease_context = DC.hold_supervisor_lease(jobs, owner_attempt, lease_path)
                    lease_context.__enter__()
                    owner_input = INPUT.OwnerInput(
                        jobs, owner_attempt, "same-native", "codex-active-turn", lambda _: None,
                    )
                    resource_data = json.loads(registry.read_text())
                    resource_row = resource_data["runs"]["fixture-run"]
                    resource_row["parent_attempt_id"] = owner_attempt
                    resource_row["owner_wait"].update(
                        parent_attempt_id=owner_attempt, session_id="same-native",
                        route_id=route["route_id"], route_hash=route["route_hash"],
                        jobs=str(jobs.resolve()), owner_pid=str(identity["pid"]),
                        owner_start=identity["starttime"],
                    )
                    registry.write_text(json.dumps(resource_data))
                    armed_path = ledger.root / "armed/full-run.json"
                    armed = json.loads(armed_path.read_text())
                    armed["resource_binding"] = WAIT.resource_body_digest(resource_row)
                    armed_path.write_text(json.dumps(armed))
                    prepared = b'{"state":"prepared","runs":[{"status":"prepared"}]}\n'
                    (output / "run.json").write_bytes(prepared)
                    self.assertEqual(supervisor.poll_once(route, ledger)[0]["action"], "wait-next-resource")

                    node = next(row for row in route["nodes"] if row["id"] == "run-verify")
                    args = argparse.Namespace(
                        route_id=route["route_id"], route_hash=route["route_hash"],
                        route_node="run-verify", registry_digest=route["registry_digest"],
                        write_scope=";".join(node["write_scope"]), route_file=str(route_file),
                        worktree=str(fixture.base), artifact_root=str(route["artifact_root"]),
                        capability=route["capability"], capability_mode=route["capability_mode"],
                        intensity=route["effective_intensity"], unit=node["unit"],
                        model_role=node["role"], model_profile=node["model_profile"],
                        attempt_id="att-child-" + harness, jobs=jobs, agent_home=fixture.agent_home,
                        action="register", dispatch_depth=2, parent_slug=owner_slug,
                        parent_attempt_id=owner_attempt, parent_harness="codex",
                        parent_transport="headless", parent_sandbox="workspace-write",
                        subsession_id=None,
                    )
                    marker_path = jobs.parent / "completion" / route["route_id"] / "full-run.json"
                    def durable_snapshot():
                        paths = [ledger.journal_path, ledger.state_path, *sorted(ledger.claims_dir.glob("*.claim"))]
                        paths.append(marker_path)
                        return {str(path): path.read_bytes() if path.is_file() else None for path in paths}
                    before = durable_snapshot()

                    def fake_run(command, **kwargs):
                        return subprocess.CompletedProcess(command, 0, str(fixture.base) + "\n", "")

                    with mock.patch.object(C, "validate_runtime_requirements"), \
                         mock.patch.object(C, "validate_route_mode_axes"), \
                         mock.patch.object(C.subprocess, "run", side_effect=fake_run), \
                         mock.patch("subprocess.check_output", return_value=str(fixture.base)), \
                         mock.patch("review_input.preview_request_nodes", return_value=frozenset()), \
                         redirect_stdout(io.StringIO()):
                        self.assertEqual(wrapper.validate_route_record(args), 0)
                        self.assertEqual(durable_snapshot(), before)
                        args.action = "dry-run"
                        self.assertEqual(wrapper.validate_route_record(args), 65)
                        self.assertEqual(durable_snapshot(), before)
                        args.action = "start"
                        self.assertEqual(wrapper.validate_route_record(args), 0)
                        admitted_marker = marker_path.read_bytes()
                        admitted_claims = ledger.claim_history()
                        self.assertEqual(len(admitted_claims), 1)
                        self.assertEqual(wrapper.validate_route_record(args), 0)
                        self.assertEqual(marker_path.read_bytes(), admitted_marker)
                        self.assertEqual(ledger.claim_history(), admitted_claims)
                        self.assertIsNone(RA.completion_gate(
                            args, "start", fixture.agent_home, jobs,
                            gate=wrapper.completion_marker_gate,
                        ))
                        self.assertEqual(marker_path.read_bytes(), admitted_marker)
                        self.assertEqual(ledger.claim_history(), admitted_claims)

                        # This is the same read-only completion callback the
                        # wrapper's jobs-lock preclaim uses.
                        with Path(str(jobs) + ".lock").open("a", encoding="utf-8") as lock:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                            lines = jobs.read_text(encoding="utf-8").splitlines()
                            wrapper.completion_marker_gate(
                                args.route_file, args.route_node, "start", fixture.agent_home,
                                jobs, registry_lines=lines, attempt_id=args.attempt_id,
                            )
                        self.assertEqual(marker_path.read_bytes(), admitted_marker)
                        self.assertEqual(ledger.claim_history(), admitted_claims)
                        self.assertEqual((output / "run.json").read_bytes(), prepared)
                        self.assertEqual(ledger.state()["nodes"]["full-run"]["state"], "STAGE_SUCCEEDED")
                finally:
                    if owner_input is not None:
                        owner_input.close()
                    if lease_context is not None:
                        lease_context.__exit__(None, None, None)
                    fixture.doCleanups()

    def test_a_few_behaviors(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(C.fail("x-reason", 64, detail="d"), 64)
        self.assertEqual(out.getvalue(), "check=failed\nreason=x-reason\ndetail=d\n")
        self.assertEqual(C.process_start_ticks(-1), "")
        owner = argparse.Namespace(owner_route_binding=None, route_file="r", route_id="i",
                                   route_hash="h", route_node="one-shot")
        self.assertEqual(C.supervisor_route(owner), ("r", "i", "h"))
        owner.route_node = "execute"
        self.assertIsNone(C.supervisor_route(owner))
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            with C.jobs_lock(jobs):
                self.assertTrue(Path(f"{jobs}.lock").exists())


    def test_the_harness_name_is_the_only_translation(self):
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(C, "model_config_state", return_value=("s", "r")) as state:
                    self.assertEqual(wrapper._model_config_state(), ("s", "r"))
                state.assert_called_once_with(harness)
                args = argparse.Namespace()
                with mock.patch.object(C, "bind_internal_eligibility_probe") as probe:
                    wrapper.bind_internal_eligibility_probe(args)
                probe.assert_called_once_with(args, harness)

    def test_reexecs_between_sibling_releases_in_both_directions_for_each_harness(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            releases = Path(tmp) / "releases"
            releases.mkdir()
            release_a = releases / "vA"
            release_b = releases / "vB"
            self.make_release(release_a)
            self.make_release(release_b)
            argv = ["--start", "--slug", "space preserving value"]
            with mock.patch.dict(os.environ, {"AGENT_HOME": "/ambient/home"}, clear=False), \
                 mock.patch.object(C.os, "execv") as execv:
                for harness in ("claude", "codex", "opencode"):
                    for source, sealed in ((release_a, release_b), (release_b, release_a)):
                        with self.subTest(harness=harness, source=source.name, sealed=sealed.name):
                            self.assertTrue(C.reexec_for_sealed_release(
                                source, sealed, harness, argv
                            ))
                            self.assertEqual(os.environ["AGENT_HOME"], str(sealed.resolve()))
                            self.assertEqual(
                                execv.call_args,
                                mock.call(sys.executable, [
                                    sys.executable,
                                    str(sealed / "adapters" / harness / "bin" / "dispatch-headless.py"),
                                    *argv,
                                ]),
                            )
                self.assertEqual(execv.call_count, 6)

    def test_reexec_skips_same_root_checkouts_other_installs_missing_markers_and_targets(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            releases = root / "managed" / "releases"
            releases.mkdir(parents=True)
            release_a = releases / "vA"
            release_b = releases / "vB"
            self.make_release(release_a)
            self.make_release(release_b)
            other_releases = root / "other" / "releases"
            other_releases.mkdir(parents=True)
            other = other_releases / "vC"
            self.make_release(other)
            checkout = root / "checkout"
            checkout.mkdir()
            with mock.patch.object(C.os, "execv") as execv:
                self.assertFalse(C.reexec_for_sealed_release(release_a, release_a, "codex", []))
                self.assertFalse(C.reexec_for_sealed_release(checkout, release_b, "codex", []))
                self.assertFalse(C.reexec_for_sealed_release(release_a, other, "codex", []))
                marker = release_a / ".hearting-release.json"
                marker.unlink()
                self.assertFalse(C.reexec_for_sealed_release(release_a, release_b, "codex", []))
                marker.write_text('{"schema":1}\n', encoding="utf-8")
                (release_b / "adapters" / "codex" / "bin" / "dispatch-headless.py").unlink()
                self.assertFalse(C.reexec_for_sealed_release(release_a, release_b, "codex", []))
                self.assertFalse(C.reexec_for_sealed_release(release_a, release_b, "invalid", []))
                execv.assert_not_called()

    def test_real_execv_reaches_the_sealed_sibling_release_for_each_harness(self):
        import subprocess

        wrapper_template = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from dispatch_wrapper_common import reexec_for_sealed_release
root = Path(__file__).resolve().parents[3]
sealed = Path(os.environ["SEALED_LAUNCH_HOME"])
reexec_for_sealed_release(root, sealed, "{harness}", sys.argv[1:])
Path(os.environ["REEXEC_RECEIPT"]).write_text(json.dumps({{
    "root": str(root), "agent_home": os.environ.get("AGENT_HOME"),
    "argv": sys.argv[1:], "pid": os.getpid(),
}}), encoding="utf-8")
"""
        argv = ["--slug", "argument with spaces", "--start"]
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            releases = temp_root / "managed" / "releases"
            releases.mkdir(parents=True)
            release_a, release_b = releases / "vA", releases / "vB"
            self.make_release(release_a)
            self.make_release(release_b)
            for harness in ("claude", "codex", "opencode"):
                for release in (release_a, release_b):
                    wrapper = release / "adapters" / harness / "bin" / "dispatch-headless.py"
                    wrapper.write_text(wrapper_template.format(harness=harness), encoding="utf-8")
                for source, sealed in ((release_a, release_b), (release_b, release_a)):
                    with self.subTest(harness=harness, source=source.name, sealed=sealed.name):
                        receipt = temp_root / f"receipt-{harness}-{source.name}-{sealed.name}.json"
                        env = {
                            "PATH": os.environ.get("PATH", ""),
                            "HOME": str(temp_root / "home"),
                            "PYTHONPATH": str(ROOT / "utilities"),
                            "AGENT_HOME": str(source),
                            "SEALED_LAUNCH_HOME": str(sealed),
                            "REEXEC_RECEIPT": str(receipt),
                        }
                        process = subprocess.Popen(
                            [sys.executable, str(source / "adapters" / harness / "bin" /
                                               "dispatch-headless.py"), *argv],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                        )
                        stdout, stderr = process.communicate(timeout=10)
                        self.assertEqual(process.returncode, 0, stdout + stderr)
                        payload = json.loads(receipt.read_text(encoding="utf-8"))
                        self.assertEqual(payload["root"], str(sealed.resolve()))
                        self.assertEqual(payload["agent_home"], str(sealed.resolve()))
                        self.assertEqual(payload["argv"], argv)
                        self.assertEqual(payload["pid"], process.pid)

    def test_owner_input_opens_only_for_a_supervised_owner(self):
        from unittest import mock
        args = argparse.Namespace(attempt_id="att-1")
        with mock.patch("dispatch_owner_input.initialize_owner_input") as init:
            C.initialize_owner_input_when(args, Path("/j"), supervised=False, input_kind="k")
            init.assert_not_called()
            C.initialize_owner_input_when(args, Path("/j"), supervised=True, input_kind="k")
            init.assert_called_once_with(Path("/j"), "att-1", "k")

    def test_diff_attribution_reads_the_exact_registry_it_was_given(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "route.json"
            route.write_text(json.dumps({"nodes": [{"id": "test"}]}), encoding="utf-8")
            custom = Path(tmp) / "custom-registry.log"
            args = argparse.Namespace(route_file=str(route), route_node="test", jobs=str(custom),
                                      agent_home=Path(tmp))
            with mock.patch.object(C, "diff_attribution_lines", return_value=["diff_base: x"]) as lines:
                self.assertEqual(C.diff_attribution_prompt(args), "- diff_base: x\n")
            self.assertEqual(lines.call_args.args[2], custom)
            args.jobs = None
            with mock.patch.dict("os.environ", {}, clear=False), \
                 mock.patch.dict("os.environ", {"AGENT_DISPATCH_JOBS": ""}), \
                 mock.patch.object(C, "resolve_dispatch_state_root",
                                   side_effect=C.DispatchContractError("registry-ambiguous", "x")):
                self.assertEqual(C.diff_attribution_prompt(args), "")

    def test_the_reset_cache_is_best_effort(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "state" / "jobs.log"
            C.write_reset_cache(Path(tmp), "codex", "usage-limit", "12:00", jobs)
            self.assertTrue((jobs.parent / "usage-reset.codex").read_text().endswith(" usage-limit 12:00\n"))
            with mock.patch.object(C, "dispatch_state_roots",
                                   side_effect=C.DispatchContractError("registry-ambiguous", "x")):
                C.write_reset_cache(Path(tmp), "claude", "usage-limit", "12:00")  # no raise


class CloseJobRowTest(unittest.TestCase):
    def row(self, status="open", slug="slug", worktree="/wt", pipe="attempt_schema_version=2"):
        return f"2026-10-07T00:00:00Z\t{status}\t/repo\t{worktree}\t{slug}\t{pipe}\n"

    def test_the_slug_path_flips_the_open_row_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text(self.row(), encoding="utf-8")
            from unittest import mock
            materialize = mock.Mock()
            self.assertTrue(C.close_job_row(jobs, "slug", "/wt", "limit", "", materialize=materialize))
            line = jobs.read_text(encoding="utf-8")
            self.assertIn("\tdone\t", line)
            self.assertIn("note=dead-limit", line)
            materialize.assert_not_called()
            self.assertFalse(C.close_job_row(jobs, "slug", "/wt", "limit", "", materialize=materialize))

    def test_capacity_and_reset_ride_the_same_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text(self.row(), encoding="utf-8")
            from unittest import mock
            self.assertTrue(C.close_job_row(jobs, "slug", "/wt", "capacity", "3pm", materialize=mock.Mock()))
            line = jobs.read_text(encoding="utf-8")
            self.assertIn("failure_class=capacity,detected_by=anchored-early-exit", line)
            self.assertIn("reset=3pm", line)

    def test_every_wrapper_injects_its_own_materialize_at_call_time(self):
        # foreground_terminal_outcome patches the wrapper's
        # materialize_after_terminal_close in place; the delegation must look
        # it up when called, not when imported.
        import ast
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text(self.row(), encoding="utf-8")
            for harness in ("claude", "codex", "opencode"):
                wrapper = load(harness)
                tree = ast.parse((ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
                                 .read_text(encoding="utf-8"))
                delegation = next(n for n in tree.body
                                  if isinstance(n, ast.FunctionDef) and n.name == "close_job_row")
                with self.subTest(harness=harness):
                    self.assertIn("WRAPPER_COMMON.close_job_row", ast.unparse(delegation))
                    self.assertIn("materialize=materialize_after_terminal_close", ast.unparse(delegation))
                    with mock.patch.object(wrapper, "materialize_after_terminal_close") as delivery, \
                         mock.patch.object(C, "close_attempt_row", return_value=True):
                        self.assertTrue(wrapper.close_job_row(jobs, "s", "/w", "limit", "", "att-1"))
                    delivery.assert_called_once_with(jobs, "att-1")


class AttachSummaryOwnerTest(unittest.TestCase):
    def args(self, attempt_id="att-1", review=None):
        return argparse.Namespace(
            attempt_id=attempt_id,
            worktree="/worktree",
            worker_runtime_env={"CLAUDE_CONFIG_DIR": "/prepared/home"},
            review_output_binding=review,
            review_governed_lease_nonce="n" * 8,
        )

    def test_review_binding_rides_along_or_stays_empty(self):
        from unittest import mock
        launcher = mock.Mock(return_value={"summary_owner": "s"})
        identity = {"pid": "4242", "pid_start": "900"}
        result = C.attach_summary_owner(
            self.args(), Path("/t.jsonl"), Path("/p.txt"), identity,
            harness="codex", summary_launcher=launcher, runtime_home="/prepared/home")
        self.assertEqual(result, {"summary_owner": "s"})
        kwargs = launcher.call_args.kwargs
        self.assertEqual((kwargs["harness"], kwargs["target_pid"], kwargs["target_start"]),
                         ("codex", 4242, "900"))
        self.assertIsNone(kwargs["review_artifact_root"])
        self.assertEqual((kwargs["runtime_home"], kwargs["attempt_cwd"]),
                         ("/prepared/home", "/worktree"))
        review = {"artifact_root": "/a", "cycle_id": "cyc", "producer_id": "p"}
        C.attach_summary_owner(
            self.args(review=review), Path("/t.jsonl"), Path("/p.txt"), identity,
            harness="claude", summary_launcher=launcher)
        kwargs = launcher.call_args.kwargs
        self.assertEqual((kwargs["review_artifact_root"], kwargs["review_cycle_id"]),
                         ("/a", "cyc"))
        self.assertEqual(kwargs["review_lease_nonce"], "n" * 8)

    def test_every_wrapper_injects_its_launcher_at_call_time(self):
        from unittest import mock
        identity = {"pid": "4242", "pid_start": "900"}
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(wrapper, "launch_summary_owner", return_value={}) as launch:
                    wrapper.attach_summary_owner(self.args(), Path("/t"), Path("/p"), identity)
                self.assertEqual(launch.call_args.kwargs["harness"], harness)
                self.assertEqual(launch.call_args.kwargs["attempt_id"], "att-1")
class ValidateRouteRecordTest(unittest.TestCase):
    def args(self, tmp, **overrides):
        values = dict(
            route_id="rt-1", route_hash="sha256:x", route_node="execute",
            registry_digest="sha256:d", write_scope="source/**",
            route_file=str(Path(tmp) / "route.json"), worktree=str(tmp),
            artifact_root=str(tmp), capability="autopilot-code", intensity="standard",
            unit="_kernel/owner", action="register", model_role="", model_profile="",
            attempt_id="att-1", agent_home=Path(tmp), jobs=None,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_early_decisions_read_the_same_way(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            bare = self.args(tmp, route_file=None, route_id=None, route_hash=None,
                             route_node=None, registry_digest=None)
            self.assertEqual(C.validate_route_record(bare, marker_gate=lambda *a, **k: None), 0)
            out = io.StringIO()
            with redirect_stdout(out):
                code = C.validate_route_record(
                    self.args(tmp, route_file=None), marker_gate=lambda *a, **k: None)
            self.assertEqual(code, 65)
            self.assertIn("route-record-required", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                code = C.validate_route_record(
                    self.args(tmp, write_scope=None), marker_gate=lambda *a, **k: None)
            self.assertEqual(code, 65)
            self.assertIn("route-metadata-missing", out.getvalue())
            (Path(tmp) / "route.json").write_text('{"schema_version": 1}', encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = C.validate_route_record(self.args(tmp), marker_gate=lambda *a, **k: None)
            self.assertEqual(code, 65)
            self.assertIn("legacy-broker-route-read-only", out.getvalue())

    def test_every_wrapper_injects_its_marker_gate_at_call_time(self):
        # sd45 and review_input patch the wrapper's completion_marker_gate;
        # the delegation must look it up when called, not when imported. The
        # admission itself asks route_authority at call time too (PR #299
        # review), so patching those names reaches the shared call.
        import ast
        import route_authority
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "route.json").write_text(
                json.dumps({"schema_version": 2, "nodes": []}), encoding="utf-8")
            for harness in ("claude", "codex", "opencode"):
                wrapper = load(harness)
                delegation = next(
                    n for n in ast.parse((ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
                                         .read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "validate_route_record")
                with self.subTest(harness=harness):
                    self.assertIn("WRAPPER_COMMON.validate_route_record", ast.unparse(delegation))
                    self.assertIn("marker_gate=completion_marker_gate", ast.unparse(delegation))
                    with mock.patch.object(wrapper, "completion_marker_gate") as marker, \
                         mock.patch.object(C, "validate_runtime_requirements"), \
                         mock.patch.object(C, "validate_route_mode_axes"), \
                         mock.patch.object(route_authority, "completion_gate", return_value=None) as completion, \
                         mock.patch.object(route_authority, "prelaunch_registry", return_value=Path("/j/jobs.log")), \
                         mock.patch.object(C, "subprocess") as proc:
                        proc.run.return_value.returncode = 0
                        proc.run.return_value.stdout = "{}"
                        proc.run.return_value.stderr = ""
                        rc = wrapper.validate_route_record(self.args(tmp))
                    self.assertEqual(rc, 0)
                    self.assertIn("--launch-phase", proc.run.call_args.args[0])
                    self.assertIs(completion.call_args.kwargs["gate"], marker)


class ParentCompletionTest(unittest.TestCase):
    def test_every_wrapper_injects_its_parent_hooks_at_call_time(self):
        # dispatch_parent_completion.test and the sd45 suites patch the
        # wrapper's probe/launch/annotate names; the delegations must look
        # them up when called, not when imported.
        from unittest import mock
        import dispatch_parent_completion as parent_completion
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                args = argparse.Namespace()
                with mock.patch.object(wrapper, "probe_managed_codex_parent") as probe, \
                     mock.patch.object(parent_completion, "resolve_parent_completion_delivery",
                                       return_value="poll-fallback") as resolve:
                    self.assertEqual(wrapper.resolve_parent_completion_delivery(args), "poll-fallback")
                    self.assertIs(resolve.call_args.kwargs["probe"], probe)
                bound = argparse.Namespace()
                with mock.patch.object(wrapper, "probe_managed_codex_parent") as probe, \
                     mock.patch.object(parent_completion, "resolve_parent_completion_delivery",
                                       return_value="codex-native-queue") as resolve:
                    wrapper.bind_parent_completion_delivery(bound)
                    self.assertEqual(bound.parent_completion_delivery, "codex-native-queue")
                    self.assertIs(resolve.call_args.kwargs["probe"], probe)
                with mock.patch.object(wrapper, "launch_codex_queue_completion_sidecar") as launch, \
                     mock.patch.object(wrapper, "annotate_attempt_row") as annotate, \
                     mock.patch.object(parent_completion, "launch_parent_completion_sidecar") as sidecar:
                    wrapper.launch_parent_completion_sidecar(args, Path("/tmp/jobs"))
                    self.assertIs(sidecar.call_args.kwargs["launch"], launch)
                    self.assertIs(sidecar.call_args.kwargs["annotate"], annotate)
class ReviewLeaseTest(unittest.TestCase):
    def args(self, lifecycle):
        return argparse.Namespace(
            review_output="/out.md",
            review_output_binding={"cycle_id": "cyc", "output_path": "/out.md"},
            watchdog_budget=60, attempt_id="att-1",
            review_governed_lease_nonce="n" * 8, artifact_root="/art",
            launch_lifecycle=lifecycle, review_watchdog_handle=object(),
        )

    def test_every_wrapper_injects_its_admission_hooks_at_call_time(self):
        # reviewer_report_binding patches the wrapper's acquire_* and witness
        # names; the delegation must look them up when called, not imported.
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            for lifecycle, name in (
                (wrapper.DETACHED, "acquire_review_admission"),
                (wrapper.FOREGROUND_SCOPED, "acquire_foreground_review_admission"),
            ):
                with self.subTest(harness=harness, admission=name):
                    with mock.patch.object(wrapper, name, return_value="admission") as acquire:
                        result = wrapper.acquire_review_lease_after_claim(
                            self.args(lifecycle), Path("/tmp/jobs"),
                            {"pid": "1", "pid_start": "2"})
                    self.assertEqual(result, "admission")
                    self.assertIs(acquire.call_args.kwargs["lease_acquire"], wrapper.review_lease_acquire)
                    self.assertIs(acquire.call_args.kwargs["budget"], self.args(lifecycle).watchdog_budget)
                    # The witness probe resolves the wrapper's name when it is
                    # called (the reviewer_report_binding pattern).
                    with mock.patch.object(wrapper, "review_governed_lease_is_held", return_value=True):
                        self.assertFalse(acquire.call_args.kwargs["witness_probe"]())
class ModelPolicyTest(unittest.TestCase):
    def test_model_policy_reads_the_wrappers_harness_and_error(self):
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(C, "resolve_config", return_value=({"a": "b"}, None)) as resolve:
                    self.assertEqual(wrapper._model_policy(), {"a": "b"})
                self.assertEqual(resolve.call_args.args[0], harness)
                with mock.patch.object(C, "resolve_config", side_effect=C.ModelConfigError("x")):
                    with self.assertRaises(wrapper.ModelSelectionError) as caught:
                        wrapper._model_policy()
                self.assertEqual(caught.exception.reason, "dispatch-model-policy-unavailable")

    def test_require_headless_model_uses_the_wrappers_policy_and_error(self):
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(C, "headless_model_refusal", return_value=("r", "m")):
                    with self.assertRaises(wrapper.ModelSelectionError) as caught:
                        wrapper._require_headless_model("m", "src")
                self.assertEqual((caught.exception.reason, caught.exception.args[0]), ("r", "m"))
                with mock.patch.object(wrapper, "_model_policy", return_value={}) as policy, \
                     mock.patch.object(C, "headless_model_refusal", return_value=None):
                    wrapper._require_headless_model("m", "src")
                policy.assert_called_once_with()

    def test_main_session_only_model_uses_the_wrappers_policy(self):
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(wrapper, "_model_policy", return_value={"a": "b"}) as policy, \
                     mock.patch.object(C, "main_session_only_models", return_value="m") as models, \
                     mock.patch.object(C, "restricted_model", return_value=True) as restricted:
                    self.assertTrue(wrapper._main_session_only_model("m"))
                policy.assert_called_once_with()
                self.assertEqual(models.call_args.args, ({"a": "b"},))
                self.assertEqual(restricted.call_args.args, ("m", "m"))

    def test_main_session_only_policy_state_uses_the_wrappers_policy_and_error(self):
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(wrapper, "_model_policy", return_value={}) as policy, \
                     mock.patch.object(C, "main_session_only_state", return_value="declared"):
                    self.assertEqual(wrapper._main_session_only_policy_state(), "declared")
                policy.assert_called_once_with()
                with mock.patch.object(wrapper, "_model_policy",
                                       side_effect=wrapper.ModelSelectionError("r", "x")) as policy:
                    self.assertEqual(wrapper._main_session_only_policy_state(), "unavailable")
                policy.assert_called_once_with()


class CompletionPolicyTest(unittest.TestCase):
    def test_owner_classification_preserves_depth_type_and_intensity(self):
        for harness in ("claude", "codex"):
            wrapper = load(harness)
            self.assertIs(wrapper._registered_owner, C.registered_owner)
            for depth in (0, 1, 2):
                for worker_type in ("owner", "stage", "review", "support"):
                    for intensity in ("direct", "quick", "standard", "strong", "thorough", "adversarial"):
                        args = argparse.Namespace(dispatch_depth=depth, worker_type=worker_type, intensity=intensity)
                        with self.subTest(harness=harness, depth=depth, worker_type=worker_type, intensity=intensity):
                            owner = depth == 1 and worker_type == "owner"
                            self.assertEqual(wrapper._registered_owner(args), owner)
                            self.assertEqual(wrapper._completion_owner(args), owner and intensity in {
                                "standard", "strong", "thorough", "adversarial"})

    def test_completion_owner_looks_up_the_wrappers_classifier_at_call_time(self):
        from unittest import mock
        for harness in ("claude", "codex"):
            wrapper = load(harness)
            with self.subTest(harness=harness), \
                 mock.patch.object(wrapper, "_registered_owner", return_value=False) as registered:
                args = argparse.Namespace()  # no intensity access after the false classifier
                self.assertFalse(wrapper._completion_owner(args))
                registered.assert_called_once_with(args)

    def test_async_policy_preserves_deny_dependency_and_supervised_short_circuit(self):
        from unittest import mock
        wrapper = load("claude")
        for delivery in (None, "one-shot", "poll-fallback", "session-resume-supervised"):
            for denied in ((), ("Monitor",)):
                args = argparse.Namespace()
                if delivery is not None:
                    args.resolved_completion_delivery = delivery
                with self.subTest(delivery=delivery, denied=denied), \
                     mock.patch.object(wrapper, "_async_deny_tools", return_value=denied) as deny:
                    expected = ("runtime-supervised" if delivery == "session-resume-supervised"
                                else "deny-proven" if denied else "unsupported")
                    self.assertEqual(wrapper._async_wait_policy(args), expected)
                    if delivery == "session-resume-supervised":
                        deny.assert_not_called()
                    else:
                        deny.assert_called_once_with(args)

    def test_completion_state_remains_below_the_exact_registry_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "custom-state" / "jobs.log"
            for harness in ("claude", "codex"):
                wrapper = load(harness)
                self.assertIs(wrapper.completion_state_path, C.completion_state_path)
                for attempt in (None, "", "att-exact_1.2-3"):
                    args = argparse.Namespace(jobs_path=jobs, attempt_id=attempt)
                    filename = f"{attempt}.json" if attempt else "preview-only.json"
                    with self.subTest(harness=harness, attempt=attempt):
                        self.assertEqual(wrapper.completion_state_path(args),
                                         jobs.parent / "supervisor-state" / filename)
                for attempt in ("att-../../outside", "att-", "foreign", "att-" + "x" * 241):
                    with self.subTest(harness=harness, attempt=attempt), \
                         self.assertRaises(C.DispatchContractError) as error:
                        wrapper.completion_state_path(argparse.Namespace(jobs_path=jobs, attempt_id=attempt))
                    self.assertEqual(error.exception.reason, "completion-state-attempt-invalid")
            self.assertFalse(jobs.parent.exists())  # path selection does not write state


class RegistrationFenceTest(unittest.TestCase):
    def test_every_wrapper_registers_behind_the_terminal_claim_fence(self):
        import ast
        shared = next(n for n in ast.parse((ROOT / "utilities" / "dispatch_wrapper_common.py").read_text(encoding="utf-8")).body
                      if isinstance(n, ast.FunctionDef) and n.name == "append_job")
        claims = [n for n in ast.walk(shared) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "claim"]
        self.assertEqual(len(claims), 1)
        self.assertIn("mutation_precheck", {k.arg for k in claims[0].keywords})
        self.assertIn("ensure_terminal_claim_absent", ast.unparse(shared))
        for harness in ("claude", "codex", "opencode"):
            tree = ast.parse((ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text(encoding="utf-8"))
            append_job = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "append_job")
            with self.subTest(harness=harness):
                self.assertIn("WRAPPER_COMMON.append_job(", ast.unparse(append_job))


class SameRowTest(unittest.TestCase):
    """The same launch writes the same row on every harness, apart from the
    values each adapter declares as its own."""

    def args(self, tmp: Path):
        from types import SimpleNamespace
        settings = {"source": "role", "role": "deep maker", "profile": "unsealed", "tier": "deep",
                    "granularity": "legacy", "model": "m", "effort": "high", "reasoning": "high",
                    "variant": "high"}
        return argparse.Namespace(
            worktree=str(tmp), repository_identity="/repo", capability="autopilot-code", capability_mode="dev", qa="standard",
            intensity="standard", dispatch_depth=1, execution_surface="registered-headless",
            registered_worker=1, fallback_hop="same-harness-headless", parent_slug=None,
            parent_binding=None, parent_session_id=None, worker_role=None, worker_mode=None,
            worker_type="owner", launch_lifecycle_resolution=SimpleNamespace(metadata=lambda: {}),
            assigned_contract="autopilot-code", unit=None, review_output=None, capability_owner=None,
            owner_harness=None, route_file=None, route_id=None, route_hash=None, route_node=None,
            registry_digest=None, write_scope=None, completion_gate=None, harness_affinity=None,
            explicit_adapter=None, resolved_model_settings=settings,
            resolved_completion_delivery="session-resume-supervised", completion_delivery_reason="ok",
            parent_completion_delivery="poll-fallback", parent_completion_reason="fixture",
            artifact_root=str(tmp), log_path=str(tmp / "log"), agent_home=str(tmp), attempt_id="att-1",
            launch_authority="conductor", fallback_ordinal=0, capacity_retry=0, broker_request_id=None,
            action="register", slug="fixture",
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def row(self, harness, repository_kind=None, **kwargs):
        from unittest import mock
        claimed = []
        tmp = self.tmp
        with \
             mock.patch.object(C.subprocess, "check_output", return_value="/repo\n"), \
             mock.patch.object(C, "sealed_launch_home", return_value="/home"), \
             mock.patch.object(C, "workflow_completion_receipt", return_value=""), \
             mock.patch.object(C, "stage_session_metadata", return_value=""), \
             mock.patch.object(C, "route_node_leg_fields", return_value=("-", "-")), \
             mock.patch.object(C, "secrets") as secrets_, \
             mock.patch("review_input.registration_fragment", return_value=""), \
             mock.patch("dispatch_replacement.seal_launch_input", return_value=""):
            secrets_.token_hex.return_value = "nonce"
            args = self.args(Path(tmp))
            if repository_kind is not None:
                args.repository_kind = repository_kind
            C.append_job(Path(tmp) / "jobs.log", args, harness=harness,
                         claim=lambda jobs, attempt, row, **kw: claimed.append((row, kw)) or True,
                         marker_gate=None, **kwargs)
        row, kw = claimed[0]
        self.assertIn("mutation_precheck", kw)
        fields = dict(part.split("=", 1) for part in row.split("\t")[5].split(","))
        return fields

    def test_repository_marker_is_only_added_for_non_git_rows(self):
        non_git_fields = self.row("codex", repository_kind="non-git",
                                  runtime_sandbox="workspace-write", effort_key="reasoning")
        self.assertEqual(non_git_fields["repository"], "non-git")

    def test_only_the_declared_translation_differs(self):
        rows = {
            "claude": self.row("claude", runtime_sandbox="adapter-default", effort_key="effort",
                               adapter_fields=",permission_mode=bypass"),
            "codex": self.row("codex", runtime_sandbox="workspace-write", effort_key="reasoning",
                              adapter_fields=",approval=never"),
            "opencode": self.row("opencode", runtime_sandbox="adapter-default", effort_key="variant"),
        }
        own = {"harness", "runtime_sandbox", "effort", "reasoning", "variant", "permission_mode",
               "approval", "supervisor_lease_file"}
        common = [{k: v for k, v in fields.items() if k not in own} for fields in rows.values()]
        self.assertEqual(common[0], common[1])
        self.assertEqual(common[0], common[2])
        self.assertEqual(rows["opencode"]["completion_delivery"], "session-resume-supervised")
        self.assertEqual(rows["codex"]["reasoning"], "high")
        self.assertEqual(rows["claude"]["supervisor_lease_nonce"], "nonce")

    def test_launch_records_all_or_no_main_runtime_fields(self):
        from unittest import mock
        with mock.patch.object(C, "_retention_main_identity",
                               return_value=("codex", "431", "900", "pid:[4026531836]")):
            fields = self.row("codex", runtime_sandbox="workspace-write", effort_key="reasoning")
        self.assertNotIn("repository", fields)
        self.assertEqual((fields["main_runtime_harness"], fields["main_runtime_pid"],
                          fields["main_runtime_pid_start"], fields["main_runtime_ns"]),
                         ("codex", "431", "900", "pid:[4026531836]"))
        with mock.patch.object(C, "_retention_main_identity", return_value=None):
            absent = self.row("claude", runtime_sandbox="adapter-default", effort_key="effort")
        self.assertFalse(any(key.startswith("main_runtime_") for key in absent))

    def test_nested_main_runtime_identity_follows_only_exact_parent_attempts(self):
        from types import SimpleNamespace
        from unittest import mock
        jobs = Path(self.tmp) / "jobs.log"
        parent = "att-parent"
        root = "att-root"
        jobs.write_text(
            "2026-01-01T00:00:00Z\tdone\t/repo\t/wt\troot\tattempt_id=att-root,dispatch_depth=1,main_runtime_harness=claude,main_runtime_pid=21,main_runtime_pid_start=32,main_runtime_ns=pid:[1]\n"
            "2026-01-01T00:00:00Z\tdone\t/repo\t/wt\tchild\tattempt_id=att-parent,parent_attempt_id=att-root,dispatch_depth=2\n",
            encoding="utf-8")
        args = SimpleNamespace(dispatch_depth=3, parent_binding=SimpleNamespace(attempt_id=parent))
        with mock.patch.object(C, "_interactive_main_identity", side_effect=AssertionError):
            self.assertEqual(C._retention_main_identity(jobs, args, "opencode"),
                             ("claude", "21", "32", "pid:[1]"))
        jobs.write_text(jobs.read_text().replace("parent_attempt_id=att-root", "parent_attempt_id=att-missing"))
        self.assertIsNone(C._retention_main_identity(jobs, args, "opencode"))

    def test_shared_start_path_checks_projection_before_seal_and_spawn(self):
        from unittest import mock

        args = self.args(Path(self.tmp))
        args.action = "start"
        args.route_file = None
        jobs = Path(self.tmp) / "fenced.jobs.log"
        claimed = []
        with mock.patch.object(C.commit_policy, "assert_launch_permission_projection",
                               side_effect=[None, C.DispatchContractError(
                                   "runtime-permission-projection-changed",
                                   "axis=git_operation predecessor=None candidate=merge child_spawned=0")]) as check, \
             mock.patch.object(C.subprocess, "check_output", return_value="/repo\n"), \
             mock.patch.object(C, "sealed_launch_home", return_value="/home"), \
             mock.patch.object(C, "workflow_completion_receipt", return_value=""), \
             mock.patch.object(C, "stage_session_metadata", return_value=""), \
             mock.patch.object(C, "route_node_leg_fields", return_value=("-", "-")), \
             mock.patch.object(C, "secrets") as secrets_, \
             mock.patch("review_input.registration_fragment", return_value=""), \
             mock.patch("dispatch_replacement.seal_launch_input", return_value=""):
            secrets_.token_hex.return_value = "nonce"
            C.append_job(jobs, args, harness="codex", runtime_sandbox="workspace-write",
                         effort_key="reasoning", claim=lambda *a, **kw: claimed.append(kw) or True,
                         marker_gate=None, replacement_sandbox="workspace-write")
            self.assertEqual(check.call_count, 1)
            with self.assertRaises(C.DispatchContractError) as refused:
                claimed[0]["preclaim"]([])
            self.assertIn("child_spawned=0", refused.exception.detail)
            self.assertEqual(check.call_count, 2)

if __name__ == "__main__":
    unittest.main()
