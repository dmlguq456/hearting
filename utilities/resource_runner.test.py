#!/usr/bin/env python3
"""CLI regression tests for the detached resource authorization boundary."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CLEAN_ENV = {k: v for k, v in os.environ.items() if not (
    k in {"AGENT_HOME", "AGENT_DISPATCH_JOBS"}
    or k.startswith("AGENT_DISPATCH_") or k.startswith("AGENT_ROUTE_")
    or k.startswith("AGENT_REVIEW_") or k.startswith("AGENT_ARTIFACT_")
)}
CLEAN_ENV["AGENT_HOME"] = str(ROOT)
CLEAN_ENV["CUDA_VISIBLE_DEVICES"] = ""  # authorization/lifecycle fixtures run CPU payloads
for _key in tuple(os.environ):
    if _key.startswith(("AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_ARTIFACT_")):
        os.environ.pop(_key, None)
RUNNER = ROOT / "utilities" / "resource-runner.py"
ROUTER = ROOT / "utilities" / "capability-route.py"
SMOKE = ROOT / "tools" / "smoke-attestation.py"
spec = importlib.util.spec_from_file_location("runner", RUNNER)
R = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(R)


class ComputeRunAdoptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.registry = self.root / "resource-runs.json"
        self.sentinel = self.root / "old.exit"
        self.sentinel.write_text("1\n")
        self.workflow = self.root / "workflow"
        self.workflow.mkdir()
        self.journal = self.workflow / "journal.jsonl"
        self.journal.write_bytes(b'{"event":"prior-history"}\n')
        self.marker = self.jobs.parent / "completion" / "rt-adopt" / "full-run.json"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_bytes(b'{"marker":"prior"}\n')
        self.claim = self.workflow / "claims" / "successor.json"
        self.claim.parent.mkdir()
        self.claim.write_bytes(b'{"claim":"prior"}\n')
        self.output = self.root / "cycle"
        self.output.mkdir()
        (self.output / "run.json").write_text('{"existing":"science artifact"}\n')
        self.compute_root = self.root / "compute"
        self.compute_root.mkdir()
        self.compute_run = self.compute_root / "compute-1"
        self.compute_run.mkdir()
        self.compute_config = self.root / "compute-hosts.yaml"
        self.attestation = self.root / "smoke-attestation.json"
        self.attestation.write_text('{"fixture":true}\n')
        attestation_sha = hashlib.sha256(self.attestation.read_bytes()).hexdigest()
        self.route = {"route_id": "rt-adopt", "route_hash": "sha256:route",
                      "capability": "autopilot-lab", "artifact_root": str(self.root),
                      "cwd": str(self.root), "nodes": [{"id": "full-run", "kind": "resource-runner",
                          "outputs": ["run.json"], "continuation": {"kind": "supervised"}}]}
        self.route_file.write_text(json.dumps(self.route))
        self.sealed = {"command": ["compute-hosts", "run", "gpu", "--cwd", "/lab",
                                    "--env", "env-a", "--", "python", "train.py"],
                       "cwd": str(self.root), "config_ref": "cfg", "config_sha256": "c" * 64,
                       "source_commit": "commit-a", "source_dirty": False,
                       "source_git_state": "clean", "config_layout": "layout-a",
                       "launch_request": {"smoke_attestation": str(self.attestation),
                           "smoke_attestation_sha256": attestation_sha}}
        self.original = {"run_id": "full-run", "route": str(self.route_file), "node": "full-run",
                         "jobs": str(self.jobs), "resource_policy": "supervised-owner",
                         "parent_attempt_id": "att-original", "status": "failed", "exit_code": 1,
                         "sentinel": str(self.sentinel), "resource_binding": None, **self.sealed}
        self.registry.write_text(json.dumps({"schema_version": 1, "runs": {"full-run": self.original}}))
        self.owner_wait = {"parent_attempt_id": "att-owner", "session_id": "sid-owner",
                           "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                           "jobs": str(self.jobs), "owner_pid": 123, "owner_start": "456"}
        self.parent_contract = {"route_id": self.route["route_id"], "route_file": str(self.route_file),
                                "route_hash": self.route["route_hash"], "node": "full-run",
                                "run_id": "full-run", "attempt_id": "att-original", "sealed": self.sealed,
                                "artifact_base": str(self.output), "declared_outputs": ["run.json"],
                                "inputs": [{"name": "smoke_attestation", "path": str(self.attestation),
                                    "sha256": attestation_sha, "verified": True}]}
        self.provenance = {"run_id": "full-run", "registry": str(self.registry),
                           "route_id": self.route["route_id"], "route_file": str(self.route_file),
                           "route_hash": self.route["route_hash"], "node": "full-run",
                           "parent_attempt_id": "att-original", "contract": self.parent_contract}
        self.contract = {"command": ["python", "train.py"], "cwd": "/lab", "env": "env-a", "share": False,
                         "resource_parent": self.parent_contract,
                         "route": {"route_id": self.route["route_id"],
                                   "route_file": str(self.route_file),
                                   "route_hash": self.route["route_hash"]}}
        self.execution = {"kind": "compute-hosts", "run_id": "compute-1",
                          "run_root": str(self.root), "host": "gpu", "meta_sha256": "m" * 64,
                          "exit_sha256": "e" * 64, "receipt_sha256": "r" * 64,
                          "outputs_sha256": "o" * 64,
                          "contract_sha256": "c" * 64, "contract": self.contract,
                          "receipt_state": "finished", "identity": {"pid": 9},
                          "outputs": [{"path": "run.json", "sha256": hashlib.sha256(
                              (self.output / "run.json").read_bytes()).hexdigest(),
                              "state": "present", "size": (self.output / "run.json").stat().st_size}],
                          "provenance": self.provenance, "started_at": 1.0, "ended_at": 2.0}
        self.armed = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                      "route_file": str(self.route_file), "node": "full-run",
                      "predecessor_kind": "resource", "predecessor_id": "full-run",
                      "resource_registry": str(self.registry), "jobs": str(self.jobs),
                      "artifact_base": str(self.output), "resource_binding": "body"}

    def _write_real_compute_fixture(self):
        output_raw = (self.output / "run.json").read_bytes()
        output_row = {"path": "run.json", "sha256": hashlib.sha256(output_raw).hexdigest(),
                      "state": "present", "size": len(output_raw)}
        self.compute_config.write_text(
            f"schema_version: 1\nrun_root: {self.compute_root}\nhosts:\n"
            "  gpu:\n    ssh_host: fixture-host\n")
        self.compute_run.joinpath("meta.json").write_text(json.dumps({
            "run_id": "compute-1", "host": "gpu", "execution_contract": self.contract,
            "contract_sha256": hashlib.sha256(json.dumps(
                self.contract, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "provenance": {"resource": self.provenance},
        }))
        self.compute_run.joinpath("exit_code").write_text("0\n")
        self.compute_run.joinpath("launch-receipt.json").write_text(json.dumps({
            "schema_version": 1, "run_id": "compute-1", "host": "fixture-host",
            "state": "finished", "exit_code": 0,
            "identity": {"pid": 9, "starttime": "11", "process_group": 9,
                         "pid_namespace": "pid:[fixture]", "boot_id": "fixture-boot"},
            "started_at": 1.0, "ended_at": 2.0,
        }))
        self.compute_run.joinpath("outputs.json").write_text(json.dumps({
            "schema_version": 1, "run_id": "compute-1", "outputs": [output_row],
        }))
        execution_identity = "compute-hosts:compute-1:" + ":".join(
            hashlib.sha256(self.compute_run.joinpath(name).read_bytes()).hexdigest()
            for name in ("meta.json", "exit_code", "launch-receipt.json")) + ":" + json.loads(
                self.compute_run.joinpath("meta.json").read_text())["contract_sha256"]
        marker = json.dumps({"resource_execution_identity": execution_identity}) + "\n"
        self.marker.write_text(marker)
        self.claim.write_text(marker)

    def _mutate_real_compute_output(self):
        output_raw = b'{"changed":true}\n'
        (self.output / "run.json").write_bytes(output_raw)
        outputs = {"schema_version": 1, "run_id": "compute-1", "outputs": [{
            "path": "run.json", "sha256": hashlib.sha256(output_raw).hexdigest(),
            "state": "present", "size": len(output_raw),
        }]}
        self.compute_run.joinpath("outputs.json").write_text(json.dumps(outputs))

    def _run_adoption(self, *, cwd="/lab", owner_status="open", target="target",
                      route_closed=False, binding_conflict=False, replay=False,
                      interrupt_before_arm=False, late_conflict=None,
                      real_compute=False, mutate_outputs_before_replay=False,
                      missing_outputs_hash=False, lineage=False, route_free=False):
        import contextlib
        import resource_run_registry as RR
        import dispatch_resource_wait as OWNER_RESOURCE
        # Current resource bindings retain the digest of the legacy reader's
        # projection. A placeholder used by the original fixture no longer
        # represents an armed row and masks the adoption/refusal paths below.
        source_file = self.root / "source-route.json"
        source = {"route_id": "rt-source", "route_hash": "sha256:source",
                  "artifact_root": str(self.root), "cwd": str(self.root), "nodes": []}
        if lineage or route_free:
            if lineage:
                source_file.write_text(json.dumps(source))
                for document in (self.provenance, self.parent_contract):
                    document.update(route_id=source["route_id"],
                                    route_hash=source["route_hash"],
                                    route_file=str(source_file))
                self.original["route"] = str(source_file)
                self.registry.write_text(json.dumps({"schema_version": 1,
                                                     "runs": {"full-run": self.original}}))
            else:
                for document in (self.provenance, self.parent_contract):
                    document.update(route_id=None, route_hash=None, route_file=None)
        self.armed["resource_binding"] = OWNER_RESOURCE.resource_legacy_body_digest(self.original)
        fake_ledger = SimpleNamespace(lock=contextlib.nullcontext,
                                      root=self.workflow,
                                      state=lambda: ({"workflow_state": "CANCELLED",
                                          "nodes": {"full-run": {"state": "READY"}}}
                                          if late_conflict == "stage-state" else
                                          {"workflow_state": "RUNNING",
                                           "nodes": {"full-run": {"state": "RUNNING"}}}),
                                      claims=lambda: {}, journal=lambda: [])
        fake_ledger.root.mkdir(exist_ok=True)
        armed_reads = 0
        def read_armed(_ledger):
            nonlocal armed_reads
            armed_reads += 1
            if late_conflict == "armed-row" and armed_reads > 1:
                return {"full-run": {**self.armed, "route_hash": "changed-route-hash"}}
            return {"full-run": self.armed}
        def resource_predecessors(*_args):
            if not real_compute:
                return [(self.armed, self.original)]
            row = json.loads(self.registry.read_text())["runs"].get(self.armed["predecessor_id"])
            return [(self.armed, row)] if row is not None else []

        fake_sup = SimpleNamespace(
            ledger_for=lambda *_args: fake_ledger,
            read_armed=read_armed,
            resource_predecessors=resource_predecessors,
            resource_continuation_cancelled=lambda *_args: route_closed,
        )
        fake_compute = SimpleNamespace(load_config=lambda: {"run_root": self.root, "hosts": {}})
        def validate_fake_outputs(base, declared, rows):
            expected_paths = list(declared)
            actual_paths = ([row.get("path") for row in rows if isinstance(row, dict)]
                            if isinstance(rows, list) else None)
            if actual_paths != expected_paths:
                return {"ok": False, "reason": "compute-run-output-membership-changed",
                        "differences": [{"field": "outputs.paths", "expected": expected_paths,
                                         "actual": actual_paths}], "retryable": False,
                        "next_action": "verify the selected compute output set"}
            for row in rows:
                target = Path(base) / row["path"]
                try:
                    raw = target.read_bytes()
                except OSError:
                    return {"ok": False, "reason": "compute-run-output-missing",
                            "differences": [{"field": "outputs." + row["path"],
                                             "expected": "present", "actual": "missing"}],
                            "retryable": False,
                            "next_action": "verify the selected compute output files"}
                actual = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                expected = {"sha256": row.get("sha256"), "size": row.get("size")}
                if actual != expected:
                    return {"ok": False, "reason": "compute-run-output-changed",
                            "differences": [{"field": "outputs." + row["path"],
                                             "expected": expected, "actual": actual}],
                            "retryable": False,
                            "next_action": "restore the original compute output before replaying"}
            return {"ok": True}
        fake_compute.validate_compute_outputs = validate_fake_outputs
        def fake_execution_record(*_args):
            execution = {**self.execution, "contract": {**self.contract, "cwd": cwd}}
            if missing_outputs_hash:
                execution.pop("outputs_sha256", None)
            return {"state": "quiescent", "execution": execution,
                    "run_dir": str(self.compute_run)}
        fake_compute.execution_record = fake_execution_record
        def run_snapshot_current(*_args):
            if late_conflict == "publication":
                current = json.loads(self.registry.read_text())
                current["runs"]["full-run"]["exit_code"] = 7
                self.registry.write_text(json.dumps(current))
                self.raced_registry = self.registry.read_bytes()
            return True
        fake_compute.run_snapshot_current = run_snapshot_current
        loader = SimpleNamespace(exec_module=lambda _module: None)
        fake_inputs = SimpleNamespace(_locked=lambda *_args: contextlib.nullcontext(),
                                      _target=lambda *_args: (SimpleNamespace(status=owner_status), target))
        if binding_conflict:
            self.armed["resource_binding"] = "wrong-binding"
        fake_artifacts = SimpleNamespace(prepare_route_artifact_env=lambda *_args, **_kwargs: {
            "AGENT_ARTIFACT_OUTPUT_DIR": str(self.output)})
        if real_compute:
            self._write_real_compute_fixture()
            real_spec_from_file_location = importlib.util.spec_from_file_location
            real_module_from_spec = importlib.util.module_from_spec
            self.remote_call = mock.Mock(return_value=SimpleNamespace(
                returncode=0, stdout='{"state":"empty","reason":"fixture-remote-empty"}\n'))
            remote_call = self.remote_call

            def real_compute_spec(name, location, *positional, **keywords):
                spec = real_spec_from_file_location(name, location, *positional, **keywords)
                if spec and Path(location).name == "compute-hosts.py":
                    source_loader = spec.loader

                    class ObserverBoundaryLoader:
                        def create_module(self, module_spec):
                            create = getattr(source_loader, "create_module", None)
                            return create(module_spec) if create else None

                        def exec_module(self, module):
                            source_loader.exec_module(module)
                            real_observer = module.observe_run_quiescence

                            def observe_with_fixture_remote(config, run_id, expected=None):
                                return real_observer(config, run_id, expected=expected,
                                                     remote_call=remote_call)

                            module.observe_run_quiescence = observe_with_fixture_remote

                    spec.loader = ObserverBoundaryLoader()
                return spec

            spec_factory = mock.patch("importlib.util.spec_from_file_location",
                                      side_effect=real_compute_spec)
            module_factory = mock.patch("importlib.util.module_from_spec",
                                        side_effect=real_module_from_spec)
        else:
            spec_factory = mock.patch("importlib.util.spec_from_file_location",
                                      return_value=SimpleNamespace(loader=loader))
            module_factory = mock.patch("importlib.util.module_from_spec", return_value=fake_compute)
        patches = [
            mock.patch.object(OWNER_RESOURCE, "start_binding", return_value=self.owner_wait),
            mock.patch.object(OWNER_RESOURCE, "supervisor", return_value=fake_sup),
            mock.patch.object(OWNER_RESOURCE, "resource_body_digest", return_value="body"),
            mock.patch.object(OWNER_RESOURCE, "resource_evidence_paths_conflict",
                              return_value=late_conflict == "evidence-path"),
            mock.patch.object(RR, "classify_identity", return_value=("exited", None, "process-absent")),
            mock.patch.object(RR, "resource_process_group_quiescent", return_value=True),
            mock.patch("dispatch_owner_input._locked", side_effect=fake_inputs._locked),
            mock.patch("dispatch_owner_input._target", side_effect=fake_inputs._target),
            spec_factory,
            module_factory,
            mock.patch.dict(sys.modules, {"artifact_producer": fake_artifacts}),
            mock.patch.object(R.subprocess, "run"),
            mock.patch.object(R.subprocess, "Popen"),
            mock.patch.object(R, "verify_launch_input"),
            mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs),
                                         "AGENT_DISPATCH_COMPLETION_MODE": "supervised",
                                         **({"COMPUTE_HOSTS_CONFIG": str(self.compute_config)}
                                            if real_compute else {})}, clear=False),
            mock.patch.object(OWNER_RESOURCE, "compute_holder", side_effect=self._holder(
                lineage, route_free, source)),
            mock.patch.object(OWNER_RESOURCE, "resource_holder", side_effect=self._holder(
                lineage, route_free, source)),
        ]
        entered = [patcher.start() for patcher in patches]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patches)])
        arm_runner = entered[11]
        process_spawn = entered[12]
        arm_calls = 0
        def run_arm(*args, **kwargs):
            nonlocal arm_calls
            arm_calls += 1
            if interrupt_before_arm and arm_calls == 1:
                raise subprocess.TimeoutExpired(args[0], 30)
            if real_compute:
                self.armed = {**self.armed, "predecessor_id": "full-run__a1"}
            return subprocess.CompletedProcess(args[0], 0)
        arm_runner.side_effect = run_arm
        args = SimpleNamespace(jobs=str(self.jobs), parent_attempt_id="att-owner")
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
            if route_free:
                with self.assertRaises(SystemExit) as refused:
                    R.adopt_compute_run(self.registry, args, self.route,
                                        self.route_file, "compute-1")
                self.assertEqual(refused.exception.code, 65)
                self.assertIn("resource-compute-route-mismatch",
                              refused.exception.resource_message)
                return json.loads(self.registry.read_text())["runs"]
            if interrupt_before_arm:
                with self.assertRaises(SystemExit) as interrupted:
                    R.adopt_compute_run(self.registry, args, self.route, self.route_file, "compute-1")
                self.assertEqual(interrupted.exception.code, 65)
                self.assertIn("resource-compute-binding-conflict", interrupted.exception.resource_message)
            else:
                R.adopt_compute_run(self.registry, args, self.route, self.route_file, "compute-1")
            if replay or interrupt_before_arm:
                self.pre_replay_registry = self.registry.read_bytes()
                self.pre_replay_failed_row = json.loads(self.pre_replay_registry)["runs"]["full-run"]
                self.pre_replay_sentinel = self.sentinel.read_bytes()
                self.pre_replay_jobs = self.jobs.read_bytes()
                self.pre_replay_workflow = {
                    path.relative_to(self.workflow).as_posix(): path.read_bytes()
                    for path in self.workflow.rglob("*") if path.is_file()
                }
                self.pre_replay_marker = self.marker.read_bytes()
                self.pre_replay_claim = self.claim.read_bytes()
                if mutate_outputs_before_replay:
                    self._mutate_real_compute_output()
                R.adopt_compute_run(self.registry, args, self.route, self.route_file, "compute-1")
        if interrupt_before_arm or replay:
            self.assertEqual(arm_calls, 1 if real_compute and not interrupt_before_arm else 2)
        self.assertEqual(process_spawn.call_count, 0)
        if real_compute:
            self.assertGreaterEqual(self.remote_call.call_count, 1)
        return json.loads(self.registry.read_text())["runs"]

    def _holder(self, lineage, route_free, source):
        def holder(document=None, **_kwargs):
            if route_free:
                return {"route_id": None, "route_hash": None, "route_file": None,
                        "inherited": False,
                        "origin": {"route_id": None, "route_hash": None, "route_file": None},
                        "chain": []}
            if lineage:
                return {"route_id": self.route["route_id"],
                        "route_hash": self.route["route_hash"],
                        "route_file": str(self.route_file), "inherited": True,
                        "origin": {"route_id": source["route_id"],
                                   "route_hash": source["route_hash"],
                                   "route_file": str(self.root / "source-route.json")},
                        "chain": [{"route_id": self.route["route_id"],
                                   "route_hash": self.route["route_hash"],
                                   "route_file": str(self.route_file),
                                   "owner_attempt_id": "att-original"}]}
            # Like the real holder: a run no handoff moved has the address its
            # own record names as its origin.
            recorded = (document or {}).get("route") or (document or {}).get("route_file")
            return {"route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"],
                    "route_file": str(self.route_file), "inherited": False,
                    "origin": {"route_id": self.route["route_id"],
                               "route_hash": self.route["route_hash"],
                               "route_file": recorded or str(self.route_file)},
                    "chain": []}
        return holder

    def test_adopt_compute_run_follows_pin_handoff_lineage(self):
        runs = self._run_adoption(lineage=True)
        adopted = runs["full-run__a1"]
        self.assertEqual(adopted["execution"]["kind"], "compute-hosts")
        self.assertEqual(adopted["execution"]["run_id"], "compute-1")
        self.assertEqual(adopted["route"], str(self.route_file.resolve()))
        self.assertEqual(adopted["parent_attempt_id"], "att-owner")

    def test_adopt_compute_run_without_handoff_still_compares_the_current_route_file(self):
        # A run no handoff moved is bound to this route's own file: a row that
        # names another file under the same id/hash is not adopted.
        self.original["route"] = str(self.root / "another-route.json")
        self.registry.write_text(json.dumps({"schema_version": 1,
                                             "runs": {"full-run": self.original}}))
        with self.assertRaises(SystemExit) as refused:
            self._run_adoption()
        self.assertEqual(refused.exception.code, 65)
        self.assertIn("resource-compute-binding-conflict", refused.exception.resource_message)
        self.assertIn("resource.route", refused.exception.resource_message)

    def test_adopt_compute_run_refuses_route_free_provenance(self):
        runs = self._run_adoption(route_free=True)
        self.assertEqual(list(runs), ["full-run"], "route-free provenance is never adopted")
        self.assertEqual(runs["full-run"], self.original)

    def test_same_contract_adopts_one_retry_without_payload_spawn(self):
        runs = self._run_adoption()
        adopted = runs["full-run__a1"]
        self.assertEqual(adopted["status"], "succeeded")
        self.assertEqual(adopted["execution"]["kind"], "compute-hosts")
        self.assertEqual(adopted["parent_attempt_id"], "att-owner")
        self.assertEqual(runs["full-run"], self.original)
        self.assertEqual(self.sentinel.read_text(), "1\n")

    def test_repeat_compute_run_reuses_the_published_row(self):
        runs = self._run_adoption(replay=True)
        self.assertEqual(list(runs), ["full-run", "full-run__a1"])
        self.assertEqual(runs["full-run__a1"]["execution"]["run_id"], "compute-1")

    def test_changed_output_receipt_refuses_armed_replay_without_mutation(self):
        with self.assertRaises(SystemExit) as caught:
            self._run_adoption(real_compute=True, replay=True,
                               mutate_outputs_before_replay=True)
        self.assertEqual(caught.exception.code, 65)
        refusal = json.loads(caught.exception.resource_message)
        self.assertEqual(refusal["reason"], "resource-compute-evidence-changed")
        self.assertEqual(refusal["differences"][0]["field"], "execution.outputs_sha256")
        self.assertTrue(refusal["differences"][0]["expected"])
        self.assertTrue(refusal["differences"][0]["actual"])
        self.assertTrue(refusal["next_action"])
        self.assertFalse(refusal["retryable"])
        self.assertEqual(self.registry.read_bytes(), self.pre_replay_registry)
        self.assertEqual(self.sentinel.read_bytes(), self.pre_replay_sentinel)
        self.assertEqual(self.jobs.read_bytes(), self.pre_replay_jobs)
        self.assertEqual({path.relative_to(self.workflow).as_posix(): path.read_bytes()
                          for path in self.workflow.rglob("*") if path.is_file()},
                         self.pre_replay_workflow)
        self.assertEqual(self.marker.read_bytes(), self.pre_replay_marker)
        self.assertEqual(self.claim.read_bytes(), self.pre_replay_claim)
        self.assertEqual(json.loads(self.marker.read_bytes())["resource_execution_identity"],
                         json.loads(self.claim.read_bytes())["resource_execution_identity"])
        self.assertEqual(list(json.loads(self.registry.read_bytes())["runs"]),
                         ["full-run", "full-run__a1"])
        self.assertEqual(self.pre_replay_failed_row, self.original)

    def test_publish_before_arm_changed_output_does_not_create_another_retry(self):
        with self.assertRaises(SystemExit) as caught:
            self._run_adoption(real_compute=True, interrupt_before_arm=True,
                               mutate_outputs_before_replay=True)
        self.assertEqual(caught.exception.code, 65)
        refusal = json.loads(caught.exception.resource_message)
        self.assertEqual(refusal["reason"], "resource-compute-evidence-changed")
        self.assertFalse(refusal["retryable"])
        self.assertEqual(self.registry.read_bytes(), self.pre_replay_registry)
        self.assertEqual(list(json.loads(self.registry.read_bytes())["runs"]),
                         ["full-run", "full-run__a1"])
        self.assertEqual(self.sentinel.read_bytes(), self.pre_replay_sentinel)
        self.assertEqual(self.marker.read_bytes(), self.pre_replay_marker)
        self.assertEqual(self.claim.read_bytes(), self.pre_replay_claim)
        self.assertEqual(self.jobs.read_bytes(), self.pre_replay_jobs)
        self.assertEqual({path.relative_to(self.workflow).as_posix(): path.read_bytes()
                          for path in self.workflow.rglob("*") if path.is_file()},
                         self.pre_replay_workflow)
        self.assertEqual(json.loads(self.registry.read_bytes())["runs"]["full-run"],
                         self.original)

    def test_real_execution_record_unchanged_replay_keeps_identity_and_registry_bytes(self):
        runs = self._run_adoption(real_compute=True, replay=True)
        self.assertEqual(list(runs), ["full-run", "full-run__a1"])
        self.assertEqual(self.registry.read_bytes(), self.pre_replay_registry)
        self.assertEqual(runs["full-run__a1"]["execution"]["outputs_sha256"],
                         hashlib.sha256(self.compute_run.joinpath("outputs.json").read_bytes()).hexdigest())
        self.assertEqual(runs["full-run__a1"]["execution"]["identity_key"],
                         "compute-hosts:compute-1:" + ":".join(
                             runs["full-run__a1"]["execution"][key] for key in
                             ("meta_sha256", "exit_sha256", "receipt_sha256", "contract_sha256")))
        self.assertEqual(self.marker.read_bytes(), self.pre_replay_marker)
        self.assertEqual(self.claim.read_bytes(), self.pre_replay_claim)
        self.assertEqual(self.jobs.read_bytes(), self.pre_replay_jobs)
        self.assertEqual({path.relative_to(self.workflow).as_posix(): path.read_bytes()
                          for path in self.workflow.rglob("*") if path.is_file()},
                         self.pre_replay_workflow)

    def test_real_publish_before_arm_interruption_reuses_identical_row(self):
        runs = self._run_adoption(real_compute=True, interrupt_before_arm=True)
        self.assertEqual(list(runs), ["full-run", "full-run__a1"])
        self.assertEqual(self.registry.read_bytes(), self.pre_replay_registry)
        self.assertEqual(runs["full-run__a1"]["execution"]["outputs_sha256"],
                         hashlib.sha256(self.compute_run.joinpath("outputs.json").read_bytes()).hexdigest())
        self.assertEqual(runs["full-run__a1"]["execution"]["identity_key"],
                         "compute-hosts:compute-1:" + ":".join(
                             runs["full-run__a1"]["execution"][key] for key in
                             ("meta_sha256", "exit_sha256", "receipt_sha256", "contract_sha256")))
        self.assertEqual(self.marker.read_bytes(), self.pre_replay_marker)
        self.assertEqual(self.claim.read_bytes(), self.pre_replay_claim)
        self.assertEqual(self.pre_replay_failed_row, self.original)

    def test_missing_outputs_hash_fails_closed_without_selecting_local_failed_row(self):
        before_registry = self.registry.read_bytes()
        with self.assertRaises(SystemExit) as caught:
            self._run_adoption(missing_outputs_hash=True)
        self.assertEqual(caught.exception.code, 65)
        refusal = json.loads(caught.exception.resource_message)
        self.assertEqual(refusal["reason"], "resource-compute-output-missing")
        self.assertEqual(refusal["differences"], [{"field": "execution.outputs_sha256",
            "expected": "non-empty recorded evidence", "actual": None}])
        self.assertFalse(refusal["retryable"])
        self.assertEqual(self.registry.read_bytes(), before_registry)

    def test_publish_then_arm_interruption_converges_on_one_row(self):
        runs = self._run_adoption(interrupt_before_arm=True)
        self.assertEqual(list(runs), ["full-run", "full-run__a1"])
        self.assertEqual(runs["full-run__a1"]["execution"]["run_id"], "compute-1")

    def test_contract_mismatch_refuses_before_registry_or_sentinel_changes(self):
        prior_bytes = self.registry.read_bytes()
        with self.assertRaises(SystemExit) as caught:
            self._run_adoption(cwd="/changed")
        self.assertEqual(caught.exception.code, 65)
        self.assertEqual(self.registry.read_bytes(), prior_bytes)
        self.assertEqual(self.sentinel.read_text(), "1\n")

    def test_output_bytes_must_match_the_compute_completion_snapshot(self):
        prior_bytes = self.registry.read_bytes()
        (self.output / "run.json").write_text('{"changed":true}\n')
        with self.assertRaises(SystemExit) as caught:
            self._run_adoption()
        self.assertEqual(caught.exception.code, 65)
        self.assertEqual(self.registry.read_bytes(), prior_bytes)
        self.assertEqual(self.sentinel.read_text(), "1\n")

    def test_typed_refusals_preserve_registry_failed_row_sentinel_and_journal(self):
        cases = (
            ("binding", {"binding_conflict": True}, "resource-compute-binding-conflict"),
            ("closed", {"route_closed": True}, "resource-compute-route-closed"),
            ("owner-input", {"owner_status": "closed", "target": None},
             "resource-compute-owner-input-unavailable"),
        )
        for name, options, reason in cases:
            with self.subTest(name=name):
                self.setUp()
                if name == "binding":
                    self.original["route"] = str(self.root / "foreign-route.json")
                    self.registry.write_text(json.dumps({"schema_version": 1,
                                                         "runs": {"full-run": self.original}}))
                before_registry = self.registry.read_bytes()
                before_failed = json.loads(before_registry)["runs"]["full-run"]
                before_sentinel = self.sentinel.read_bytes()
                before_jobs = self.jobs.read_bytes()
                workflow = self.root / "workflow"
                workflow.mkdir(exist_ok=True)
                before_workflow = {path.relative_to(workflow).as_posix(): path.read_bytes()
                                   for path in workflow.rglob("*") if path.is_file()}
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as caught:
                        self._run_adoption(**options)
                self.assertEqual(caught.exception.code, 65)
                refusal = json.loads(caught.exception.resource_message)
                self.assertEqual(refusal["reason"], reason)
                self.assertTrue(refusal["differences"])
                if name == "binding":
                    self.assertIn("resource.route", {item["field"] for item in refusal["differences"]})
                self.assertTrue(refusal["next_action"])
                self.assertEqual(self.registry.read_bytes(), before_registry)
                self.assertEqual(json.loads(self.registry.read_bytes())["runs"]["full-run"], before_failed)
                self.assertEqual(self.sentinel.read_bytes(), before_sentinel)
                self.assertEqual(self.jobs.read_bytes(), before_jobs)
                self.assertEqual({path.relative_to(workflow).as_posix(): path.read_bytes()
                                  for path in workflow.rglob("*") if path.is_file()}, before_workflow)
                self.assertNotIn("full-run__a1", json.loads(self.registry.read_bytes())["runs"])

    def test_late_binding_conflicts_include_field_differences_before_mutation(self):
        for case in ("armed-row", "stage-state", "evidence-path", "publication"):
            with self.subTest(case=case):
                self.setUp()
                before_registry = self.registry.read_bytes()
                before_sentinel = self.sentinel.read_bytes()
                before_jobs = self.jobs.read_bytes()
                before_workflow = self.root / "workflow"
                before_workflow.mkdir(exist_ok=True)
                before_journal = {path.relative_to(before_workflow).as_posix(): path.read_bytes()
                                  for path in before_workflow.rglob("*") if path.is_file()}
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        self._run_adoption(late_conflict=case)
                self.assertEqual(caught.exception.code, 65)
                refusal = json.loads(caught.exception.resource_message)
                self.assertEqual(refusal["reason"], "resource-compute-binding-conflict")
                self.assertTrue(refusal["differences"])
                self.assertTrue(all({"field", "expected", "actual"} <= set(item)
                                    for item in refusal["differences"]))
                self.assertTrue(refusal["next_action"])
                expected_registry = (self.raced_registry if case == "publication" else before_registry)
                self.assertEqual(self.registry.read_bytes(), expected_registry)
                self.assertEqual(self.sentinel.read_bytes(), before_sentinel)
                self.assertEqual(self.jobs.read_bytes(), before_jobs)
                self.assertEqual({path.relative_to(before_workflow).as_posix(): path.read_bytes()
                                  for path in before_workflow.rglob("*") if path.is_file()}, before_journal)


class TestProgressLaunch(unittest.TestCase):
    def test_main_assigns_one_path_and_replaces_inherited_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, log = root / "runs.json", root / "eval.log"
            route = root / "route.json"
            route.write_text(json.dumps({"capability": "autopilot-lab", "artifact_root": str(root),
                "nodes": [{"id": "eval-run", "kind": "resource-runner", "resource_transport": "detached-process"}]}))
            payload = (f"import sys, time; sys.path.insert(0, {str(ROOT / 'utilities')!r}); "
                       "from resource_progress import write_progress; assert write_progress(1, 'item'); time.sleep(.2)")
            smoke_spec = importlib.util.spec_from_file_location("progress_smoke_fixture", SMOKE)
            smoke = importlib.util.module_from_spec(smoke_spec)
            smoke_spec.loader.exec_module(smoke)
            smoke_input = root / "smoke-input"
            smoke_input.write_text("progress fixture\n")
            children = []
            real_popen = subprocess.Popen
            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            with mock.patch.object(R.subprocess, "run"), mock.patch.object(R, "register_registry"), \
                 mock.patch.object(R.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout"), \
                 mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "",
                                             "AGENT_RESOURCE_PROGRESS_FILE": str(root / "foreign.json"),
                                             "AGENT_RESOURCE_RUN_ID": "foreign", "AGENT_RESOURCE_NODE": "other"}):
                # Use the normal producer payload and seal for this launch's
                # exact command/cwd/environment, alongside the earlier mocks.
                attestation = smoke.payload([smoke_input], [sys.executable, "-c", payload], root)
                attestation.update(status="passed", exit_code=0)
                attestation["attestation_hash"] = "sha256:" + hashlib.sha256(json.dumps(
                    attestation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                smoke.verify(attestation)
                (root / "smoke.json").write_text(json.dumps(attestation))
                R.main(["--registry", str(registry), "start", "--run-id", "auto", "--cwd", str(root),
                        "--log", str(log), "--route", str(route), "--node", "eval-run",
                        "--smoke-attestation", str(root / "smoke.json"), "--", sys.executable, "-c", payload])
                children[0].wait(timeout=5)
            row = json.loads(registry.read_text())["runs"]["auto"]
            self.assertEqual(row["progress_file"], str(log) + ".progress.json")
            counter = json.loads(Path(row["progress_file"]).read_text())
            self.assertEqual((counter["run_id"], counter["node"], counter["completed"]), ("auto", "eval-run", 1))
            self.assertFalse((root / "foreign.json").exists())
            self.assertEqual(R.read_sentinel(row["sentinel"]), 0)


class PayloadSessionEnvironmentTest(unittest.TestCase):
    def test_gpu_payload_uses_lease_owner_and_explicit_cpu_keeps_environment(self):
        from session_identity import session_env
        declared = session_env()
        source = {key: "stale-" + key for names in declared.values() for key in names}
        source["KEEP_ME"] = "preserved"
        lease = {"owner": {"kind": "session", "harness": "claude", "id": "route-parent"}}
        updated = R.gpu_payload_environment(source, lease)
        self.assertEqual(source[declared["claude"][0]], "stale-" + declared["claude"][0])
        self.assertEqual(updated[declared["claude"][0]], "route-parent")
        self.assertEqual(updated["KEEP_ME"], "preserved")
        session_keys = {key for names in declared.values() for key in names}
        self.assertFalse((session_keys - {declared["claude"][0]}) & updated.keys())
        self.assertIs(R.gpu_payload_environment(source, None), source)


class TestRunner(unittest.TestCase):
    def test_launch_release_pipe_supports_multidigit_fd_on_both_paths(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                route = self._resume_route() if verified else json.loads(self.route.read_text())
                route_path = self.resume_path if verified else self.route
                jobs = self.jobs if verified else self.base / "plain-jobs.log"
                jobs.parent.mkdir(parents=True, exist_ok=True)
                jobs.write_text("")
                run_id = "highfd-verified" if verified else "highfd-ordinary"
                log = self.base / (run_id + ".log")
                result_path = self.base / (run_id + ".json")
                startup_marker = self.base / (run_id + ".startup-ran")
                startup = self.base / (run_id + ".bash-env")
                startup.write_text("printf startup > " + shlex.quote(str(startup_marker)) + "\n")
                code = ("import json,os,sys; from pathlib import Path; Path(sys.argv[1]).write_text("
                        "json.dumps({'args':sys.argv[2:], 'stdin':sys.stdin.read(), "
                        "'bash_env':os.environ.get('BASH_ENV')}))")
                args = ["start", "--run-id", run_id, "--cwd", str(self.repo), "--log", str(log),
                        "--route", str(route_path), "--node", "resume-run" if verified else "full-run",
                        "--jobs", str(jobs)]
                command = [sys.executable, "-c", code, str(result_path), "payload-arg"]
                if not verified:
                    attestation = self.base / (run_id + ".smoke.json")
                    subprocess.run([sys.executable, str(SMOKE), "attest", "--input",
                        str(self.repo / "config"), "--cwd", str(self.repo), "--output",
                        str(attestation), "--", *command], check=True, stdout=subprocess.DEVNULL,
                        env=CLEAN_ENV)
                    args.extend(["--smoke-attestation", str(attestation)])
                args.extend(["--", *command])
                held = []
                try:
                    for _ in range(6):
                        held.extend(os.pipe())
                    self.assertGreaterEqual(max(held), 10)
                    outcome = self.cli(*args, pass_fds=tuple(held), extra_env={"BASH_ENV": str(startup)})
                finally:
                    for fd in held:
                        os.close(fd)
                self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
                row = json.loads(self.registry.read_text())["runs"][run_id]
                sentinel = Path(row["sentinel"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not sentinel.exists():
                    time.sleep(.02)
                self.assertEqual(R.read_sentinel(sentinel), 0)
                # Smoke attestation also runs the command. Judge the new launch
                # after its own sentinel, rather than reading the smoke output.
                self.assertTrue(result_path.exists(), outcome.stdout + outcome.stderr)
                self.assertFalse(startup_marker.exists(), "wrapper sourced BASH_ENV before the release fence")
                self.assertEqual(json.loads(result_path.read_text()),
                                 {"args": ["payload-arg"], "stdin": "", "bash_env": str(startup)})

    def test_queued_payload_uses_smoke_execution_environment(self):
        import importlib.util
        overlay = self.base / "smoke-only-overlay"
        overlay.mkdir()
        (overlay / "frame_overlay.py").write_text("VALUE = 'smoke-only'\n")
        smoke_command = ["env", "PYTHONPATH=" + str(overlay), sys.executable,
                         "-c", "import frame_overlay; assert frame_overlay.VALUE == 'smoke-only'"]
        smoke = self.base / "smoke-only-environment.json"
        smoke_tool = importlib.util.spec_from_file_location("environment_smoke_fixture", SMOKE)
        module = importlib.util.module_from_spec(smoke_tool)
        smoke_tool.loader.exec_module(module)
        attestation = module.payload([self.repo / "config"], smoke_command, self.repo)
        smoke_result = subprocess.run(smoke_command, cwd=self.repo, env=CLEAN_ENV,
                                      text=True, capture_output=True, check=False)
        self.assertEqual(smoke_result.returncode, 0, smoke_result.stderr)
        attestation.update(exit_code=0, status="passed")
        attestation["attestation_hash"] = "sha256:" + hashlib.sha256(json.dumps(
            attestation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        smoke.write_text(json.dumps(attestation, sort_keys=True))
        payload_code = ("import sys,time; from pathlib import Path; time.sleep(.1); "
                        "import frame_overlay; Path(sys.argv[1]).write_text(frame_overlay.VALUE)")
        args = list(self.start_args(smoke=smoke, run_id="smoke-env-boundary"))
        separator = args.index("--")
        args[separator + 1:] = [sys.executable, "-c", payload_code, str(self.launch)]
        outcome = self.cli(*args, cwd=self.repo)
        self.assertNotEqual(outcome.returncode, 0,
                            "the detached payload must be refused before launch when smoke-only PYTHONPATH differs")
        self.assertIn("smoke-execution-environment-mismatch", outcome.stderr)
        self.assertFalse(self.launch.exists())
        if self.registry.exists():
            rows = json.loads(self.registry.read_text()).get("runs", {})
            self.assertNotIn("smoke-env-boundary", rows)

        legacy = dict(attestation)
        legacy.pop("execution_environment")
        legacy.pop("attestation_hash")
        legacy["attestation_hash"] = "sha256:" + hashlib.sha256(json.dumps(
            legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        legacy_path = self.base / "legacy-unbound-smoke.json"
        legacy_path.write_text(json.dumps(legacy, sort_keys=True))
        legacy_args = self.start_args(smoke=legacy_path, run_id="legacy-unbound-smoke")
        legacy_outcome = self.cli(*legacy_args, cwd=self.repo)
        self.assertNotEqual(legacy_outcome.returncode, 0)
        self.assertIn("smoke-execution-environment-unbound", legacy_outcome.stderr)
        self.assertFalse(self.launch.exists())

    def test_cleanup_stops_only_its_exact_supervisor(self):
        watch = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], env=CLEAN_ENV)
        try:
            supervision = R.launched_identity(watch)
            self.assertIsNotNone(supervision)
            run = {"supervision": {**supervision, "starttime": "0"}}
            self.registry.write_text(json.dumps({"runs": {"observer": run}}))
            self._stop_started_runs()
            self.assertIsNone(watch.poll(), "cleanup stopped a mismatched process identity")

            run["supervision"] = supervision
            self.registry.write_text(json.dumps({"runs": {"observer": run}}))
            self._stop_started_runs()
            self.assertEqual(watch.poll(), -signal.SIGTERM,
                             "cleanup returned before its observer exited")
        finally:
            if watch.poll() is None:
                watch.terminate()
            watch.wait(timeout=3)

    def test_cleanup_waits_for_exit_when_supervisor_observation_is_unknown(self):
        code = textwrap.dedent("""
            import os, signal, time
            def stop(signum, frame):
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                time.sleep(.1)
                os.kill(os.getpid(), signal.SIGTERM)
            signal.signal(signal.SIGTERM, stop)
            print('ready', flush=True)
            time.sleep(30)
        """)
        watch = subprocess.Popen([sys.executable, "-c", code], env=CLEAN_ENV,
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([watch.stdout], [], [], 3)[0])
            self.assertEqual(watch.stdout.readline(), "ready\n")
            supervision = R.launched_identity(watch)
            self.assertIsNotNone(supervision)
            self.registry.write_text(json.dumps({"runs": {
                "observer": {"supervision": supervision}}}))
            alive = R.RESOURCE_RESUME.supervisor_alive
            observed = False

            def lose_command_observation(binding):
                nonlocal observed
                if observed:
                    # A non-working projection can be unknown before the
                    # exact process has finished handling its termination.
                    return False
                observed = True
                return alive(binding)

            with mock.patch.object(R.RESOURCE_RESUME, "supervisor_alive",
                                   side_effect=lose_command_observation):
                self._stop_started_runs()
            self.assertEqual(watch.poll(), -signal.SIGTERM,
                             "unknown observation is not an exit acknowledgement")
        finally:
            if watch.poll() is None:
                watch.terminate()
            watch.wait(timeout=3)
            watch.stdout.close()

    def test_resource_run_probe_attributes_both_gpu_spawn_paths_to_route_parent(self):
        harness = textwrap.dedent(r"""
            import contextlib, hashlib, importlib.util, io, json, os, signal, subprocess, sys, time
            from pathlib import Path
            from types import SimpleNamespace
            from unittest import mock

            mode, base_arg, root_arg, node_id = sys.argv[1:]
            base, root = Path(base_arg), Path(root_arg)
            utilities = root / "utilities"
            sys.path.insert(0, str(utilities))
            import gpu_leases as G

            def load(path, name):
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module

            runner = load(utilities / "resource-runner.py", "resource_probe_runner")
            compute = load(utilities / "compute-hosts.py", "resource_probe_compute_hosts")
            jobs = base / "dispatch" / "jobs.log"
            route_file = base / "route.json"
            registry = base / "resource-runs.json"
            log = base / "run.log"
            pid_file = base / "gpu-process.pid"
            run_id = "probe-" + mode
            route = json.loads(route_file.read_text())
            command = [sys.executable, "-c",
                "import pathlib,subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "pathlib.Path(%r).write_text(str(p.pid)); p.wait()" % str(pid_file)]
            observation = {"reachable": True, "gpus": [
                {"index": 0, "uuid": "GPU-A", "free_mib": 24000,
                 "utilization_gpu_pct": 0, "processes": []}]}
            real_run = subprocess.run
            output = io.StringIO()
            process_group = None
            try:
                with mock.patch.object(G, "local_observation", return_value=observation), \
                     contextlib.redirect_stdout(output):
                    if mode == "main":
                        smoke = load(root / "tools" / "smoke-attestation.py", "probe_smoke_fixture")
                        attestation = smoke.payload([route_file], command, base)
                        attestation.update(status="passed", exit_code=0)
                        attestation["attestation_hash"] = "sha256:" + hashlib.sha256(json.dumps(
                            attestation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        smoke.verify(attestation)
                        smoke_path = base / "smoke.json"
                        smoke_path.write_text(json.dumps(attestation))
                        with mock.patch.object(runner, "verify_launch_input"), \
                             mock.patch.object(runner, "register_registry"):
                            runner.main(["--registry", str(registry), "start", "--run-id", run_id,
                                "--cwd", str(base), "--log", str(log), "--route", str(route_file),
                                "--node", node_id, "--smoke-attestation", str(smoke_path),
                                "--", *command])
                    else:
                        import artifact_producer
                        args = SimpleNamespace(jobs=str(jobs), run_id=run_id, node=node_id, share=False)
                        placeholder = {"run_id": run_id, "cwd": str(base), "log": str(log),
                            "command": command, "route": str(route_file), "node": node_id,
                            "status": "launching", "workflow_state": "READY",
                            "sentinel": str(log) + ".exit", "progress_file": str(log) + ".progress.json",
                            "started_at": time.time()}
                        fake_watch = SimpleNamespace(pid=os.getpid(), poll=lambda: None,
                                                     terminate=lambda: None)
                        supervision = runner.proc_identity(os.getpid())
                        with mock.patch.object(runner, "register_registry"), \
                             mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                                return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(base)}), \
                             mock.patch.object(runner, "start_watch",
                                return_value=(fake_watch, supervision)), \
                             mock.patch.object(runner.subprocess, "run"):
                            runner.start_verified(registry, args, route, route_file, placeholder)
                row = json.loads(output.getvalue())
                process_group = int(row["process_group"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pid_file.is_file():
                    time.sleep(.01)
                if not pid_file.is_file():
                    raise RuntimeError("resource payload did not publish its GPU process pid")
                gpu_pid = int(pid_file.read_text())
                proc_stat = Path("/proc") / str(os.getpid()) / "stat"
                stat_fields = proc_stat.read_text().rsplit(")", 1)[1].split()
                cmdline = (Path("/proc") / str(os.getpid()) / "cmdline").read_bytes()
                claim = {"root_pid": os.getpid(), "root_start": int(stat_fields[19]),
                    "root_cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
                    "owner": {"kind": "session", "harness": "opencode",
                              "id": "outer-pgid-claim"}}
                claims = [claim]
                if os.environ.get("AGENT_DISPATCH_DEPTH") == "1":
                    run_root = int(row["pid"])
                    root_stat = (Path("/proc") / str(run_root) / "stat").read_text()
                    root_fields = root_stat.rsplit(")", 1)[1].split()
                    root_cmdline = (Path("/proc") / str(run_root) / "cmdline").read_bytes()
                    claims.append({"root_pid": run_root, "root_start": int(root_fields[19]),
                        "root_cmdline_sha256": hashlib.sha256(root_cmdline).hexdigest(),
                        "owner": {"kind": "session", "harness": "codex", "id": "route-parent"}})
                fakebin = base / "fake-bin"
                fakebin.mkdir()
                fake_smi = fakebin / "nvidia-smi"
                fake_smi.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os,sys\n"
                    "if any(a.startswith('--query-gpu=') for a in sys.argv):\n"
                    " print('0, GPU-A, NVIDIA Test GPU, 10, 24000, 512')\n"
                    "else:\n"
                    " print('GPU-A, %s, python, 512' % os.environ['FAKE_GPU_PID'])\n",
                    encoding="utf-8")
                fake_smi.chmod(0o755)
                probe_env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                    "FAKE_GPU_PID": str(gpu_pid),
                    "HEARTING_OWNER_CLAIMS_JSON": json.dumps(claims)}
                probed = real_run(["bash", "-c", compute.PROBE_SCRIPT], text=True,
                    capture_output=True, env=probe_env, timeout=8)
                if probed.returncode:
                    raise RuntimeError("probe failed: " + probed.stderr)
                payload = json.loads(probed.stdout)
                process = next(proc for gpu in payload["gpus"] for proc in gpu["processes"]
                               if proc["pid"] == gpu_pid)
                print(json.dumps({"row": row, "process": process, "lease_state":
                    str(jobs.parent / "gpu-leases.json"), "outer_claim": claim,
                    "outer_session": os.environ.get("CLAUDE_CODE_SESSION_ID")}, sort_keys=True))
            finally:
                if process_group:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process_group, signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(process_group, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.02)
        """)
        session_keys = {"CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
                        "CODEX_SESSION_ID", "OPENCODE_SESSION_ID", "SSH_CONNECTION"}
        base_env = {key: value for key, value in CLEAN_ENV.items() if key not in session_keys}

        cases = [(node, part, launch, depth)
                 for node, part in (("full-run", None), ("eval-borrowed", "autopilot-lab:eval-run"))
                 for launch, depth in (("main", "1"), ("verified", "1"), ("main", None))]
        for node_id, part, launch_path, dispatch_depth in cases:
            with self.subTest(node=node_id, launch_path=launch_path, dispatch_depth=dispatch_depth):
                depth_label = dispatch_depth or "missing-depth"
                base = self.base / ("resource-probe-" + node_id + "-" + launch_path + "-" + depth_label)
                dispatch = base / "dispatch"
                route_state = base / "route-chains"
                dispatch.mkdir(parents=True)
                jobs = dispatch / "jobs.log"
                metadata = ",".join(("attempt_id=att-route-owner", "worker_type=owner",
                    "dispatch_depth=1", "owner_route_id=rt-resource-probe",
                    "owner_route_hash=sha256:resource-probe", "parent_sid=route-parent"))
                jobs.write_text("\t".join(("2099-01-01T00:00:00Z", "done", "/work", "/work",
                    "owner", metadata)) + "\n", encoding="utf-8")
                parent_ledger = route_state / "codex" / "route-parent.jsonl"
                parent_ledger.parent.mkdir(parents=True)
                parent_ledger.write_text('{"event":"compose","sid":"route-parent"}\n',
                                         encoding="utf-8")
                route_file = base / "route.json"
                route_file.write_text(json.dumps({"capability": "autopilot-code" if part else "autopilot-lab",
                    "artifact_root": str(base), "route_id": "rt-resource-probe",
                    "route_hash": "sha256:resource-probe", "nodes": [{"id": node_id,
                    **({"part": part} if part else {}), "kind": "resource-runner",
                    "resource_class": "gpu" if not part else "long-running",
                    "outputs": ["run.json"], "write_scope": ["run.json"],
                    "resource_transport": "detached-process"}]}), encoding="utf-8")
                env = {**base_env, "AGENT_HOME": str(ROOT),
                    "AGENT_DISPATCH_ATTEMPT_ID": "att-route-owner",
                    "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_DISPATCH_CALLER_HARNESS": "claude",
                    "CLAUDE_CODE_SESSION_ID": "worker-self-session",
                    "XDG_STATE_HOME": str(base / "state-home"),
                    "FLEET_ROUTE_CHAIN_DIR": str(route_state), "CUDA_VISIBLE_DEVICES": "0"}
                if dispatch_depth is not None:
                    env["AGENT_DISPATCH_DEPTH"] = dispatch_depth
                outcome = subprocess.run([sys.executable, "-c", harness, launch_path,
                    str(base), str(ROOT), node_id], text=True, capture_output=True, env=env, timeout=20)
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                result = json.loads(outcome.stdout)
                lease_owner = result["row"]["gpu_lease"]["owner"]
                expected_owner = (("codex", "route-parent") if dispatch_depth is not None else None)
                self.assertEqual((lease_owner["harness"], lease_owner["id"])
                                 if lease_owner else None, expected_owner)
                self.assertEqual(result["row"]["gpus"], "0")
                self.assertTrue(Path(result["lease_state"]).is_relative_to(base))
                self.assertEqual(result["outer_session"], "worker-self-session")
                session_owner = result["process"].get("session_owner")
                self.assertEqual((session_owner["harness"], session_owner["id"])
                                 if session_owner else None, expected_owner)
                if session_owner:
                    self.assertEqual(session_owner["source"], "persistent-claim+ancestry")
                    self.assertEqual(session_owner["evidence_pid"], result["row"]["pid"])
                    self.assertNotEqual(session_owner["id"], result["outer_claim"]["owner"]["id"])
                self.assertEqual(result["process"]["owner"]["kind"], "job")
                self.assertEqual(result["process"]["owner"]["id"], "att-route-owner")

    def test_verified_publication_cas_preserves_changed_or_missing_reservation(self):
        expected = {"run_id":"resume", "status":"launching", "command":["approved"], "token":"one"}
        for current in ({**expected,"command":["foreign"]}, {**expected,"token":"two"}, None):
            with self.subTest(current=current):
                data = {"schema_version":1,"runs":{} if current is None else {"resume":current}}
                self.registry.write_text(json.dumps(data))
                before = self.registry.read_bytes()
                with self.assertRaisesRegex(ValueError,"resource-reservation-changed"):
                    R.publish_verified_run(self.registry,"resume",expected,{**expected,"status":"running"})
                self.assertEqual(self.registry.read_bytes(),before)
        self.registry.write_text(json.dumps({"schema_version":1,"runs":{"resume":expected}}))
        published = {**expected,"status":"running"}
        R.publish_verified_run(self.registry,"resume",expected,published)
        self.assertEqual(json.loads(self.registry.read_text())["runs"]["resume"],published)

        jobs = self.base / "cas-jobs.log"
        jobs.write_text("")
        args = SimpleNamespace(jobs=str(jobs), run_id="resume", node="resume-run")
        route_file = self.base / "cas-route.json"
        for changed in ({**expected,"command":["foreign"]}, None):
            with self.subTest(exception_row=changed):
                self.registry.write_text(json.dumps({"schema_version":1,"runs":{}}))
                preserved = []
                def interrupt(_registry):
                    self.registry.write_text(json.dumps({"schema_version":1,
                        "runs":{} if changed is None else {"resume":changed}}))
                    preserved.append(self.registry.read_bytes())
                    raise RuntimeError("launch-interrupted")
                with mock.patch.object(R,"register_registry",side_effect=interrupt), \
                     mock.patch.object(R.subprocess,"Popen") as launch:
                    with self.assertRaisesRegex(ValueError,"resource-reservation-changed"):
                        R.start_verified(self.registry,args,{},route_file,dict(expected))
                self.assertEqual(self.registry.read_bytes(),preserved[0])
                self.assertEqual(launch.call_count,0)

    def test_owner_resource_external_watch_once_fence_and_fast_exit(self):
        import artifact_producer
        jobs = self.base / "owner-jobs.log"
        jobs.write_text("")
        route_file = self.base / "owner-route.json"
        args = SimpleNamespace(jobs=str(jobs), run_id="owner-run", node="full-run")
        placeholder = {"run_id":"owner-run", "cwd":str(self.repo), "log":str(self.log),
            "command":[sys.executable,"-c",f"import sys; sys.path.insert(0, {str(ROOT / 'utilities')!r}); from resource_progress import write_progress; assert write_progress(12, 'epoch', total=50); from pathlib import Path; Path({str(self.launch)!r}).write_text('once')"],
            "route":str(route_file), "node":"full-run", "status":"launching", "sentinel":str(self.log)+".exit",
            "parent_attempt_id":"att-parent", "progress_file":str(self.log)+".progress.json",
            "owner_wait":{"session_id":"same-native"}}
        real_popen = subprocess.Popen
        watch = mock.Mock(pid=os.getpid())
        watch.poll.return_value = None
        payloads = []
        def launch(argv, **kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0],b"ready\n")
                return watch
            proc = real_popen(argv, **kwargs)
            payloads.append(proc)
            return proc
        route = {"route_id":"rt-owner", "route_hash":"sha256:owner"}
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}), \
             mock.patch.object(artifact_producer,"prepare_route_artifact_env",return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(self.base)}), \
             mock.patch.object(R,"register_registry"), \
             mock.patch.object(R.subprocess,"run") as arm, \
             mock.patch.object(R.subprocess,"Popen",side_effect=launch), \
             mock.patch("sys.stdout"):
            R.start_verified(self.registry,args,route,route_file,dict(placeholder))
            payloads[0].wait(timeout=5)
            R.start_verified(self.registry,args,route,route_file,dict(placeholder))
        self.assertEqual(len(payloads),1)
        self.assertEqual(self.launch.read_text(),"once")
        self.assertEqual(arm.call_count,1)
        self.assertIn("--successor-external",arm.call_args.args[0])
        self.assertNotIn("--successor-command",arm.call_args.args[0])
        row = json.loads(self.registry.read_text())["runs"]["owner-run"]
        self.assertEqual(row["resource_policy"],"supervised-owner")
        self.assertEqual(row["owner_wait"],placeholder["owner_wait"])
        self.assertEqual(R.read_sentinel(row["sentinel"]),0)
        progress = json.loads(Path(row["progress_file"]).read_text())
        self.assertEqual((progress["run_id"], progress["node"], progress["completed"], progress["total"]),
                         ("owner-run", "full-run", 12, 50))

    def test_codex_tool_intent_has_no_process_until_controller_admission_once(self):
        import artifact_producer
        jobs = self.base / "codex-owner-jobs.log"
        jobs.write_text("")
        route_file = self.base / "codex-owner-route.json"
        args = SimpleNamespace(jobs=str(jobs),run_id="codex-owner-run",node="full-run")
        body = {"run_id":args.run_id,"cwd":str(self.repo),"log":str(self.log),
            "command":[sys.executable,"-c",f"from pathlib import Path; import time; time.sleep(.2); Path({str(self.launch)!r}).write_text('once')"],
            "route":str(route_file),"node":"full-run","status":"launching","sentinel":str(self.log)+".exit",
            "parent_attempt_id":"att-parent","owner_wait":{"session_id":"same-native","launch_scope":"codex-owner-controller"},
            "launch_request":{"smoke_attestation":None,"smoke_attestation_sha256":None,"config_manifest":None,"config_manifest_sha256":None}}
        real_popen = subprocess.Popen
        watch = mock.Mock(pid=os.getpid())
        watch.poll.return_value=None
        payloads=[]
        def launch(argv,**kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0],b"ready\n")
                return watch
            proc=real_popen(argv,**kwargs)
            payloads.append(proc)
            return proc
        route={"route_id":"rt-codex-owner","route_hash":"sha256:owner"}
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}), \
             mock.patch.object(artifact_producer,"prepare_route_artifact_env",return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(self.base)}), \
             mock.patch.object(R,"register_registry"), mock.patch.object(R.subprocess,"run"), \
             mock.patch("sys.stdout") as stdout, mock.patch.object(R.subprocess,"Popen",side_effect=launch) as spawn:
            R.start_verified(self.registry,args,route,route_file,dict(body))
            queued=json.loads(self.registry.read_text())["runs"][args.run_id]
            receipt=json.loads(stdout.write.call_args_list[-2].args[0])
            self.assertEqual(queued["launch_state"],"queued")
            self.assertNotIn("pid",queued)
            self.assertIs(receipt["payload_spawned"],False)
            self.assertIs(receipt["supervisor_alive"],False)
            self.assertEqual(spawn.call_count,0)
            self.assertFalse(self.launch.exists())
            R.start_verified(self.registry,args,route,route_file,dict(body))
            self.assertEqual(spawn.call_count,0)
            identity={**R.proc_identity(os.getpid()),"pid_namespace":os.readlink("/proc/self/ns/pid")}
            admission=SimpleNamespace(expected=queued,identity=identity,command=body["command"],
                sandbox={"mode":"workspace-write","enforcement":"os-sandbox"},guard=contextlib.nullcontext)
            R.start_verified(self.registry,args,route,route_file,dict(body),controller=admission)
            running=json.loads(self.registry.read_text())["runs"][args.run_id]
            self.assertEqual(running["pid_namespace"],os.readlink("/proc/self/ns/pid"))
            self.assertEqual(R.classify_identity(running)[0],"working")
            self.assertEqual(running["launch_controller"],identity)
            self.assertEqual(running["launch_state"],"started")
            self.assertFalse(self.launch.exists())
            self.assertIs(admission.children[0],payloads[0])
            self.assertIs(admission.children[1],watch)
            self.assertEqual(admission.row,running)
            payloads[0].wait(timeout=5)
            self.assertEqual(R.classify_identity(running)[0],"exited")
            self.assertEqual(self.launch.read_text(),"once")
            self.assertEqual(R.read_sentinel(running["sentinel"]),0)
            before=self.registry.read_bytes()
            with self.assertRaisesRegex(ValueError,"resource-reservation-changed"):
                R.start_verified(self.registry,args,route,route_file,dict(body),controller=admission)
            self.assertEqual(self.registry.read_bytes(),before)
            self.assertEqual(len(payloads),1)
            foreign={**running,"pid_namespace":"pid:[foreign]"}
            self.assertEqual(R.classify_identity(foreign)[0],"stale")
            self.assertEqual(R.classify_identity(foreign)[2],"process-namespace-mismatch")

    def test_queued_controller_rechecks_smoke_environment_before_release(self):
        import artifact_producer
        import importlib.util
        jobs = self.base / "queued-environment-jobs.log"
        jobs.write_text("")
        route = json.loads(self.route.read_text())
        run_id = "queued-environment"
        effect = self.base / "queued-environment-payload"
        command = [sys.executable, "-c",
                   f"from pathlib import Path; Path({str(effect)!r}).write_text('released')"]
        smoke_path = self.base / "queued-environment-smoke.json"
        smoke_spec = importlib.util.spec_from_file_location("queued_environment_smoke", SMOKE)
        smoke_module = importlib.util.module_from_spec(smoke_spec)
        smoke_spec.loader.exec_module(smoke_module)
        attestation = smoke_module.payload([self.repo / "config"], [sys.executable, "-c", "pass"], self.repo)
        attestation.update(status="passed", exit_code=0)
        attestation["attestation_hash"] = "sha256:" + hashlib.sha256(json.dumps(
            attestation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        smoke_path.write_text(json.dumps(attestation, sort_keys=True))
        args = SimpleNamespace(jobs=str(jobs), run_id=run_id, node="full-run",
                               smoke_attestation=str(smoke_path), config_manifest=None)
        body = {"run_id": run_id, "cwd": str(self.repo), "log": str(self.log),
            "command": command, "route": str(self.route), "node": "full-run",
            "status": "launching", "sentinel": str(self.log) + ".exit",
            "jobs": str(jobs), "parent_attempt_id": "att-parent",
            "owner_wait": {"session_id": "same-native", "launch_scope": "codex-owner-controller"}}
        route_watch = mock.Mock(pid=os.getpid())
        route_watch.poll.return_value = None
        runner = R
        payloads=[]
        real_popen=subprocess.Popen
        def capture_popen(*args,**kwargs):
            process=real_popen(*args,**kwargs)
            payloads.append(process)
            return process
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.base)}), \
             mock.patch.object(runner, "register_registry"), \
             mock.patch.object(runner.subprocess, "run"), \
             mock.patch.object(runner.gpu_leases, "resource_admission",
                               return_value=(None, None, {"CUDA_VISIBLE_DEVICES": ""})), \
             mock.patch.object(runner, "start_watch", return_value=(route_watch, runner.proc_identity(os.getpid()))), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=capture_popen), \
             mock.patch("sys.stdout"):
            runner.start_verified(self.registry, args, route, self.route, dict(body))
            current_registry = json.loads(self.registry.read_text())
            self.assertIn(run_id, current_registry.get("runs", {}), current_registry)
            queued = current_registry["runs"][run_id]
            self.assertEqual(queued["launch_state"], "queued")
            before = self.registry.read_bytes()
            controller = SimpleNamespace(expected=queued,
                identity={**runner.proc_identity(os.getpid()),
                          "pid_namespace": os.readlink("/proc/self/ns/pid")},
                command=command, sandbox={"mode": "workspace-write", "enforcement": "os-sandbox"},
                guard=contextlib.nullcontext, registry=str(self.registry))
            refused=None
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.base / "different-overlay")}):
                try:
                    runner.start_verified(self.registry, args, route, self.route,
                                          dict(queued), controller=controller)
                except ValueError as error:
                    refused=error
            for process in payloads:
                process.wait(timeout=5)
            self.assertIsNotNone(refused,
                "the controller released payload under an environment different from smoke")
            self.assertIn("smoke-execution-environment-mismatch", str(refused))
            self.assertEqual(payloads, [])
            self.assertEqual(self.registry.read_bytes(), before)
            self.assertFalse(effect.exists())
            runner.start_verified(self.registry, args, route, self.route,
                                  dict(queued), controller=controller)
            final_registry=json.loads(self.registry.read_text())
            self.assertIn(run_id, final_registry.get("runs", {}), json.dumps(final_registry, sort_keys=True))
            self.assertEqual(len(payloads), 1, json.dumps(final_registry["runs"][run_id], sort_keys=True))
            self.assertEqual(payloads[0].wait(timeout=5), 0)
            self.assertEqual(effect.read_text(), "released")
            final_registry=json.loads(self.registry.read_text())
            self.assertIn(run_id, final_registry.get("runs", {}), json.dumps(final_registry, sort_keys=True))
            running = final_registry["runs"][run_id]
            self.assertEqual(running["launch_state"], "started")
            self.assertEqual(runner.read_sentinel(running["sentinel"]), 0)

    def _host_route(self):
        """A real lab eval route that borrowed the GPU-free host part (the catalog is the only source)."""
        prompt = self.base / "host-request.txt"
        prompt.write_text("Run the registered GPU-free measurement and check its result.\n")
        result = subprocess.run([sys.executable, str(ROUTER), "compose", "--capability", "autopilot-lab",
            "--capability-mode", "eval", "--shape", "staged", "--graph", "host-run,metrics",
            "--slug", "host-fixture", "--cwd", str(self.repo), "--artifact-root", str(self.artifacts),
            "--dispatch-evidence", str(self.base / "dispatch-evidence.json"), "--parent-harness", "codex",
            "--spec-read", "not-applicable", "--unassigned", "--prompt-file", str(prompt)],
            env=CLEAN_ENV, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        route = json.loads(result.stdout)
        self.host_path = self.artifacts / ".runtime/routes" / (route["route_id"] + ".json")
        self.assertTrue(self.host_path.is_file())
        self.host_jobs = self.base / "host-state/jobs.log"
        self.host_jobs.parent.mkdir()
        self.host_jobs.write_text("")
        return json.loads(self.host_path.read_text())

    def _host_env(self):
        # The fixture must prove the node itself has no GPU lease, so nothing here hides CUDA devices.
        return {key: value for key, value in {**CLEAN_ENV, "AGENT_HOME": str(ROOT),
                "AGENT_RESOURCE_RUN_INDEX": str(self.index)}.items() if key != "CUDA_VISIBLE_DEVICES"}

    def test_host_run_starts_a_cpu_payload_without_smoke_or_gpu_lease(self):
        route = self._host_route()
        self.assertEqual([node["id"] for node in route["nodes"]], ["host-run", "metrics"])
        payload = ["--", sys.executable, "-c", f"from pathlib import Path; Path({str(self.launch)!r}).write_text('cpu')"]
        base = ["--registry", str(self.registry), "start", "--run-id", "host-case", "--cwd", str(self.repo),
                "--log", str(self.log), "--route", str(self.host_path), "--node", "host-run",
                "--jobs", str(self.host_jobs)]
        started = subprocess.run([sys.executable, str(RUNNER), *base, *payload],
                                 env=self._host_env(), text=True, capture_output=True, timeout=60)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        row = json.loads(self.registry.read_text())["runs"]["host-case"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and R.read_sentinel(row["sentinel"]) is None:
            time.sleep(.05)
        self.assertEqual(R.read_sentinel(row["sentinel"]), 0)
        self.assertEqual(self.launch.read_text(), "cpu")
        self.assertEqual(row["node"], "host-run")
        for key in ("gpu_lease", "gpus", "smoke_attestation", "config_ref"):
            self.assertNotIn(key, row)
        # The same runner still refuses a training node that carries no attestation.
        unattested = self.cli(*self.start_args(smoke=None, run_id="unattested"))
        self.assertNotEqual(unattested.returncode, 0)
        self.assertIn("hash-bound smoke attestation required", unattested.stderr)
        self.assertNotIn("unattested", json.loads(self.registry.read_text())["runs"])

    def test_host_run_start_refuses_a_strict_request_and_a_stale_attestation_still_applies(self):
        self._host_route()
        request = self.base / "strict-request.json"
        request.write_text(json.dumps({"schema_version": 1, "writable_roots": [], "read_roots": [],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "os-sandbox", "justification": {}}))
        argv = [sys.executable, str(RUNNER), "--registry", str(self.registry), "start", "--run-id", "strict",
                "--cwd", str(self.repo), "--log", str(self.log), "--route", str(self.host_path),
                "--node", "host-run", "--jobs", str(self.host_jobs), "--", sys.executable, "-c",
                f"from pathlib import Path; Path({str(self.launch)!r}).write_text('x')"]
        refused = subprocess.run(argv, env={**self._host_env(),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(request)}, text=True, capture_output=True, timeout=60)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("execution-access-enforcement-unavailable:host-process-payload", refused.stderr)
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse(self.launch.exists())
        # A supplied attestation keeps its verification even on the host node.
        stale = subprocess.run([*argv[:-4], "--smoke-attestation", str(self.base / "absent.json"), *argv[-4:]],
                               env=self._host_env(), text=True, capture_output=True, timeout=60)
        self.assertNotEqual(stale.returncode, 0)
        self.assertFalse(self.launch.exists())

    def test_controller_admission_releases_a_host_payload_once_without_smoke(self):
        import artifact_producer
        route = self._host_route()
        jobs = self.host_jobs
        args = SimpleNamespace(jobs=str(jobs), run_id="host-owner", node="host-run")
        body = {"run_id": args.run_id, "cwd": str(self.repo), "log": str(self.log),
            "command": [sys.executable, "-c", f"from pathlib import Path; Path({str(self.launch)!r}).write_text('once')"],
            "route": str(self.host_path), "node": "host-run", "status": "launching", "sentinel": str(self.log) + ".exit",
            "jobs": str(jobs), "parent_attempt_id": "att-parent", "resource_policy": "supervised-owner",
            "owner_wait": {"session_id": "same-native", "launch_scope": "codex-owner-controller"},
            "launch_request": {"smoke_attestation": None, "smoke_attestation_sha256": None,
                               "config_manifest": None, "config_manifest_sha256": None}}
        real_popen = subprocess.Popen
        watch = mock.Mock(pid=os.getpid())
        watch.poll.return_value = None
        payloads = []
        def launch(argv, **kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0], b"ready\n")
                return watch
            proc = real_popen(argv, **kwargs)
            payloads.append(proc)
            return proc
        sealed = {key: route[key] for key in ("route_id", "route_hash")}
        gpu_probe = mock.patch.object(R.gpu_leases, "resource_admission",
                                      wraps=R.gpu_leases.resource_admission)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                               return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.base)}), \
             mock.patch.object(R, "register_registry"), mock.patch.object(R.subprocess, "run"), \
             mock.patch("sys.stdout"), mock.patch.object(R.subprocess, "Popen", side_effect=launch) as spawn, \
             gpu_probe as admission:
            R.start_verified(self.registry, args, {**route, **sealed}, self.host_path, dict(body))
            queued = json.loads(self.registry.read_text())["runs"][args.run_id]
            self.assertEqual((queued["launch_state"], spawn.call_count), ("queued", 0))
            identity = {**R.proc_identity(os.getpid()), "pid_namespace": os.readlink("/proc/self/ns/pid")}
            admission_ctl = SimpleNamespace(expected=queued, identity=identity, command=body["command"],
                sandbox={"mode": "danger-full-access", "enforcement": "none"}, guard=contextlib.nullcontext,
                registry=str(self.registry))
            with mock.patch.object(R, "verify_launch_input"):
                R.main(R.controller_argv(self.registry, queued), controller=admission_ctl)
            payloads[0].wait(timeout=5)
            self.assertEqual(len(payloads), 1)
            self.assertEqual(self.launch.read_text(), "once")
            running = json.loads(self.registry.read_text())["runs"][args.run_id]
            self.assertEqual((running["launch_state"], running["payload_sandbox"]["enforcement"]),
                             ("started", "none"))
            self.assertNotIn("gpu_lease", running)
            self.assertEqual(R.read_sentinel(running["sentinel"]), 0)
            for call in admission.call_args_list:
                self.assertIs(call.kwargs["gpu_scoped"], False)

    def test_controller_start_refuses_a_strict_request_queued_after_intent_and_settles(self):
        route = self._host_route()
        request = self.base / "late-strict-request.json"
        request.write_text(json.dumps({"schema_version": 1, "writable_roots": [], "read_roots": [],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "os-sandbox", "justification": {}}))
        args = SimpleNamespace(jobs=str(self.host_jobs), run_id="host-late", node="host-run")
        body = {"run_id": args.run_id, "cwd": str(self.repo), "log": str(self.log),
            "command": [sys.executable, "-c", f"from pathlib import Path; Path({str(self.launch)!r}).write_text('x')"],
            "route": str(self.host_path), "node": "host-run", "status": "launching", "sentinel": str(self.log) + ".exit",
            "jobs": str(self.host_jobs), "parent_attempt_id": "att-parent", "resource_policy": "supervised-owner",
            "owner_wait": {"session_id": "same-native", "launch_scope": "codex-owner-controller"},
            "launch_request": {"smoke_attestation": None, "smoke_attestation_sha256": None,
                               "config_manifest": None, "config_manifest_sha256": None}}
        sealed = {key: route[key] for key in ("route_id", "route_hash")}
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.host_jobs)}), \
             mock.patch.object(R, "register_registry"), mock.patch.object(R.subprocess, "run"), \
             mock.patch("sys.stdout"), mock.patch.object(R.subprocess, "Popen") as spawn:
            R.start_verified(self.registry, args, {**route, **sealed}, self.host_path, dict(body))
            queued = json.loads(self.registry.read_text())["runs"][args.run_id]
            ctl = SimpleNamespace(expected=queued, registry=str(self.registry), guard=contextlib.nullcontext,
                identity={**R.proc_identity(os.getpid()), "pid_namespace": os.readlink("/proc/self/ns/pid")},
                command=body["command"], sandbox={"mode": "danger-full-access", "enforcement": "none"})
            seen = []
            def settle(controller, error):
                seen.append(error)
                return None
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(request)}), \
                 mock.patch.object(R, "settle_queued_validation_failure", side_effect=settle):
                with self.assertRaises(SystemExit):
                    R.main(R.controller_argv(self.registry, queued), controller=ctl)
        # The runner start check, not the command builder, refuses it: the failure reaches the
        # runner's pre-release settle hook (stubbed here) and no payload was launched.
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].code, 65)
        self.assertEqual(seen[0].resource_message, "execution-access-enforcement-unavailable:host-process-payload")
        spawn.assert_not_called()
        self.assertFalse(self.launch.exists())

    def test_gpu_launch_refusal_preserves_failure_and_cpu_bridge_retry_starts_once(self):
        import artifact_producer
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                jobs = self.base / (harness + "-jobs.log")
                jobs.write_text("")
                registry = self.base / (harness + "-runs.json")
                route_file = self.base / (harness + "-route.json")
                route = {"route_id": "rt-gpu-refusal-" + harness, "route_hash": "sha256:" + harness,
                         "nodes": [{"id": "full-run", "resource_class": "gpu"}]}
                args = SimpleNamespace(jobs=str(jobs), run_id="bridge", node="full-run")
                log = self.base / (harness + "-failed.log")
                marker = self.base / (harness + "-payload")
                command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('once')"]
                owner = {"session_id": "same-native"}
                if harness == "codex":
                    owner["launch_scope"] = "codex-owner-controller"
                body = {"run_id": "bridge", "cwd": str(self.repo), "log": str(log), "command": command,
                        "route": str(route_file), "node": "full-run", "status": "launching",
                        "sentinel": str(log) + ".exit", "parent_attempt_id": "att-parent", "owner_wait": owner}
                if harness == "codex":
                    body["launch_request"] = {}
                def controller(expected):
                    return SimpleNamespace(expected=expected, identity={**R.proc_identity(os.getpid()),
                        "pid_namespace": os.readlink("/proc/self/ns/pid")},
                        command=expected["command"], sandbox={}, guard=contextlib.nullcontext)
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
                     mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                                       return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.base)}), \
                     mock.patch.object(R, "register_registry"), mock.patch.object(R.subprocess, "run"), \
                     mock.patch("sys.stdout"), mock.patch.object(R.subprocess, "Popen") as spawn:
                    admission = None
                    if harness == "codex":
                        R.start_verified(registry, args, route, route_file, dict(body))
                        queued = json.loads(registry.read_text())["runs"]["bridge"]
                        admission = controller(queued)
                    with mock.patch.object(R.gpu_leases, "resource_admission",
                                           side_effect=R.gpu_leases.GPUUnavailable("Local GPU admission: in use")):
                        R.start_verified(registry, args, route, route_file, dict(body), controller=admission)
                    spawn.assert_not_called()
                failed = json.loads(registry.read_text())["runs"]["bridge"]
                self.assertEqual(failed["launch_state"], "not-started")
                self.assertEqual(failed["launch_request"], R.launch_request(args))
                self.assertEqual(failed["launch_error"], {"type": "GPUUnavailable", "message": "Local GPU admission: in use"})
                self.assertTrue(R.resource_never_started(failed))
                self.assertFalse(marker.exists())
                # The existing empty CUDA spelling describes a CPU control
                # bridge; target-host compute admission remains its own step.
                args.run_id = "bridge__a1"
                retry_log = self.base / (harness + "-retry.log")
                retry = {**body, "run_id": args.run_id, "log": str(retry_log),
                    "sentinel": str(retry_log) + ".exit", "command": ["env", "CUDA_VISIBLE_DEVICES=", *command]}
                real_popen = subprocess.Popen
                watch = mock.Mock(pid=os.getpid())
                watch.poll.return_value = None
                payloads = []
                def launch(argv, **kwargs):
                    if "watch" in argv:
                        os.write(kwargs["pass_fds"][0], b"ready\n")
                        return watch
                    proc = real_popen(argv, **kwargs)
                    payloads.append(proc)
                    return proc
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
                     mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                                       return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.base)}), \
                     mock.patch.object(R, "register_registry"), mock.patch.object(R.subprocess, "run"), \
                     mock.patch("sys.stdout"), mock.patch.object(R.subprocess, "Popen", side_effect=launch), \
                     mock.patch.object(R.gpu_leases, "local_observation") as probe:
                    R.start_verified(registry, args, route, route_file, dict(retry))
                    if harness == "codex":
                        queued = json.loads(registry.read_text())["runs"][args.run_id]
                        R.start_verified(registry, args, route, route_file, dict(retry), controller=controller(queued))
                    payloads[0].wait(timeout=5)
                    R.start_verified(registry, args, route, route_file, dict(retry))
                    probe.assert_not_called()
                runs = json.loads(registry.read_text())["runs"]
                self.assertEqual(runs["bridge"], failed)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(marker.read_text(), "once")
                self.assertEqual(R.read_sentinel(runs[args.run_id]["sentinel"]), 0)

    def test_stale_smoke_input_before_claim_preserves_request_and_releases_no_payload(self):
        import dispatch_resource_wait as RESOURCE
        from workflow_state import WorkflowLedger
        jobs = self.base / "smoke-jobs.log"
        jobs.write_text("")
        route = json.loads(self.route.read_text())
        ledger = WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
        row = {"run_id": "stale", "cwd": str(self.repo), "log": str(self.log),
            "command": [sys.executable, "-c", f"open({str(self.launch)!r},'w').write('wrong')"],
            "route": str(self.route), "node": "full-run", "jobs": str(jobs),
            "status": "launching", "workflow_state": "READY", "launch_state": "queued",
            "resource_policy": "supervised-owner", "parent_attempt_id": "att-parent",
            "sentinel": str(self.log) + ".exit", "owner_wait": {"session_id": "same-native"},
            "launch_request": {"smoke_attestation": str(self.attestation),
                "smoke_attestation_sha256": hashlib.sha256(self.attestation.read_bytes()).hexdigest(),
                "config_manifest": None, "config_manifest_sha256": None}}
        armed = {"predecessor_id": row["run_id"], "node": row["node"],
            "resource_registry": str(self.registry), "resource_binding": RESOURCE.resource_body_digest(row)}
        sup = RESOURCE.supervisor()
        identity = {**R.proc_identity(os.getpid()), "pid_namespace": os.readlink("/proc/self/ns/pid")}
        controller = SimpleNamespace(expected=row, registry=str(self.registry), identity=identity,
            guard=contextlib.nullcontext)
        # A legitimate edit after queued registration invalidates the real verifier.
        (self.repo / "config").write_text("changed by another session\n")
        attestation_bytes = self.attestation.read_bytes()
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness), mock.patch.dict(os.environ, CLEAN_ENV), \
                 mock.patch.object(sup, "load_route", return_value=route), \
                 mock.patch.object(sup, "ledger_for", return_value=ledger), \
                 mock.patch.object(sup, "read_armed", return_value={"full-run": armed}), \
                 mock.patch.object(sup, "resource_continuation_cancelled", return_value=False), \
                 mock.patch.object(sup, "_evaluate") as settle_stage, \
                 mock.patch.object(R, "start_verified") as launch, \
                 mock.patch.object(R.subprocess, "Popen", wraps=subprocess.Popen) as processes, \
                 mock.patch("sys.stderr"):
                self.registry.write_text(json.dumps({"schema_version": 1, "runs": {row["run_id"]: row}}))
                expectation = contextlib.nullcontext() if harness == "codex" else self.assertRaises(subprocess.CalledProcessError)
                with expectation:
                    R.main(["--registry", str(self.registry), "start", "--run-id", row["run_id"],
                        "--cwd", str(self.repo), "--log", str(self.log), "--route", str(self.route),
                        "--node", "full-run", "--smoke-attestation", str(self.attestation),
                        "--", *row["command"]], controller=controller if harness == "codex" else None)
                launch.assert_not_called()
                # These subprocesses are route/smoke verification only; no payload/watch.
                self.assertEqual(len(processes.call_args_list), 2)
                actual = json.loads(self.registry.read_text())["runs"][row["run_id"]]
                if harness == "codex":
                    self.assertTrue(R.resource_never_started(actual))
                    self.assertEqual(actual["launch_controller"], identity)
                    self.assertIn("stale smoke input: " + str(self.repo / "config"), actual["launch_error"]["message"])
                    self.assertEqual(actual["launch_request"], row["launch_request"])
                    self.assertEqual(RESOURCE.resource_body_digest(actual), armed["resource_binding"])
                    settle_stage.assert_called_once_with(route, ledger, armed, [])
                else:
                    self.assertEqual(actual, row)  # the native tool returns its refusal directly
                    settle_stage.assert_not_called()
                self.assertEqual(self.attestation.read_bytes(), attestation_bytes)
                self.assertFalse(self.launch.exists())
                self.assertFalse(Path(row["sentinel"]).exists())

    def test_queued_validation_failure_preserves_foreign_claim_cancel_and_pending_correction(self):
        import dispatch_resource_wait as RESOURCE
        sup = RESOURCE.supervisor()
        expected = {"run_id": "queued", "route": str(self.route), "node": "full-run",
            "jobs": str(self.base / "jobs.log"), "status": "launching", "launch_state": "queued",
            "resource_policy": "supervised-owner", "workflow_state": "READY",
            "sentinel": str(self.log) + ".exit"}
        armed = {"predecessor_id": "queued", "resource_registry": str(self.registry),
            "resource_binding": RESOURCE.resource_body_digest(expected)}
        ledger = SimpleNamespace(lock=contextlib.nullcontext)
        controller = SimpleNamespace(expected=expected, registry=str(self.registry),
            identity={"pid": os.getpid()}, guard=contextlib.nullcontext)
        for changes in ({"command": ["foreign"]}, {"launch_state": "claimed"},
                        {"launch_state": "started", "pid": os.getpid()},
                        {"cancel_requested": True}, {"parent_close_requested": True}):
            with self.subTest(changes=changes), mock.patch.object(sup, "load_route", return_value={}), \
                 mock.patch.object(sup, "ledger_for", return_value=ledger), \
                 mock.patch.object(sup, "read_armed", return_value={"full-run": armed}), \
                 mock.patch.object(sup, "_evaluate") as settle_stage:
                self.registry.write_text(json.dumps({"runs": {"queued": {**expected, **changes}}}))
                before = self.registry.read_bytes()
                R.settle_queued_validation_failure(controller, ValueError("invalid input"))
                self.assertEqual(self.registry.read_bytes(), before)
                settle_stage.assert_not_called()
        self.registry.write_text(json.dumps({"runs": {"queued": expected}}))
        with mock.patch.object(sup, "load_route", return_value={}), \
             mock.patch.object(sup, "ledger_for", return_value=ledger), \
             mock.patch.object(sup, "read_armed", return_value={"full-run": armed}), \
             mock.patch.object(sup, "resource_continuation_cancelled", return_value=False), \
             mock.patch.object(sup, "_evaluate") as settle_stage:
            before = self.registry.read_bytes()
            with mock.patch.object(controller, "guard", side_effect=R.LaunchDeferred("resource-owner-correction-pending")):
                with self.assertRaises(R.LaunchDeferred):
                    R.settle_queued_validation_failure(controller, ValueError("invalid input"))
            self.assertEqual(self.registry.read_bytes(), before)
            settle_stage.assert_not_called()

    def test_controller_correction_before_release_preserves_queued_body_and_payload_zero(self):
        import artifact_producer
        jobs=self.base/"correction-jobs.log"; jobs.write_text("")
        route_file=self.base/"correction-route.json"
        args=SimpleNamespace(jobs=str(jobs),run_id="correction-run",node="full-run")
        row={"run_id":args.run_id,"cwd":str(self.repo),"log":str(self.log),"command":[sys.executable,"-c",f"open({str(self.launch)!r},'w').write('wrong')"],
             "route":str(route_file),"jobs":str(jobs),"node":"full-run","status":"launching","sentinel":str(self.log)+".exit",
             "parent_attempt_id":"att-parent","resource_policy":"supervised-owner","launch_state":"queued",
             "owner_wait":{"session_id":"same-native","launch_scope":"codex-owner-controller"},
             "launch_request":R.launch_request(args)}
        self.registry.write_text(json.dumps({"schema_version":1,"runs":{args.run_id:row}}))
        checks=[]
        @contextlib.contextmanager
        def guard():
            checks.append("check")
            if len(checks)==2:
                raise R.LaunchDeferred("correction-pending")
            yield
        watch=mock.Mock(pid=os.getpid()); watch.poll.return_value=None
        real_popen=subprocess.Popen
        payloads=[]
        def launch(argv,**kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0],b"ready\n"); return watch
            proc=real_popen(argv,**kwargs); payloads.append(proc); return proc
        admission=SimpleNamespace(expected=row,identity={"pid_namespace":os.readlink("/proc/self/ns/pid")},
            command=row["command"],sandbox={"mode":"workspace-write"},guard=guard)
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}), \
             mock.patch.object(artifact_producer,"prepare_route_artifact_env",return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(self.base)}), \
             mock.patch.object(R,"register_registry"),mock.patch.object(R.subprocess,"run"), \
             mock.patch.object(R.subprocess,"Popen",side_effect=launch):
            with self.assertRaises(R.LaunchDeferred):
                R.start_verified(self.registry,args,{"route_id":"rt-correction","route_hash":"sha256:c"},route_file,dict(row),controller=admission)
        self.assertEqual(json.loads(self.registry.read_text())["runs"][args.run_id],row)
        self.assertEqual(payloads[0].returncode,125)
        self.assertFalse(self.launch.exists())
        self.assertFalse(Path(row["sentinel"]).exists())
        watch.terminate.assert_called_once()

    def test_owner_start_derives_existing_native_tuple_and_refuses_foreign_or_unknown_before_spawn(self):
        import dispatch_resource_wait as OWNER_RESOURCE
        import owner_route_binding as OWNER
        import dispatch_contract as CONTRACT
        import dispatch_owner_input as INPUT
        join = OWNER_RESOURCE.JOIN
        jobs = self.base / "binding-state/jobs.log"
        jobs.parent.mkdir()
        jobs.write_text("time\topen\t/repo\t/wt\towner\tattempt_id=att-parent,attempt_schema_version=2,worker_type=owner,dispatch_depth=1\n")
        state = CONTRACT.dispatch_state_root(jobs) / "supervisor-state/att-parent.json"
        join.write_supervisor_state(state,"att-parent",set(),phase="running-turn")
        args = SimpleNamespace(jobs=None,parent_attempt_id=None)
        route_file = self.base / "bound-route.json"
        route = {"route_id":"rt-bound","route_hash":"sha256:bound"}
        binding = SimpleNamespace(route_file=str(route_file),route_id="rt-bound",route_hash="sha256:bound")
        native = {"thread_id":"same-native","supervisor_live":True}
        env = {"AGENT_DISPATCH_COMPLETION_MODE":"supervised", "AGENT_DISPATCH_ATTEMPT_ID":"att-parent",
               "AGENT_OWNER_ROUTE_FILE":str(route_file),"AGENT_DISPATCH_JOBS":str(jobs),
               "AGENT_DISPATCH_COMPLETION_STATE_FILE":str(state)}
        with mock.patch.object(OWNER,"resolve_owner_route_lifecycle",return_value=(binding,"bound")), \
             mock.patch.object(CONTRACT,"resolve_live_parent_attempt",return_value=SimpleNamespace(pid=444,pid_start="200")) as live, \
             mock.patch.object(INPUT,"inspect",return_value=native), \
             mock.patch.object(R.subprocess,"Popen") as spawn:
            actual = OWNER_RESOURCE.start_binding(route,route_file,args,env)
            self.assertEqual(actual,{"parent_attempt_id":"att-parent","session_id":"same-native",
                "route_id":"rt-bound","route_hash":"sha256:bound","jobs":str(jobs),"owner_pid":444,"owner_start":"200"})
            self.assertEqual(args.parent_attempt_id,"att-parent")
            old_jobs=jobs.read_text()
            jobs.write_text(old_jobs.rstrip("\n")+",harness=codex\n")
            self.assertEqual(OWNER_RESOURCE.start_binding(route,route_file,args,env)["launch_scope"],"codex-owner-controller")
            jobs.write_text(old_jobs)
            self.assertEqual(live.call_args.kwargs["repo"],"/repo")
            self.assertEqual(live.call_args.kwargs["worktree"],"/wt")
            self.assertEqual(join.read_supervisor_phase_state(state,"att-parent").resource["session_id"],"same-native")
            before = state.read_bytes()
            args.parent_attempt_id="att-foreign"
            with self.assertRaisesRegex(ValueError,"resource-owner-attempt-conflict"):
                OWNER_RESOURCE.start_binding(route,route_file,args,env)
            args.parent_attempt_id="att-parent"
            native["supervisor_live"]=False
            with self.assertRaisesRegex(ValueError,"resource-owner-session-unavailable"):
                OWNER_RESOURCE.start_binding(route,route_file,args,env)
            native.update(supervisor_live=True,thread_id="different-native")
            with self.assertRaisesRegex(ValueError,"resource-owner-session-conflict"):
                OWNER_RESOURCE.start_binding(route,route_file,args,env)
            self.assertEqual(state.read_bytes(),before)
            self.assertEqual(spawn.call_count,0)
            self.assertIsNone(OWNER_RESOURCE.start_binding(route,route_file,args,{}))

    def _resume_route(self):
        evidence = self.base / "headless.json"
        evidence.write_text(json.dumps({"candidates": [{"harness": "codex", "transport": "headless",
            "surface": "registered-headless", "status": "supported", "probe_source": "fixture",
            "probe_time": "2026-10-05T00:00:00Z"}]}))
        prompt = self.base / "request.txt"
        prompt.write_text("Verify the already approved same-code/config resume after it exits.\n")
        result = subprocess.run([sys.executable, str(ROUTER), "compose", "--capability", "autopilot-lab",
            "--capability-mode", "setup", "--shape", "direct", "--graph", "resume-run,run-verify",
            "--slug", "resume-fixture", "--cwd", str(self.repo), "--artifact-root", str(self.artifacts),
            "--registered-headless-evidence", str(evidence), "--unassigned", "--prompt-file", str(prompt)],
            env=CLEAN_ENV, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        route = json.loads(result.stdout)
        self.resume_path = self.artifacts / ".runtime/routes" / (route["route_id"] + ".json")
        self.assertTrue(self.resume_path.is_file())
        self.jobs = self.base / "state/jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        return json.loads(self.resume_path.read_text())

    def test_verified_resume_concurrent_exact_body_starts_once_and_fast_exit_has_identity(self):
        self._resume_route()
        # Failed payload deliberately ends before any verifier can be admitted.
        # This tests real reserve/fence/watch behavior without a model invocation.
        args = ["start", "--run-id", "resume", "--cwd", str(self.repo), "--log", str(self.log),
                "--route", str(self.resume_path), "--node", "resume-run", "--jobs", str(self.jobs),
                "--", sys.executable, "-c", f"from pathlib import Path; p=Path({str(self.launch)!r}); "
                "p.open('a').write('x'); raise SystemExit(7)"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self.cli(*args), range(2)))
        for outcome in outcomes:
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
        receipts = [json.loads(o.stdout) for o in outcomes]
        self.assertEqual(sum(r["payload_spawned"] for r in receipts), 1)
        self.assertEqual(sum(r["replayed"] for r in receipts), 1)
        started = next(r for r in receipts if r["payload_spawned"])
        self.assertTrue(started["supervision"]["starttime"])
        self.assertFalse(started["verification_admitted"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = json.loads(self.registry.read_text())["runs"]["resume"]
            if row["status"] == "failed": break
            time.sleep(.05)
        self.assertEqual(self.launch.read_text(), "x")
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(row["failure_class"], "exit-7")
        self.assertTrue(row["starttime"])
        replay = self.cli(*args)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertFalse(json.loads(replay.stdout)["payload_spawned"])
        changed = self.cli(*args[:-1], "raise SystemExit(9)")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("resource-route-body-conflict", changed.stderr)
        different_run = list(args)
        different_run[different_run.index("--run-id")+1] = "another"
        self.assertIn("resource-route-body-conflict", self.cli(*different_run).stderr)
        self.assertEqual(self.launch.read_text(), "x")
        self.assertEqual(self.jobs.read_text(), "")

    def test_verified_resume_watch_spawn_failure_releases_no_payload(self):
        route = self._resume_route()
        args = type("Args", (), {"jobs": str(self.jobs), "node": "resume-run", "run_id": "resume"})()
        placeholder = {"run_id": "resume", "route": str(self.resume_path), "node": "resume-run",
            "cwd": str(self.repo), "log": str(self.log), "sentinel": str(self.log)+".exit",
            "command": [sys.executable, "-c", f"open({str(self.launch)!r},'w').write('wrong')"],
            "status": "launching", "workflow_state": "READY"}
        original = subprocess.Popen
        watch_spawn_calls = []
        def spawn(argv, **kwargs):
            if "watch" in argv:
                watch_spawn_calls.append(tuple(argv))
                raise OSError("synthetic watch spawn failure")
            return original(argv, **kwargs)
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT), "AGENT_WORKFLOW_ROOT": str(self.base/"workflow"),
                                         "AGENT_RESOURCE_RUN_INDEX": str(self.index), "CUDA_VISIBLE_DEVICES": ""}), \
             mock.patch.object(R.gpu_leases, "local_observation",
                               side_effect=AssertionError("CPU fixture reached local GPU observer")) as observer, \
             mock.patch.object(R.subprocess, "Popen", side_effect=spawn):
            R.start_verified(self.registry, args, route, self.resume_path, placeholder)
            observer.assert_not_called()
        self.assertEqual(len(watch_spawn_calls), 1)
        self.assertIn("watch", watch_spawn_calls[0])
        self.assertFalse(self.launch.exists())
        failed = json.loads(self.registry.read_text())["runs"]["resume"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["launch_error"],
                         {"type": "OSError", "message": "synthetic watch spawn failure"})
        self.assertEqual(self.jobs.read_text(), "")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Stop payloads and their separate workflow observers before removing
        # the directory: either can still be writing after the payload exits.
        self.addCleanup(self._stop_started_runs)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "config").write_text("ok\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir()
        evidence = self.base / "dispatch-evidence.json"
        evidence.write_text(json.dumps({"tuples": [{
            "harness": "codex", "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported", "probe_source": "test",
            "probe_time": "2026-07-27T00:00:00Z", "failure_class": "none",
            "checked_worktree": str(self.repo.resolve()), "failure_scope": "none",
            "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}))
        result = subprocess.run([
            sys.executable, str(ROUTER), "compile", "--slug", "resource-runner-fixture",
            "--capability", "autopilot-lab",
            "--capability-mode", "setup", "--intensity", "auto", "--signal", "resource-run",
            "--cwd", str(self.repo), "--artifact-root", str(self.artifacts),
            "--dispatch-evidence", str(evidence), "--tracking", "untracked",
            "--spec-read", "not-applicable", "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked", "--artifact-guard", "preflight-passed",
        ], text=True, capture_output=True, env=CLEAN_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        compiled = json.loads(result.stdout)
        self.route = (
            self.artifacts / ".runtime" / "routes" / f"{compiled['route_id']}.json"
        )
        self.assertTrue(self.route.is_file())
        self.registry = self.base / "registry.json"
        self.index = self.base / "resource-runs.index.json"
        self.log = self.base / "logs" / "run.log"
        self.launch = self.base / "launched"
        self.attestation = self.base / "smoke.json"
        subprocess.run([
            sys.executable, str(SMOKE), "attest", "--input", str(self.repo / "config"),
            "--cwd", str(self.repo), "--output", str(self.attestation), "--", sys.executable, "-c", "pass",
        ], check=True, stdout=subprocess.DEVNULL, env=CLEAN_ENV)

    def test_routed_start_defaults_registry_worktree_log_route_and_node(self):
        import artifact_producer
        jobs = self.base / "dispatch-jobs.log"
        jobs.write_text("")
        artifact_producer.activate(self.artifacts, repository_id="repo_" + "a" * 32,
                                   artifact_root_id="root_" + "b" * 32)
        route = artifact_producer.load_route(self.artifacts, self.route)
        node = next(row for row in route["nodes"] if row.get("id") == "full-run")
        begun = artifact_producer.begin(self.artifacts, route_file=self.route,
            capability=route["capability"], intensity=route["effective_intensity"],
            jobs=jobs, require_cycle=True)
        env = {**CLEAN_ENV, "AGENT_HOME": str(ROOT),
            "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_DISPATCH_WORKTREE": str(self.repo),
            "AGENT_ROUTE_FILE": str(self.route), "AGENT_ROUTE_ID": route["route_id"],
            "AGENT_ROUTE_NODE": node["id"], "AGENT_ARTIFACT_ROOT": str(self.artifacts),
            "AGENT_RESOURCE_RUN_INDEX": str(self.index), "AGENT_WORKFLOW_ROOT": str(self.base / "workflow"),
            **begun["env"]}
        command = [sys.executable, str(RUNNER), "start", "--run-id", "route-defaults",
                   "--smoke-attestation", str(self.attestation), "--", sys.executable, "-c", "pass"]
        result = subprocess.run(command, cwd=self.repo, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = Path(begun["env"]["AGENT_ARTIFACT_OUTPUT_DIR"])
        registry = output / "resource-runs.json"
        self.assertTrue(registry.is_file())
        self.assertIn("route-defaults", json.loads(registry.read_text())["runs"])
        self.assertTrue((output / "logs/route-defaults.log").is_file())
        reader = subprocess.run([sys.executable, str(ROOT / "utilities" / "artifact_reader.py"),
            "resolve", "--cycle", begun["cycle_id"]], cwd=self.repo, env=env,
            text=True, capture_output=True, timeout=20)
        self.assertEqual(reader.returncode, 0, reader.stdout + reader.stderr)
        self.assertTrue(json.loads(reader.stdout))
        resolved = subprocess.run([sys.executable, str(ROOT / "utilities" / "artifact_producer.py"),
            "resolve-output", "--bucket", "logs"], cwd=self.repo, env=env,
            text=True, capture_output=True, timeout=20)
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["output_dir"], str(output / "logs"))

    def _stop_started_runs(self):
        from dispatch_contract import process_identity_disposition
        try:
            runs = json.loads(self.registry.read_text()).get("runs", {})
        except (AttributeError, OSError, ValueError):
            return
        own = os.getpgrp()
        groups = {run.get("process_group") for run in runs.values() if isinstance(run, dict)}
        for group in groups:
            try:
                group = int(group)
                if group <= 1 or group == own:
                    continue  # fixtures record this test's own group on purpose
                os.killpg(group, signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                continue
            for _ in range(50):
                try:
                    os.killpg(group, 0)
                except OSError:
                    break
                time.sleep(0.02)
        for run in runs.values():
            supervision = run.get("supervision") if isinstance(run, dict) else None
            if not R.RESOURCE_RESUME.supervisor_alive(supervision):
                continue
            pid = int(supervision["pid"])
            if pid <= 1 or pid == os.getpid():
                continue  # fixtures may deliberately record this test process
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            deadline = time.monotonic() + 3
            # Command metadata may become unreadable before exit is waitable.
            # Keep unknown pending; the exact PID/start must positively exit.
            def disposition():
                return process_identity_disposition(pid, str(supervision["starttime"]))
            while disposition() != "dead" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(disposition(), "dead",
                             f"fixture workflow observer {pid} did not exit")

    def cli(self, *args, cwd=None, pass_fds=(), input=None, extra_env=None):
        return subprocess.run([
            sys.executable, str(RUNNER), "--registry", str(self.registry), *args,
        ], cwd=cwd, text=True, capture_output=True, pass_fds=pass_fds, input=input,
           env={**CLEAN_ENV, "AGENT_HOME": str(ROOT),
                "AGENT_RESOURCE_RUN_INDEX": str(self.index), **(extra_env or {})})

    def start_args(self, route=None, node="full-run", smoke=None, run_id="case", config_manifest=None):
        return ("start", "--run-id", run_id, "--cwd", str(self.repo), "--log", str(self.log),
                "--route", str(route or self.route), "--node", node,
                *(('--smoke-attestation', str(smoke)) if smoke is not None else ()),
                *(('--config-manifest', str(config_manifest)) if config_manifest is not None else ()),
                "--", sys.executable, "-c", f"from pathlib import Path; import time; Path({str(self.launch)!r}).write_text('launched'); time.sleep(30)")

    def seal_config_manifest(self, label):
        PROV = ROOT / "tools" / "lab-config-provenance.py"
        artifact_root = self.base / f"artifacts-{label}"
        artifact_root.mkdir()
        result = subprocess.run([
            sys.executable, str(PROV), "seal", "--repo", str(self.repo), "--config", "./config",
            "--slug", "demo", "--artifact-root", str(artifact_root),
        ], check=True, capture_output=True, text=True, env=CLEAN_ENV)
        computed_run_id = json.loads(result.stdout)["run_id"]
        return artifact_root / "experiments" / "demo" / "_internal" / "configs" / f"{computed_run_id}.manifest.json"

    def attest_with_config_manifest(self, manifest_path, name):
        attestation = self.base / f"{name}.json"
        subprocess.run([
            sys.executable, str(SMOKE), "attest", "--input", str(self.repo / "config"),
            "--config-manifest", str(manifest_path), "--cwd", str(self.repo),
            "--output", str(attestation), "--", sys.executable, "-c", "pass",
        ], check=True, stdout=subprocess.DEVNULL, env=CLEAN_ENV)
        return attestation

    def assert_rejected_before_side_effects(self, *args, cli_cwd=None):
        result = self.cli(*args, cwd=cli_cwd)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists(), result.stderr)
        self.assertFalse(self.registry.exists(), result.stderr)
        self.assertFalse(self.launch.exists(), result.stderr)

    def test_pid_identity_and_registry(self):
        identity = R.proc_identity(os.getpid())
        self.assertTrue(identity)
        self.assertTrue(R.alive(identity))
        identity["starttime"] = "0"
        self.assertFalse(R.alive(identity))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.json"
            R.locked_update(path, lambda data: data["runs"].update(x={"pid": 1}))
            self.assertIn("x", json.loads(path.read_text())["runs"])

    def test_actual_cli_rejects_every_invalid_launch_proof_before_side_effects(self):
        cases = [
            ("omitted route", tuple(x for x in self.start_args() if x not in ("--route", str(self.route)))),
            ("omitted node", tuple(x for x in self.start_args() if x not in ("--node", "full-run"))),
            ("unknown node", self.start_args(node="missing")),
            ("missing smoke", self.start_args(smoke=None)),
            ("invalid smoke", self.start_args(smoke=self.base / "missing-smoke.json")),
        ]
        # Omitted flags are represented explicitly to ensure argparse rejects them.
        for name, args in cases:
            with self.subTest(name=name):
                self.assert_rejected_before_side_effects(*args)

        linked = self.base / "linked-route.json"
        linked.symlink_to(self.route)
        self.assert_rejected_before_side_effects(*self.start_args(route=linked, smoke=self.attestation))

        for name, mutate in (
            ("tampered route", lambda row: row.update(route_id="rt-tampered")),
            ("stale source commit", lambda row: row.update(source_commit="0" * 40)),
            ("wrong kind", lambda row: next(n for n in row["nodes"] if n["id"] == "full-run").update(kind="pipeline-stage")),
            ("wrong resource transport", lambda row: next(n for n in row["nodes"] if n["id"] == "full-run").update(resource_transport="inline")),
        ):
            row = json.loads(self.route.read_text())
            mutate(row)
            row.pop("route_hash", None)
            row.pop("route_id", None)
            bare = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            row["route_hash"] = "sha256:" + hashlib.sha256(bare).hexdigest()
            row["route_id"] = "rt-" + row["route_hash"].split(":", 1)[1][:16]
            candidate = self.base / f"{name.replace(' ', '-')}.json"
            candidate.write_text(json.dumps(row))
            with self.subTest(name=name):
                self.assert_rejected_before_side_effects(*self.start_args(route=candidate, smoke=self.attestation))

        other = self.base / "other-cwd"
        other.mkdir()
        wrong_cwd_args = list(self.start_args(smoke=self.attestation))
        wrong_cwd_args[wrong_cwd_args.index("--cwd") + 1] = str(other)
        self.assert_rejected_before_side_effects(*wrong_cwd_args)

    def test_valid_detached_start_status_stop_cleans_process_group(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), repr(result))
        run = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(self.log.exists())
        status = self.cli("status", "--run-id", "case")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["run_id"], "case")
        stop = self.cli("stop", "--run-id", "case")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        for _ in range(50):
            # Empty zombie cmdline loses identity before the subreaper removes
            # /proc. Wait for the disappearance asserted below (2026-10-25).
            if not Path(f"/proc/{run['pid']}").exists():
                break
            time.sleep(0.02)
        self.assertFalse(R.proc_identity(run["pid"]))
        self.assertEqual(os.getpgid(run["pid"]) if Path(f"/proc/{run['pid']}").exists() else None, None)

    def test_stop_rejects_changed_process_group_identity(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout.strip().splitlines()[-1])
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(process_group=run["process_group"] + 1),
        )
        stop = self.cli("stop", "--run-id", "case")
        self.assertNotEqual(stop.returncode, 0)
        self.assertIsNotNone(R.proc_identity(run["pid"]))
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(process_group=run["process_group"]),
        )
        self.assertEqual(self.cli("stop", "--run-id", "case").returncode, 0)

    def test_stop_rejects_command_identity_mismatch_without_signal(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout.strip().splitlines()[-1])
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(command_hash="0" * 64),
        )
        stop = self.cli("stop", "--run-id", "case")
        self.assertNotEqual(stop.returncode, 0)
        self.assertIsNotNone(R.proc_identity(run["pid"]))
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(command_hash=run["command_hash"]),
        )
        self.assertEqual(self.cli("stop", "--run-id", "case").returncode, 0)

    def test_index_existing_registry_does_not_restart_process(self):
        legacy = self._legacy_row("existing")
        R.locked_update(self.registry, lambda data: data["runs"].update(existing=legacy))
        before = R.proc_identity(os.getpid())
        result = subprocess.run(
            [sys.executable, str(RUNNER), "index", "--registry", str(self.registry)],
            text=True, capture_output=True,
            env={**CLEAN_ENV, "AGENT_RESOURCE_RUN_INDEX": str(self.index)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(R.proc_identity(os.getpid()), before)
        payload = json.loads(self.index.read_text())
        self.assertEqual(
            [row["path"] for row in payload["registries"].values()],
            [str(self.registry.resolve())],
        )

    def _legacy_row(self, run_id):
        identity = R.proc_identity(os.getpid())
        return {**identity, "run_id": run_id, "process_group": os.getpgid(os.getpid()),
                "cwd": str(self.repo), "log": str(self.log), "command": ["true"],
                "route": str(self.route), "node": "full-run", "status": "running"}

    # T7 -- a running legacy process (registry row predating the config
    # fields) must keep the same status/alive() judgment under the new code.
    def test_legacy_run_row_without_config_fields_is_unaffected(self):
        legacy_run = self._legacy_row("legacy")
        R.locked_update(self.registry, lambda data: data["runs"].update(legacy=legacy_run))
        self.assertTrue(R.alive(legacy_run))
        result = self.cli("status", "--run-id", "legacy")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["run_id"], "legacy")
        self.assertEqual(row["status"], "running")
        self.assertNotIn("config_ref", row)
        self.assertNotIn("config_sha256", row)

    # T13 -- an existing_run_exception recorded in run.json (a surface the
    # harness never writes) must not cause the registry row to be rewritten,
    # and the original worktree/command/config path/run ID stay intact.
    def test_existing_run_exception_in_run_json_does_not_trigger_a_registry_rewrite(self):
        legacy_run = self._legacy_row("legacy")
        R.locked_update(self.registry, lambda data: data["runs"].update(legacy=legacy_run))
        registry_before = self.registry.read_bytes()
        run_json = self.base / "run.json"
        run_json.write_text(json.dumps({
            "worktree": str(self.repo), "command": ["true"], "config_path": "config",
            "run_id": "legacy",
            "existing_run_exception": {
                "reason": "policy predates this run", "policy_version": "2026-08-03",
                "applies_from": "2026-08-04",
            },
        }, indent=2))
        run_json_before = run_json.read_bytes()
        status = self.cli("status", "--run-id", "legacy")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual(run_json.read_bytes(), run_json_before)
        row = json.loads(status.stdout)
        self.assertEqual(row["cwd"], str(self.repo))
        self.assertEqual(row["command"], ["true"])
        self.assertEqual(row["run_id"], "legacy")

    # T14 -- adding the new provenance fields must not make Fleet mistake an
    # ordinary process for a separate training run: the registry gains
    # exactly one row per start, never a phantom second one.
    def test_ordinary_start_creates_exactly_one_registry_row(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.registry.read_text())
        self.assertEqual(len(data["runs"]), 1)
        self.assertNotIn("config_ref", data["runs"]["case"])
        indexed = json.loads(self.index.read_text())
        self.assertEqual(
            [record["path"] for record in indexed["registries"].values()],
            [str(self.registry.resolve())],
        )

    # B3 regression -- a config manifest whose run_id disagrees with --run-id
    # (and isn't a valid --attempt suffix of it) is rejected before Popen.
    def test_config_manifest_run_id_mismatch_is_rejected_before_side_effects(self):
        manifest = self.seal_config_manifest("sealed-run")
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-mismatch")
        self.assert_rejected_before_side_effects(
            *self.start_args(smoke=attestation, run_id="mismatched-run-id", config_manifest=manifest))

    # B3 regression -- on a match, the registry key and the row's run_id
    # field are always identical (never split by the manifest's own run_id).
    def test_config_manifest_matching_run_id_binds_registry_key_to_row_field(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-match")
        result = self.cli(*self.start_args(smoke=attestation, run_id=manifest_run_id, config_manifest=manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(run["run_id"], manifest_run_id)
        data = json.loads(self.registry.read_text())
        self.assertIn(manifest_run_id, data["runs"])
        self.assertEqual(data["runs"][manifest_run_id]["run_id"], manifest_run_id)
        self.assertEqual(data["runs"][manifest_run_id]["config_ref"], "path:config")

    # B3 regression -- the documented "__aN" attempt-suffix policy: a
    # registry key that retries the same sealed manifest under a distinct
    # key is accepted, and the row still carries the registry key, not the
    # manifest's run_id.
    def test_config_manifest_attempt_suffix_is_accepted(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-attempt")
        attempt_run_id = f"{manifest_run_id}__a2"
        result = self.cli(*self.start_args(smoke=attestation, run_id=attempt_run_id, config_manifest=manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.registry.read_text())
        self.assertIn(attempt_run_id, data["runs"])
        self.assertEqual(data["runs"][attempt_run_id]["run_id"], attempt_run_id)

    # T-F3-5 (A9): every one of these must be rejected before Popen, whether
    # by safe_run_id() or by the exact-match-then-regex attempt policy.
    def test_T_F3_5_unsafe_or_invalid_run_ids_are_rejected(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-unsafe")
        cases = [
            "run__a0", "run__a01", "run__a", "run__aX", "run__a1x", "run__a-1", "../evil",
            # regex matches, but base != manifest_run_id after the split -- must
            # still be rejected by the exact-match-first ordering (A9).
            f"{manifest_run_id}__a1__a2",
        ]
        for bad in cases:
            with self.subTest(run_id=bad):
                self.assert_rejected_before_side_effects(
                    *self.start_args(smoke=attestation, run_id=bad, config_manifest=manifest))

    # T-F9-1: the full provenance field set is visible on `status`.
    def test_T_F9_1_status_exposes_the_full_provenance_field_set(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-fields")
        result = self.cli(*self.start_args(smoke=attestation, run_id=manifest_run_id, config_manifest=manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.cli("status", "--run-id", manifest_run_id)
        self.assertEqual(status.returncode, 0, status.stderr)
        row = json.loads(status.stdout)
        for key in ("run_id", "config_ref", "config_sha256", "source_commit",
                    "source_dirty", "source_git_state", "config_layout"):
            self.assertIn(key, row)

    # T-F9-2: status/stop/tail never create a process -- exactly one registry
    # row for one start, no phantom second run.
    def test_T_F9_2_read_commands_never_create_processes_or_rows(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.cli("status", "--run-id", "case")
        self.cli("tail", "--run-id", "case")
        self.cli("stop", "--run-id", "case")
        self.cli("status", "--run-id", "case")
        data = json.loads(self.registry.read_text())
        self.assertEqual(len(data["runs"]), 1)

    # T-F7-6: tampering the config source after attest (before start) must be
    # rejected before any process, log, or registry row is created.
    def test_T_F7_6_source_tamper_after_attest_is_rejected_before_side_effects(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-tamper")
        m = json.loads(manifest.read_text())
        Path(m["source_path"]).write_text("tampered-after-attest")
        self.assert_rejected_before_side_effects(
            *self.start_args(smoke=attestation, run_id=manifest_run_id, config_manifest=manifest))

    # T-G4-7: a smoke attestation missing its required hash must be rejected
    # by `start` before Popen, the registry row, or the log file exist.
    def test_T_G4_7_missing_attestation_hash_is_rejected_before_side_effects(self):
        data = json.loads(self.attestation.read_text())
        del data["attestation_hash"]
        broken = self.base / "broken-hash-smoke.json"
        broken.write_text(json.dumps(data))
        self.assert_rejected_before_side_effects(*self.start_args(smoke=broken))

    # T-G4-8: a smoke attestation with only partial config-provenance metadata
    # (one of the three fields dropped, hash recomputed over the rest) must
    # be rejected the same way -- config provenance is all-or-none.
    def test_T_G4_8_partial_config_metadata_is_rejected_before_side_effects(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-partial")
        data = json.loads(attestation.read_text())
        del data["config_source_path"]
        del data["attestation_hash"]
        data["attestation_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        broken = self.base / "broken-partial-smoke.json"
        broken.write_text(json.dumps(data))
        self.assert_rejected_before_side_effects(
            *self.start_args(smoke=broken, run_id=manifest_run_id, config_manifest=manifest))


if __name__ == "__main__":
    unittest.main()
