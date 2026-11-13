#!/usr/bin/env python3
"""Preserved nonzero exits can finish only through current same-byte verification."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("resolution_fixture", HERE / "workflow_supervisor.test.py")
FIX = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIX
spec.loader.exec_module(FIX)
SUP, WS = FIX.SUP, FIX.WS
import resource_failure_resolution as R
import dispatch_resource_wait as WAIT
from split_module_fixture import patch_split


class FailedOutputResolutionTest(FIX.WorkflowFixture):
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture
    _settle_resource_owner = FIX.TestSupervisorAdvance._settle_resource_owner

    def fixture(self):
        supervisor_patch = mock.patch.object(WAIT, 'supervisor', return_value=SUP)
        supervisor_patch.start()
        self.addCleanup(supervisor_patch.stop)
        route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        resource = copy.deepcopy(WS.route_node(route, "full-run"))
        resource["depends_on"] = []
        route["nodes"] = [resource,
            {"id": "metrics", "kind": "pipeline-stage", "depends_on": ["full-run"],
             "outputs": ["summary.json", "metrics.jsonl"], "completion_gate": "metrics"},
            {"id": "verify", "kind": "review-worker", "depends_on": ["metrics"],
             "completion_gate": "independent-verify", "terminal_gate": "independent-verify", "terminal": True}]
        path.write_text(json.dumps(route))
        data = json.loads(registry.read_text())
        row = data["runs"]["fixture-run"]
        row.update(status="failed", exit_code=1, workflow_state="FAILED_RETRYABLE",
                   config_sha256="a" * 64, parent_attempt_id="att-parent")
        Path(row["sentinel"]).write_text("1")
        registry.write_text(json.dumps(data))
        ledger = SUP.ledger_for(route, jobs)
        arm_path = ledger.root / "armed/full-run.json"
        armed = json.loads(arm_path.read_text())
        armed["resource_binding"] = WAIT.resource_body_digest(row)
        arm_path.write_text(json.dumps(armed))
        raw = output / "raw.json"
        raw.write_text('{"scientific_verdict":"FAIL","data":[1,2,3]}')
        receipt = output / "run.json"
        receipt.write_text(json.dumps({"route_id": route["route_id"], "exit": 1,
            "config_sha256": row["config_sha256"], "outputs": [{"path": "raw.json", "sha256": R.digest(raw)}]}))
        correction = output / "correction.json"
        correction.write_text(json.dumps({"route_id": route["route_id"], "node": "full-run",
            "gate_evidence_verdict": "PASS", "artifact_sha256": {"raw.json": R.digest(raw)}}))
        sidecar = output / "metrics.jsonl"
        sidecar.write_text(json.dumps({"evidence_path": str(raw), "input_sha256": R.digest(raw)}) + "\n")
        summary = output / "summary.json"
        summary.write_text(json.dumps({"run_sha256": R.digest(receipt), "metrics_sha256": R.digest(sidecar)}))
        verdict = output / "verdict.json"
        verdict.write_text(json.dumps({"verdict": "PASS", "evidence_hashes": {"summary.json": R.digest(summary)}}))
        directory = SUP.route_module().completion_dir(route["route_id"], jobs=jobs)
        directory.mkdir(parents=True, exist_ok=True)
        for node, evidence in ((resource, correction), (route["nodes"][1], summary), (route["nodes"][2], verdict)):
            marker = {"schema_version": 2, "sequence": 1, "registry_digest": route["registry_digest"],
                      "route_id": route["route_id"], "route_hash": route["route_hash"],
                      "node_id": node["id"], "completion_gate": node["completion_gate"],
                      "registered_worker": False, "evidence": {"path": str(evidence), "sha256": R.digest(evidence)}}
            if node["id"] == "verify":
                marker["review_independence"] = "independent"
            (directory / (node["id"] + ".json")).write_text(json.dumps(marker))
            (directory / (node["id"] + ".1.json")).write_text(json.dumps(marker))
        evidence = SUP.resource_evidence(armed)
        ledger.record("full-run", "FAILED_RETRYABLE", evidence=evidence)
        ledger.set_workflow_state("FAILED_RETRYABLE", evidence={"node": "full-run"})
        gates = {"verify": {"passed": True}}
        # Dispatch authority is tested by the shared marker suites. Here the
        # real identity/evidence-currency reader still detects modified markers
        # and bytes, while fixture-only native dispatch receipts are omitted.
        module = SUP.route_module()
        reader = module._marker_identity_row
        patch = patch_split(module, "_marker_identity_row", side_effect=lambda *a, **kw:
            reader(*a, **{**kw, "exact_terminal": False}))
        patch.start()
        self.addCleanup(patch.stop)
        return route, path, jobs, registry, output, ledger, gates

    def owner_fixture(self):
        route, path, jobs, registry, _output = self._resume_fixture(ordinary=True)
        data = json.loads(registry.read_text(encoding="utf-8"))
        data["runs"]["fixture-run"]["parent_attempt_id"] = "att-parent"
        registry.write_text(json.dumps(data), encoding="utf-8")
        ledger = SUP.ledger_for(route, jobs)
        armed = json.loads((ledger.root / "armed/full-run.json").read_text(encoding="utf-8"))
        return route, path, jobs, registry, ledger, armed

    def owner_row(self, jobs, route, path, *, status="done", attempt="att-parent",
                  note="", failure_class=""):
        import dispatch_contract
        lease = dispatch_contract.supervisor_lease_path(jobs, attempt)
        metadata = [
            "attempt_schema_version=2", "dispatch_depth=1", "transport=headless",
            "execution_surface=registered-headless", "registered_worker=1",
            "worker_type=owner", "harness=codex", "completion_delivery=app-server-supervised",
            "supervisor_lease=flock-v1", f"supervisor_lease_file={lease}",
            "supervisor_lease_nonce=" + "b" * 64, "launch_started=1",
            f"attempt_id={attempt}", f"owner_route_id={route['route_id']}",
            f"owner_route_hash={route['route_hash']}", f"owner_route_file={path}",
            f"route_id={route['route_id']}", f"route_hash={route['route_hash']}",
            f"route_file={path}",
        ]
        if note:
            metadata.append(f"note={note}")
        if failure_class:
            metadata.append(f"failure_class={failure_class}")
        jobs.write_text("\t".join(["2026-10-25T00:00:00Z", status,
            route["cwd"], route["cwd"], "fixture-owner", ",".join(metadata)]) + "\n")
        return lease

    def advanced_replacement_owner_fixture(self):
        import artifact_lifecycle
        import dispatch_owner_input
        import dispatch_replacement as DR
        import owner_route_binding as ORB

        root, _old_path, jobs, registry, ledger, _old_armed = self.owner_fixture()
        root_path = artifact_lifecycle.canonical_route_path(self.base, root["route_id"])
        root_path.parent.mkdir(parents=True, exist_ok=True)
        root_path.write_text(json.dumps(root), encoding="utf-8")

        parent_id = "att-parent"
        node_id = root["nodes"][0]["id"]
        logical = {"root_route_id": root["route_id"], "node": "__owner__"}
        family = DR._digest(logical)
        replacement_id = "att-" + hashlib.sha256(
            ("replacement:" + family).encode()).hexdigest()[:48]
        owner_id = replacement_id
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": owner_id}):
            active = SUP.route_module().build_continuation_route(
                root, resume_from_node=node_id, requested_boundary=node_id,
                reason="resource-failure", artifact_root=str(self.base))
        active_path = artifact_lifecycle.canonical_route_path(self.base, active["route_id"])
        active_path.parent.mkdir(parents=True, exist_ok=True)
        active_path.write_text(json.dumps(active), encoding="utf-8")

        record = {"schema": DR.SCHEMA, "family_id": family, "logical_node": logical,
                  "original_attempt_id": parent_id, "replacement_attempt_id": replacement_id,
                  "ordinal": 1}
        record_path = DR._record_path(jobs, family)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record), encoding="utf-8")

        parent_metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "1",
            "registered_worker": "1", "execution_surface": "registered-headless",
            "transport": "headless", "worker_type": "owner", "unit": "_kernel/owner",
            "attempt_id": parent_id, "parent_sid": "fixture-parent-session",
            "parent_attempt_id": "", "harness": "codex", "owner_harness": "codex",
            "launch_started": "1", "capability": root["capability"],
            "capability_mode": root["capability_mode"], "artifact_root": str(self.base),
            "owner_route_file": str(root_path), "owner_route_id": root["route_id"],
            "owner_route_hash": root["route_hash"], "route_file": str(root_path),
            "route_id": root["route_id"], "route_hash": root["route_hash"],
            "replacement_family_id": family, "replacement_attempt_id": replacement_id,
            "replacement_claim_digest": DR._digest(record),
        }
        current_metadata = {
            **parent_metadata, "attempt_id": owner_id, "automatic_retry_of": parent_id,
            "replacement_original_attempt_id": parent_id,
        }

        def line(status, attempt, metadata):
            return "\t".join(("2026-10-25T00:00:00Z", status, str(self.base),
                              str(self.base), attempt, ",".join(
                                  f"{key}={value}" for key, value in metadata.items())))

        parent_metadata.update(note="dead-worker-blocked", failure_class="blocked")
        jobs.write_text(line("done", parent_id, parent_metadata) + "\n"
                        + line("open", owner_id, current_metadata) + "\n", encoding="utf-8")
        dispatch_owner_input.initialize_owner_input(jobs, owner_id, "codex-active-turn")

        source = ORB.OwnerRouteBinding(str(root_path), root["route_id"], root["route_hash"])
        target = ORB.OwnerRouteBinding(str(active_path), active["route_id"], active["route_hash"])
        ORB.publish_owner_route_advance(
            jobs, owner_attempt_id=owner_id, source=source, target=target,
            from_generation=0, to_generation=1,
            route_family_key=active["route_family_key"])
        child_metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "2",
            "registered_worker": "1", "execution_surface": "registered-headless",
            "attempt_id": "att-current-child", "parent_attempt_id": owner_id,
            "worker_type": "stage", "route_node": node_id,
            "route_file": str(active_path), "route_id": active["route_id"],
            "route_hash": active["route_hash"], "capability": active["capability"],
            "capability_mode": active["capability_mode"],
            "artifact_root": active["artifact_root"], "launch_started": "1",
        }
        current_metadata.update(note="dead-worker-blocked", failure_class="blocked")
        jobs.write_text(line("done", parent_id, parent_metadata) + "\n"
                        + line("done", owner_id, current_metadata) + "\n"
                        + line("open", "att-current-child", child_metadata) + "\n",
                        encoding="utf-8")

        data = json.loads(registry.read_text(encoding="utf-8"))
        data["runs"]["fixture-run"].update(route=str(active_path), parent_attempt_id=parent_id)
        registry.write_text(json.dumps(data), encoding="utf-8")
        armed = json.loads((ledger.root / "armed/full-run.json").read_text(encoding="utf-8"))
        armed["route_file"] = str(active_path)
        return root, root_path, active, active_path, jobs, registry, ledger, armed, owner_id

    def close_route(self, route, path):
        path.with_name(path.stem + ".outcome.json").write_text(json.dumps({
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "disposition": "fixture-closed",
        }))

    def run_correct_pipe(self, step, payload):
        tokens = shlex.split(step["command"])
        right = tokens[tokens.index("|") + 1:]
        probe = '''import importlib.util, sys
script = sys.argv[1]
spec = importlib.util.spec_from_file_location("retry_correct_probe", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._continue_after_answer = lambda _jobs, _attempt, result: result
sys.argv = [script, *sys.argv[2:]]
module.main()
'''
        return subprocess.run([right[0], "-c", probe, *right[1:],], input=payload,
                              text=True, capture_output=True, timeout=30)

    def test_original_failure_blocks_then_same_bytes_close_without_rewriting_exit_or_artifacts(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        before = ledger.journal_path.read_bytes()
        protected = {p: p.read_bytes() for p in [registry, *output.glob("*"),
                     *SUP.route_module().completion_dir(route["route_id"], jobs=jobs).glob("*.json")]}
        with self.assertRaises(WS.WorkflowStateError):
            ledger.complete(["verify"], gates)
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.result, commits), ("completed", 1), result)
        self.assertEqual(ledger.state()["workflow_state"], "COMPLETE")
        self.assertTrue(ledger.journal_path.read_bytes().startswith(before))
        self.assertEqual(ledger.claims(), {})
        for p, raw in protected.items():
            self.assertEqual(p.read_bytes(), raw)
        proof = ledger.state()["nodes"]["full-run"]["evidence"]["resolved_resource_failure"]
        self.assertEqual(proof["original_failure"]["exit_code"], 1)
        self.assertEqual(proof["independent_nodes"], ["verify"])
        self.assertEqual(len(proof["consumed_artifacts"]), 1)
        journal = ledger.journal_path.read_bytes()
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, "completed")
        self.assertEqual(ledger.journal_path.read_bytes(), journal)

    def test_changed_output_keeps_failure_and_returns_an_existing_executable_next_leg(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        (output / "raw.json").write_text('{"data":[9]}')
        before = ledger.journal_path.read_bytes(), registry.read_bytes()
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual(result.reason, "resource-failure-unresolved")
        self.assertEqual(commits, 0)
        self.assertEqual((ledger.journal_path.read_bytes(), registry.read_bytes()), before)
        self.assertIn("capability-route.py start --route", result.next_step["command"])
        self.assertEqual(result.next_step["run_id"], "fixture-run__a1")
        self.assertEqual(result.next_step["revalidate_nodes"], ["metrics", "verify"])
        self.assertEqual(result.next_step["continuation"], "resume")

    def test_blocked_owner_gets_prefilled_correction_and_the_pipe_is_retained(self):
        import dispatch_owner_input
        import dispatch_replacement as DR

        route, path, jobs, _registry, ledger, armed = self.owner_fixture()
        self.owner_row(jobs, route, path, status="open")
        dispatch_owner_input.initialize_owner_input(jobs, "att-parent", "codex-active-turn")
        self.owner_row(jobs, route, path, status="done", note="dead-worker-blocked")
        resource_row = json.loads(_registry.read_text())["runs"]["fixture-run"]
        completion = SUP.route_module().completion_dir(route["route_id"], jobs=jobs)
        protected = [path, jobs, _registry, Path(resource_row["sentinel"]),
                     ledger.journal_path, *completion.glob("*.json"),
                     dispatch_owner_input._path(jobs, "att-parent")]
        before = {item: item.read_bytes() for item in protected}
        with mock.patch.object(DR, "owner_parked_gate", return_value=None):
            step = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual({item: item.read_bytes() for item in protected}, before)
        self.assertEqual(step["continuation"], "correct")
        self.assertIn("correct --jobs", step["command"])
        self.assertIn("--attempt-id att-parent --message-file /dev/stdin", step["command"])
        self.assertIn("같은 route", step["meaning"])
        payload = shlex.split(step["command"])[2]
        result = self.run_correct_pipe(step, payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["retained"])
        self.assertEqual(dispatch_owner_input.retained(jobs, "att-parent")[0]["text"], payload)

    def test_live_owner_gets_correction_and_the_pipe_is_queued(self):
        import dispatch_contract
        import dispatch_owner_input
        import dispatch_replacement as DR
        import route_parent_close
        from types import SimpleNamespace

        route, path, jobs, _registry, ledger, armed = self.owner_fixture()
        lease = self.owner_row(jobs, route, path, status="running")
        dispatch_owner_input.initialize_owner_input(jobs, "att-parent", "codex-active-turn")
        with dispatch_contract.hold_supervisor_lease(jobs, "att-parent", lease):
            with mock.patch.object(DR, "owner_parked_gate", return_value=None), \
                    mock.patch.object(route_parent_close, "owner_continuation_processes",
                                      return_value=(SimpleNamespace(state="live", reason="fixture"), [])):
                step = R.retry_step(route, armed, jobs, "changed output")
            self.assertEqual(step["continuation"], "correct")
            payload = shlex.split(step["command"])[2]
            result = self.run_correct_pipe(step, payload)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["requests"][0]["state"], "queued")
        self.assertEqual(dispatch_owner_input.retained(jobs, "att-parent")[0]["text"], payload)

    def test_retained_answer_and_unobserved_owner_resume_the_open_route(self):
        import dispatch_owner_input
        import dispatch_replacement as DR

        route, path, jobs, _registry, ledger, armed = self.owner_fixture()
        self.owner_row(jobs, route, path, status="open")
        dispatch_owner_input.initialize_owner_input(jobs, "att-parent", "codex-active-turn")
        self.owner_row(jobs, route, path, status="done", note="dead-worker-blocked")
        dispatch_owner_input.submit(jobs, "att-parent", "answer already stored", "fixture-answer")
        with mock.patch.object(DR, "owner_parked_gate", return_value=None):
            answered = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(answered["continuation"], "resume")
        self.assertIn("capability-route.py start --route", answered["command"])

        jobs.write_text("")
        unobserved = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(unobserved["continuation"], "resume")
        self.assertIn("capability-route.py start --route", unobserved["command"])

    def test_other_owner_receipt_states_resume_and_checked_projection_selects_current_owner(self):
        import dispatch_replacement as DR
        import route_authority
        from types import SimpleNamespace

        route, path, jobs, _registry, ledger, armed = self.owner_fixture()
        self.owner_row(jobs, route, path, status="running")
        for state in ("continue", "wait", "blocked", "protected", "settle"):
            with self.subTest(state=state), \
                    mock.patch.object(route_authority, "decide_continuation",
                                      return_value=SimpleNamespace(state=state)):
                step = R.retry_step(route, armed, jobs, "changed output")
                self.assertEqual(step["continuation"], "resume")
                self.assertIn("capability-route.py start --route", step["command"])

        self.owner_row(jobs, route, path, status="done", attempt="att-current",
                       note="dead-worker-blocked")
        with mock.patch.object(DR, "effective_attempts", return_value=({"att-current"}, [])) as project, \
                mock.patch.object(DR, "owner_parked_gate", return_value=None):
            current = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(project.call_args_list[0].args, (jobs, {"att-parent"}))
        self.assertEqual(current["continuation"], "correct")
        self.assertIn("--attempt-id att-current --message-file /dev/stdin", current["command"])

    def test_route_path_falls_back_from_watch_to_failure_row_and_canonical_path(self):
        import artifact_lifecycle

        route, path, jobs, registry, _output, ledger, _gates = self.fixture()
        armed = json.loads((ledger.root / "armed/full-run.json").read_text())
        armed.pop("route_file", None)
        from_failure = R.retry_step(route, armed, jobs, "changed output")
        self.assertIn(str(path), from_failure["command"])

        canonical = artifact_lifecycle.canonical_route_path(Path(route["artifact_root"]), route["route_id"])
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(json.dumps(route))
        data = json.loads(registry.read_text())
        data["runs"]["fixture-run"]["route"] = ""
        registry.write_text(json.dumps(data))
        from_canonical = R.retry_step(route, armed, jobs, "changed output")
        self.assertIn("--route " + route["route_id"], from_canonical["command"])
        self.assertNotIn(str(path), from_canonical["command"])

    def test_supplied_priority_route_mismatch_does_not_fall_back_to_closed_duplicate(self):
        route, _path, jobs, registry, output, ledger, _gates = self.fixture()
        armed = json.loads((ledger.root / "armed/full-run.json").read_text())
        priority = output / "contradictory-route.json"
        priority.write_text(json.dumps({"route_id": "rt-other", "route_hash": "sha256:other"}))
        closed_fallback = output / "closed-fallback-route.json"
        closed_fallback.write_text(json.dumps(route))
        self.close_route(route, closed_fallback)
        data = json.loads(registry.read_text())
        data["runs"]["fixture-run"]["route"] = str(closed_fallback)
        registry.write_text(json.dumps(data))
        armed["route_file"] = str(priority)

        step = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(step["continuation"], "resume-unobservable")
        self.assertIn("capability-route.py start --route", step["command"])
        self.assertNotIn("compose --start", step["command"])

    def test_advanced_route_replacement_targets_unanswered_blocked_current_owner(self):
        import dispatch_owner_input
        import dispatch_replacement as DR
        import owner_route_binding as ORB

        root, root_path, active, active_path, jobs, registry, _ledger, armed, owner_id = \
            self.advanced_replacement_owner_fixture()
        effective, lineage = DR.effective_attempts(jobs, {"att-parent"})
        self.assertEqual(effective, {owner_id})
        self.assertEqual(lineage[0]["replacement_attempt_id"], owner_id)
        self.assertEqual(dispatch_owner_input.retained(jobs, owner_id), [])
        binding, binding_status = ORB.resolve_owner_route_lifecycle(
            jobs, owner_attempt_id=owner_id)
        self.assertEqual(binding_status, "owner-route-advance-current")
        self.assertEqual((binding.route_id, binding.route_hash),
                         (active["route_id"], active["route_hash"]))

        for stored_route, stored_path in ((active, active_path), (root, root_path)):
            with self.subTest(stored_route=stored_route["route_id"]):
                armed["route_file"] = str(stored_path)
                data = json.loads(registry.read_text(encoding="utf-8"))
                data["runs"]["fixture-run"]["route"] = str(stored_path)
                registry.write_text(json.dumps(data), encoding="utf-8")
                step = R.retry_step(stored_route, armed, jobs, "changed output")
                self.assertEqual(step["continuation"], "correct")
                self.assertIn(f"--attempt-id {owner_id} --message-file /dev/stdin", step["command"])
                self.assertIn(active["route_id"], shlex.split(step["command"])[2])

        self.assertEqual(SUP.route_module()._current_owner_attempt(jobs, "att-parent"), owner_id)
        payload = shlex.split(step["command"])[2]
        result = self.run_correct_pipe(step, payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["retained"])

    def test_exact_closed_route_composes_and_invalid_outcome_resumes(self):
        route, path, jobs, _registry, ledger, armed = self.owner_fixture()
        self.close_route(route, path)
        closed = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(closed["continuation"], "closed-compose")
        self.assertIn("capability-route compose --start --shape staged", closed["command"])

        path.with_name(path.stem + ".outcome.json").write_text(json.dumps({
            "route_id": route["route_id"], "route_hash": "sha256:wrong",
        }))
        invalid = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(invalid["continuation"], "resume-unobservable")
        self.assertIn("capability-route.py start --route", invalid["command"])

        path.with_name(path.stem + ".outcome.json").write_text("{")
        malformed = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(malformed["continuation"], "resume-unobservable")
        with mock.patch("dispatch_notice_state.closed_outcome", return_value={"finish_pending": True}):
            pending = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(pending["continuation"], "resume")

    def test_exact_cancelled_closure_resumes_with_and_without_an_owner(self):
        route, path, jobs, registry, ledger, armed = self.owner_fixture()
        outcome = path.with_name(path.stem + ".outcome.json")
        outcome.write_text(json.dumps({
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "state": "cancelled", "disposition": "cancelled",
            "terminal_gate_proven": False,
        }))
        for owner_state in ("missing", "blocked", "pass"):
            with self.subTest(owner_state=owner_state):
                jobs.write_text("")
                if owner_state == "blocked":
                    self.owner_row(jobs, route, path, note="dead-worker-blocked")
                elif owner_state == "pass":
                    self.owner_row(jobs, route, path, failure_class="pass")
                protected = [path, jobs, registry, outcome, ledger.journal_path]
                before = {item: item.read_bytes() for item in protected}

                step = R.retry_step(route, armed, jobs, "changed output")

                self.assertEqual(step["continuation"], "resume-protected")
                self.assertIn("capability-route.py start --route", step["command"])
                self.assertNotIn("compose --start", step["command"])
                self.assertEqual({item: item.read_bytes() for item in protected}, before)

    def test_only_a_proven_pass_settlement_uses_the_agreed_compose_exception(self):
        import dispatch_replacement as DR
        import route_parent_close
        from types import SimpleNamespace

        route, path, jobs, _registry, ledger, armed = self.owner_fixture()
        self.owner_row(jobs, route, path, status="done", failure_class="pass")
        with mock.patch.object(DR, "owner_parked_gate", return_value=None), \
                mock.patch.object(route_parent_close, "owner_continuation_processes",
                                  return_value=(SimpleNamespace(state="quiescent", reason="fixture"), [])):
            passed = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(passed["continuation"], "pass-settlement-compose")
        self.assertIn("capability-route compose --start --shape staged", passed["command"])

        self.owner_row(jobs, route, path, status="done")
        with mock.patch.object(DR, "owner_parked_gate", return_value=None), \
                mock.patch.object(route_parent_close, "owner_continuation_processes",
                                  return_value=(SimpleNamespace(state="quiescent", reason="fixture"), [])):
            unknown_done = R.retry_step(route, armed, jobs, "changed output")
        self.assertEqual(unknown_done["continuation"], "resume")
        self.assertIn("capability-route.py start --route", unknown_done["command"])

    def test_independent_pass_and_current_marker_without_consumer_hash_are_insufficient(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        summary = output / "summary.json"
        summary.write_text('{"owner_says":"PASS"}')
        marker = SUP.route_module().completion_dir(route["route_id"], jobs=jobs) / "metrics.json"
        data = json.loads(marker.read_text())
        data["evidence"]["sha256"] = R.digest(summary)
        marker.write_text(json.dumps(data))
        marker.with_name('metrics.1.json').write_text(json.dumps(data))
        verdict = output / 'verdict.json'
        verdict.write_text(json.dumps({'verdict': 'PASS', 'summary_sha256': R.digest(summary)}))
        self.update_marker(route, jobs, 'verify', verdict)
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ("resource-failure-unresolved", 0))
        self.assertIn("same-byte-consumer-binding-absent", result.detail)

    def test_retry_pipe_delivers_the_original_task_to_compose(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        self.close_route(route, path)
        armed = json.loads((ledger.root / 'armed/full-run.json').read_text())
        tokens = shlex.split(R.retry_step(route, armed, jobs, 'changed output')['command'])
        separator = tokens.index('|')
        task = tokens[2]
        # Exercise the real CLI argument/task reader with a one-shot stdin pipe;
        # stop at composition so this test cannot create or dispatch a route.
        probe = '''import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("retry_compose_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._resolve_compose_plan = lambda *args: (None, None)
module._compose_git_diagnosis = lambda cwd: {"state": "git", "root": str(cwd)}
def capture(**kwargs):
    print(json.dumps(kwargs["work_request"]["text"]))
    raise SystemExit(0)
module.compose_route = capture
sys.argv = [sys.argv[1], *sys.argv[2:]]
module.main()
'''
        result = subprocess.run([sys.executable, '-c', probe, str(HERE / 'capability-route.py'),
                                 *tokens[separator + 4:]], input=task, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), task)
        self.assertIn('fixture-run__a1', task)

    def test_retry_recovery_shows_completed_compute_candidate_as_unverified(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        self.close_route(route, path)
        armed = json.loads((ledger.root / 'armed/full-run.json').read_text())
        with mock.patch('route_parent_close.linked_resources', return_value=[
                {"kind": "compute", "run_id": "compute-candidate", "state": "finished"}]) as scan:
            task = R.retry_step(route, armed, jobs, 'resource failed')['command']
        scan.assert_called_once()
        self.assertIn("compute-candidate", task)
        self.assertIn("unverified", task)
        self.assertIn("resource-runner start --compute-run compute-candidate", task)
        self.assertNotIn("--compute-run latest", task)

    def test_same_route_correction_keeps_completed_compute_candidate_unverified(self):
        import dispatch_replacement as DR

        route, path, jobs, _registry, _ledger, armed = self.owner_fixture()
        self.owner_row(jobs, route, path, note="dead-worker-blocked")
        with mock.patch.object(DR, "owner_parked_gate", return_value=None), \
                mock.patch('route_parent_close.linked_resources', return_value=[
                    {"kind": "compute", "run_id": "compute-candidate", "state": "finished"}]):
            step = R.retry_step(route, armed, jobs, 'resource failed')
        self.assertEqual(step["continuation"], "correct")
        self.assertIn("--attempt-id att-parent --message-file /dev/stdin", step["command"])
        task = shlex.split(step["command"])[2]
        self.assertIn("unverified", task)
        self.assertIn("resource-runner start --compute-run compute-candidate", task)
        self.assertNotIn("--compute-run latest", task)

    def test_retry_of_a_retry_gets_a_distinct_identity_with_a_missing_registry(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        armed = json.loads((ledger.root / 'armed/full-run.json').read_text())
        armed['predecessor_id'] = 'fixture-run__a1'
        armed['resource_registry'] = str(output / 'missing-registry.json')
        step = R.retry_step(route, armed, jobs, 'missing registry')
        self.assertEqual(step['run_id'], 'fixture-run__a2')
        registry.write_text(json.dumps({'runs': {'fixture-run__a2': {}}}))
        armed['resource_registry'] = str(registry)
        self.assertEqual(R.retry_step(route, armed, jobs, 'changed bytes')['run_id'], 'fixture-run__a3')

    def test_owner_override_and_stale_consumer_cannot_resolve(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        directory = SUP.route_module().completion_dir(route["route_id"], jobs=jobs)
        marker = directory / "verify.json"
        data = json.loads(marker.read_text())
        data["review_independence"] = "owner-overridden"
        marker.write_text(json.dumps(data))
        marker.with_name('verify.1.json').write_text(json.dumps(data))
        self.assertIn("independent-pass-absent", self._settle_resource_owner(route, path, jobs)[0].detail)
        data["review_independence"] = "independent"
        marker.write_text(json.dumps(data))
        marker.with_name('verify.1.json').write_text(json.dumps(data))
        journal = ledger.journal_path.read_bytes()
        (output / "summary.json").write_text('{}')
        # Marker currency or the bound-byte reader may reject this first,
        # depending on the existing gate policy; both keep the failure intact.
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
        self.assertEqual(ledger.journal_path.read_bytes(), journal)

    def update_marker(self, route, jobs, node, evidence):
        directory = SUP.route_module().completion_dir(route['route_id'], jobs=jobs)
        path = directory / (node + '.json')
        data = json.loads(path.read_text())
        data['evidence']['sha256'] = R.digest(evidence)
        for name in (node + '.json', node + '.1.json'):
            (directory / name).write_text(json.dumps(data))

    def test_independent_pass_without_current_consumer_admission_cannot_resolve(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        verdict = output / 'verdict.json'
        verdict.write_text('{"verdict":"PASS"}')
        self.update_marker(route, jobs, 'verify', verdict)
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
        self.assertIn('same-byte-consumer-binding-absent', result.detail)

    def test_consumer_change_after_marker_validation_is_rejected_without_appending(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        before = ledger.journal_path.read_bytes()
        module = SUP.route_module()
        reader = module._marker_identity_row
        def swap(*args, **kwargs):
            result = reader(*args, **kwargs)
            if args[2] == 'metrics':
                (output / 'summary.json').write_text('{"owner_says":"PASS"}')
            return result
        with patch_split(module, '_marker_identity_row', side_effect=swap):
            result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
        self.assertIn('bound-artifact-changed', result.detail)
        self.assertEqual(ledger.journal_path.read_bytes(), before)

    def test_unequal_expected_actual_is_not_an_admitted_hash(self):
        self.assertNotIn('a' * 64, R.hashes({'expected': 'a' * 64, 'actual': 'b' * 64, 'match': False}))

    def test_missing_or_damaged_watch_keeps_failure_and_names_the_original_resource(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        armed = ledger.root / 'armed/full-run.json'
        original = armed.read_bytes()
        before = ledger.journal_path.read_bytes()
        for value in ({}, [], None, {'node': 'other', 'predecessor_kind': 'resource'}):
            with self.subTest(value=value):
                armed.write_text(json.dumps(value))
                result, commits = self._settle_resource_owner(route, path, jobs)
                self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
                self.assertEqual(result.next_step['run_id'], 'fixture-run__a1')
                self.assertIn('capability-route.py start --route', result.next_step['command'])
                self.assertEqual(ledger.journal_path.read_bytes(), before)
        armed.unlink()
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].reason, 'resource-failure-unresolved')
        armed.write_bytes(original)
        for value in (None, [], 1):
            registry.write_text(json.dumps({'runs': value}))
            result, commits = self._settle_resource_owner(route, path, jobs)
            self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
            self.assertEqual(result.next_step['run_id'], 'fixture-run__a1')
            self.assertEqual(ledger.journal_path.read_bytes(), before)

    def test_interrupted_resolution_resumes_each_append_and_preserves_failure_prefix(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        before = ledger.journal_path.read_bytes()
        append = WS.WorkflowLedger._append
        for target in ("READY", "RUNNING", "STAGE_SUCCEEDED"):
            def interrupt(instance, entry):
                append(instance, entry)
                if entry.get("node") == "full-run" and entry.get("state") == target:
                    raise OSError("interrupted after append")
            with mock.patch.object(WS.WorkflowLedger, "_append", interrupt):
                self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, "recoverable")
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, "completed")
        self.assertTrue(ledger.journal_path.read_bytes().startswith(before))
        self.assertEqual([e["state"] for e in ledger.journal() if e.get("node") == "full-run"][-4:],
                         ["FAILED_RETRYABLE", "READY", "RUNNING", "STAGE_SUCCEEDED"])


if __name__ == "__main__":
    unittest.main()
