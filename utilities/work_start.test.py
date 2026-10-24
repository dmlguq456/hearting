#!/usr/bin/env python3
"""Public start orchestration; outcome authority remains the production join.

Admission and observation are controlled here to exercise crash/replay and
refusal boundaries. Actual claim/process/closure tests live in their owners.
"""
import json
import contextlib
import io
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import work_start as W
from dispatch_completion_join import CurrentDeliveryState


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class WorkStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.route = {"route_id": "rt-probe", "route_hash": "sha256:probe", "slug": "task",
            "effective_intensity": "standard", "work_request": {"text": "Run the two commands", "owner_harness": "codex"},
            "nodes": [{"id": n, "worker_type": "frame", "dispatch_depth": 1} for n in ("frame", "frame-alternative")]
                     + [{"id": "test", "depends_on": ["frame", "frame-alternative"]}],
            "dispatch_evidence": {"tuples": [{"child_harness": h, "status": "supported"} for h in ("codex", "opencode")]}}
        self.path = Path(self.tmp.name) / "route.json"
        self.path.write_text(json.dumps(self.route))
        self.calls = []
        self.ready = False
        self.released = False
        self.statuses = {}
        self.parent = mock.patch.object(W, "default_parent_session_id", return_value="parent")
        self.parent.start(); self.addCleanup(self.parent.stop)
        self.environment = mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "0"})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.join = mock.patch.object(W, "join_selected_attempts", side_effect=self.observe)
        self.join.start(); self.addCleanup(self.join.stop)
        self.gate = mock.patch.object(W, "owner_frame_launch_gate", side_effect=self.owner_gate)
        self.gate.start(); self.addCleanup(self.gate.stop)
        self.pair = mock.patch.object(W, "completion_marker_gate")
        self.pair.start(); self.addCleanup(self.pair.stop)
        self.current = mock.patch.object(W, "current_delivery_state", side_effect=self.delivery)
        self.current.start(); self.addCleanup(self.current.stop)
        # This orchestration suite controls observations as well as launch receipts.
        # Real process identity and registered payload preservation have separate tests.
        import route_parent_close as close
        import dispatch_contract as contract
        original_observe = close.owner_continuation_processes
        def owner_observation(meta, jobs):
            if meta.get("pid"):
                return original_observe(meta, jobs)
            status = W._rows(jobs).get(meta.get("attempt_id"), ("done", {}))[0]
            return contract.ProcessQuiescence("live" if status in {"open", "running"} else "quiescent", "fixture-observation"), []
        self.process = mock.patch.object(close, "owner_continuation_processes", side_effect=owner_observation)
        self.process.start(); self.addCleanup(self.process.stop)

    def test_start_work_surfaces_consumed_tombstone_restore_and_keeps_advancing(self):
        outcome = {"restored": [{"node": "plan-check"}], "errors": ["pending-restore-invalid:leaf"]}
        module = SimpleNamespace(restore_consumed_tombstones=mock.Mock(return_value=outcome))
        advanced = []
        def advance(route, path, jobs, result, **kwargs):
            advanced.append(True)
            return {**result, "state": "ready"}
        with mock.patch("route_parent_close.intent", return_value=None), \
                mock.patch.object(W, "_route_module", return_value=module), \
                mock.patch.object(W, "_advance", side_effect=advance), \
                mock.patch.object(W, "_advance_plan", side_effect=lambda _route, _jobs, result, **kw: result), \
                mock.patch.object(W, "_arm_capacity_resume", side_effect=lambda result, *_args: result), \
                mock.patch("start_receipt.save", side_effect=lambda result, _jobs: result):
            result = W.start_work(self.route, self.path, self.jobs)
        self.assertEqual(result["restored"], ["plan-check"])
        self.assertEqual(result["restore_errors"], ["pending-restore-invalid:leaf"])
        self.assertTrue(advanced)

    def test_legacy_continuation_normal_start_reads_original_request_without_rewriting(self):
        source = json.loads(json.dumps(self.route))
        source_path = self.path.with_name("source.json")
        source_path.write_text(json.dumps(source))
        self.route = {**self.route, "route_id": "rt-continuation", "route_hash": "sha256:continued",
                      "source_route_id": source["route_id"], "source_route_hash": source["route_hash"],
                      "nodes": [{"id": "review"}]}
        self.route.pop("work_request")
        self.path.write_text(json.dumps(self.route))
        before = self.path.read_bytes()
        self.ready = self.released = True
        module = SimpleNamespace(resolve_route_argument=lambda *a: source_path,
                                 verify_route=lambda value: value,
                                 restore_consumed_tombstones=lambda *a, **kw: {"restored": [], "errors": []})
        with mock.patch.object(W, "_route_module", return_value=module):
            first = self.start()
            second = self.start()
        self.assertTrue(first["owner_started"])
        self.assertTrue(second["owner_started"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][self.calls[0].index("--prompt-text") + 1], source["work_request"]["text"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("work_request", self.route)

    def test_start_and_resume_prepare_the_same_canonical_root_request(self):
        primary = Path(self.tmp.name) / "primary"
        project = Path(self.tmp.name) / "linked"
        primary.mkdir()
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "fixture-linked", str(project)], check=True)
        artifact_root = primary / ".agent_reports"
        artifact_root.mkdir()
        manifest = artifact_root / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "done <<'ROOTS'\nalpha|/tmp/canonical-alpha/.agent_reports\n"
            "beta|/tmp/beta/.agent_reports\nextra|/tmp/extra/.agent_reports\nROOTS\n",
            encoding="utf-8",
        )
        shadow = project / ".agent_reports/_scratch/flow/previews/run_all.sh"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("done <<'ROOTS'\nalpha|/tmp/shadow-alpha/.agent_reports\nROOTS\n", encoding="utf-8")
        route = {
            **self.route,
            "route_id": "rt-direct-targets",
            "route_hash": "sha256:" + "a" * 64,
            "cwd": str(project),
            "artifact_root": str(artifact_root),
            "work_request": {"text": (
                "## 입력\n"
                "- 미리보기(사용자가 본 것): .agent_reports/_scratch/flow/previews/<루트>.md\n"
                "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha, beta)\n"
            )},
        }
        route_file = Path(self.tmp.name) / "direct-route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "started=1\n", "")

        with mock.patch.dict(os.environ, {}, clear=False):
            for name in list(os.environ):
                if name.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_")):
                    os.environ.pop(name, None)
            result = W._start(route, route_file, self.jobs, "owner", "codex", run)
            W._start(route, route_file, self.jobs, "owner", "codex", run)
        self.assertEqual("", result.get("execution_access_diagnostic", ""))
        self.assertIn("--execution-access-file", calls[0])
        request_path = Path(calls[0][calls[0].index("--execution-access-file") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(
            ["/tmp/beta/.agent_reports", "/tmp/canonical-alpha/.agent_reports"],
            request["writable_roots"],
        )

        # start and resume use the same preparation entry point and therefore
        # reuse the exact route-bound request, including its full digest.
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0][calls[0].index("--execution-access-file") + 1],
                         calls[1][calls[1].index("--execution-access-file") + 1])
        request_path = Path(calls[1][calls[1].index("--execution-access-file") + 1])
        self.assertEqual(request, json.loads(request_path.read_text()))
        from execution_access import AccessContext
        access_env = {"HOME": str(Path(self.tmp.name) / "home")}
        context = AccessContext.build(
            worktree=project,
            artifact_root=artifact_root,
            dispatch_state_root=Path(self.tmp.name) / "dispatch-state",
            agent_home=W.ROOT,
            environ=access_env,
        )
        for adapter_name, runtime in (("codex", "codex-exec"), ("claude", "claude-cli"),
                                      ("opencode", "opencode")):
            adapter = load("work_start_access_" + adapter_name,
                           W.ROOT / "adapters" / adapter_name / "bin/dispatch-headless.py")
            grant = adapter.bind_execution_access_request(
                str(request_path), environ=access_env, context=context,
                is_child=False, parent=None, runtime=runtime,
            )
            self.assertEqual(request_path.resolve(), grant.source_path)
            self.assertEqual(tuple(Path(value) for value in request["writable_roots"]), grant.writable_roots)

        explicit_calls = []
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": "/tmp/existing-request.json"}):
            W._start(route, route_file, self.jobs, "owner", "codex",
                     lambda command, **kwargs: (explicit_calls.append(command)
                         or subprocess.CompletedProcess(command, 0, "started=1\n", "")))
        self.assertNotIn("--execution-access-file", explicit_calls[0])

    def test_invalid_explicit_roots_stop_before_launch_with_typed_error(self):
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        artifact_root = project / ".agent_reports"
        manifest = artifact_root / "_scratch/flow/previews/run_all.sh"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("done <<'ROOTS'\nalpha|relative/root\nROOTS\n", encoding="utf-8")
        route = {
            **self.route,
            "route_id": "rt-invalid-targets",
            "route_hash": "sha256:" + "b" * 64,
            "cwd": str(project),
            "artifact_root": str(artifact_root),
            "work_request": {"text": (
                "## 입력\n"
                "- 미리보기(사용자가 본 것): .agent_reports/_scratch/flow/previews/<루트>.md\n"
                "- 루트 목록과 경로: previews/run_all.sh 의 ROOTS 표(alpha)\n"
            ), "owner_harness": "codex"},
        }
        route_file = Path(self.tmp.name) / "invalid-route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        calls = []
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
            result = W.start_work(route, route_file, self.jobs,
                                  run=lambda command, **kwargs: calls.append(command))
        self.assertEqual([], calls)
        self.assertEqual("needs-attention", result["state"])
        self.assertEqual("execution-access-target-input-invalid", result["reason"])
        self.assertIn("reason=execution-access-target-input-invalid", result["launches"][0]["receipt"])
        self.assertIn("child_spawned=0", result["launches"][0]["receipt"])

    def test_lab_owner_start_and_resume_pass_inventory_and_explicit_data_to_dispatch(self):
        root = Path(self.tmp.name)
        inventory = root / "configuration" / "compute-hosts.yaml"
        inventory.parent.mkdir()
        run_root = root / "custom-runs"
        data_root = root / "approved-data"
        inventory.write_text(
            f"schema_version: 1\nrun_root: {run_root}\nhosts:\n  fixture:\n    ssh_host: local\n")
        explicit = root / "data-access.json"
        explicit.write_text(json.dumps({
            "schema_version": 1, "writable_roots": [str(data_root)], "read_roots": [],
            "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "os-sandbox", "justification": {str(data_root): "approved output"},
        }))
        original = explicit.read_bytes()
        route = {**self.route, "route_id": "rt-lab-data", "route_hash": "sha256:" + "c" * 64,
                 "capability": "autopilot-lab", "cwd": str(root / "worktree"),
                 "artifact_root": str(root / "artifacts"), "nodes": [{"id": "full-run"}]}
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "started=1\n", "")

        with mock.patch.dict(os.environ, {
            "HOME": str(root / "home"), "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(explicit),
        }, clear=True):
            W._start(route, self.path, self.jobs, "owner", "codex", run)
            W._start(route, self.path, self.jobs, "owner", "codex", run)
        paths = [Path(command[command.index("--execution-access-file") + 1]) for command in calls]
        self.assertEqual(paths[0], paths[1])
        request = json.loads(paths[0].read_text())
        self.assertEqual({str(data_root), str(run_root)}, set(request["writable_roots"]))
        self.assertEqual([str(inventory.parent)], request["read_roots"])
        self.assertFalse(request["network"]["required"])
        self.assertEqual("os-sandbox", request["enforcement_required"])
        self.assertEqual(original, explicit.read_bytes())
        self.assertFalse(data_root.exists())
        self.assertFalse(run_root.exists())

        given_route = {**route, "route_id": "rt-lab-parent-given",
                       "route_hash": "sha256:" + "f" * 64}
        given_route_file = root / "parent-given-route.json"
        given_route_file.write_text(json.dumps(given_route))
        from execution_access import AccessContext, load_request, normalized_request
        import route_authority as RA
        given_data = root / "parent-given.json"
        given_data.write_text(json.dumps({
            "schema_version": 1, "writable_roots": [str(data_root)],
            "read_roots": [], "network": {"required": False, "reason": "", "hosts": []},
            "enforcement_required": "any", "justification": {str(data_root): "parent-approved output"},
        }))
        context = AccessContext.build(
            worktree=given_route["cwd"], artifact_root=given_route["artifact_root"],
            dispatch_state_root=root, agent_home=W.ROOT,
            environ={"HOME": str(root / "home")},
        )
        given_request = load_request(given_data, context=context)
        RA.record_access_change(
            given_route, request=normalized_request(given_request),
            request_sha256=given_request.request_sha256,
            by={"harness": "codex", "session_id": "fixture-parent"}, source="unattributed",
        )
        with mock.patch.dict(os.environ, {"HOME": str(root / "home"),
                                           "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            W._start(given_route, given_route_file, self.jobs, "owner", "codex", run)
        given_path = Path(calls[-1][calls[-1].index("--execution-access-file") + 1])
        combined = json.loads(given_path.read_text())
        self.assertEqual({str(data_root), str(run_root)}, set(combined["writable_roots"]))
        self.assertFalse(combined["network"]["required"])

        explicit.write_text("{")
        calls.clear()
        with mock.patch.dict(os.environ, {
            "HOME": str(root / "home"), "COMPUTE_HOSTS_CONFIG": str(inventory),
            "AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(explicit),
        }, clear=True):
            result = W._start(route, self.path, self.jobs, "owner", "codex", run)
        self.assertEqual([], calls)
        self.assertEqual(69, result["exit_code"])
        self.assertIn("execution-access-invalid-json", result["execution_access_diagnostic"])

    def test_real_start_prepares_equal_native_and_borrowed_compute_defaults_and_none_for_plain_routes(self):
        root = Path(self.tmp.name)
        inventory = root / "configuration" / "compute-hosts.yaml"
        inventory.parent.mkdir()
        run_root = root / "shared-runs"
        inventory.write_text(
            f"schema_version: 1\nrun_root: {run_root}\nhosts:\n  fixture:\n    ssh_host: local\n")
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "started=1\n", "")

        native = {**self.route, "route_id": "rt-native", "route_hash": "sha256:" + "d" * 64,
                  "capability": "autopilot-lab", "cwd": str(root / "native"),
                  "artifact_root": str(root / "native-artifacts"), "nodes": [{"id": "full-run"}]}
        borrowed = {**self.route, "route_id": "rt-borrowed", "route_hash": "sha256:" + "e" * 64,
                    "capability": "autopilot-code", "cwd": str(root / "borrowed"),
                    "artifact_root": str(root / "borrowed-artifacts"),
                    "nodes": [{"id": "lab-full-run", "part": "autopilot-lab:full-run"}]}
        with mock.patch.dict(os.environ, {"HOME": str(root / "home"),
                                           "COMPUTE_HOSTS_CONFIG": str(inventory)}, clear=True):
            requests = []
            for route in (native, borrowed):
                route_file = root / f"{route['route_id']}.json"
                route_file.write_text(json.dumps(route))
                W._start(route, route_file, root / "jobs.log", "owner", "codex", run)
                request_file = Path(calls[-1][calls[-1].index("--execution-access-file") + 1])
                requests.append(json.loads(request_file.read_text()))
            self.assertEqual(requests[0]["writable_roots"], requests[1]["writable_roots"])
            self.assertEqual([str(run_root)], requests[0]["writable_roots"])
            self.assertEqual(requests[0]["read_roots"], requests[1]["read_roots"])
            self.assertEqual([str(inventory.parent)], requests[0]["read_roots"])
            self.assertEqual(requests[0]["network"], requests[1]["network"])
            self.assertTrue(requests[0]["network"]["required"])
            from execution_access import COMPUTE_NETWORK_REASON
            self.assertEqual(COMPUTE_NETWORK_REASON,
                             requests[0]["network"]["reason"])

            plain_routes = (
                {"route_id": "rt-plain-code", "capability": "autopilot-code", "nodes": [{"id": "execute"}]},
                {"route_id": "rt-frame", "capability": "route-frame", "nodes": [{"id": "frame"}]},
            )
            for index, partial in enumerate(plain_routes):
                route = {**self.route, **partial,
                         "route_hash": "sha256:" + str(index + 1) * 64,
                         "cwd": str(root / partial["route_id"]),
                         "artifact_root": str(root / (partial["route_id"] + "-artifacts"))}
                route_file = root / f"{route['route_id']}.json"
                route_file.write_text(json.dumps(route))
                before = len(calls)
                W._start(route, route_file, root / "jobs.log", "owner", "codex", run)
                self.assertNotIn("--execution-access-file", calls[before])

    def observe(self, **kw):
        return {"state": "ready" if self.ready else "timeout", "children": []}

    def delivery(self, jobs, aid, **kw):
        fields = dict(marker={"artifact": "/exact/report.md"}, marker_digest="sha256:marker",
            row_revision="1", row_digest="sha256:row", status="done", verdict="PASS", quiescent=True,
            owned_children=0, advanced=False, completion_proven=True)
        fields.update(self.statuses.get(aid, {}))
        return CurrentDeliveryState(**fields)

    def owner_gate(self, *args, **kw):
        if not self.released:
            raise W.DispatchContractError("human-gate-unreleased", "frame-review")

    def admit(self, command, **kwargs):
        self.calls.append(command)
        def value(flag, fallback=""):
            return command[command.index(flag) + 1] if flag in command else fallback
        aid = value("--attempt-id")
        node = value("--route-node", "owner")
        meta = {"attempt_id": aid, "parent_sid": "parent", "launch_started": "1",
            "worker_type": "owner" if node == "owner" else "frame", "route_node": node,
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "owner_route_hash": self.route["route_hash"] if node == "owner" else "",
            "parent_completion_delivery": "codex-managed-gateway"}
        with self.jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k+"="+v for k,v in meta.items()) + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def start(self, **kwargs):
        return W.start_work(self.route, self.path, self.jobs, run=self.admit, **kwargs)

    def refusal_receipt(self, *, worker_class="dispatch", retry_after_seconds=1,
                         frees_at=None, retryable="1", reason="model-worker-governor-denied",
                         child_spawned="0"):
        if frees_at is None:
            frees_at = int(time.time()) + (retry_after_seconds or 60)
        lines = [
            "check=failed", f"reason={reason}",
            f"detail=rolling model-worker start budget reached: class={worker_class} "
            f"used=20 limit=20 retry_after_seconds={retry_after_seconds}",
            f"child_spawned={child_spawned}",
        ]
        if retryable is not None:
            lines += [
                f"retryable={retryable}", "refusal=start-budget", f"worker_class={worker_class}",
                f"retry_after_seconds={retry_after_seconds}", f"frees_at={frees_at}",
            ]
        return "\n".join(lines) + "\n"

    def make_run(self, *, refuse_node, refuse_times=1, receipt=None):
        """A `run` fixture that refuses one node's launch a bounded number of
        times (typed refusal receipt, no registered row), then falls through
        to the normal admitting fixture."""
        counts = {"n": 0}

        def run(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == refuse_node and counts["n"] < refuse_times:
                counts["n"] += 1
                return subprocess.CompletedProcess(command, 75, receipt or self.refusal_receipt(), "")
            return self.admit(command, **kwargs)

        run.counts = counts
        return run

    def test_repeated_start_and_restart_keep_both_frames_then_one_owner(self):
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing", result)
            self.assertEqual(result["observation"]["state"], "pending")
            self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("--adapter" not in c for c in self.calls))
        self.ready = True
        result = self.start()
        self.assertEqual(result["state"], "needs-interview", result)
        self.assertEqual(len(self.calls), 2)
        self.released = True
        result = self.start()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["result"]["recovery_command"], "")
        self.assertEqual(result["result"]["marker"]["artifact"], "/exact/report.md")
        self.start(); self.assertEqual(len(self.calls), 3)

    def test_partial_admission_retains_the_started_child_and_exact_resume(self):
        def partial(command, **kwargs):
            if "frame-alternative" in command:
                return subprocess.CompletedProcess(command, 75, "started=0", "capacity temporarily full")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=partial)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(len(result["frame_attempts"]), 1)
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertIn("capacity temporarily full", result["launches"][1]["diagnostic"])
        self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_terminal_frame_cleanup_unavailable_reports_attention_without_relaunch(self):
        first = self.start()
        original = self.jobs.read_bytes()
        children = [{"attempt_id": aid, "status": "done", "readiness": "pending",
                     "reason": "process-unverifiable"} for aid in first["frame_attempts"]]
        W.join_selected_attempts.side_effect = None
        W.join_selected_attempts.return_value = {"state": "timeout", "children": children}
        result = self.start()
        self.assertEqual((result["state"], result["reason"]),
                         ("needs-attention", "frame-cleanup-unverifiable"))
        self.assertNotIn("parent_next", result)
        self.assertNotIn("parent_next_command", result)
        self.assertEqual(result["observation"]["children"], children)
        self.assertEqual(self.jobs.read_bytes(), original)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.path.parent / "answers.json").exists())

        children[0].update(status="open", reason="process-alive")
        live = self.start()
        self.assertEqual(live["state"], "preparing")
        self.assertEqual(live["parent_next"], "end-turn")
        expired = self.start(wait=True)
        self.assertEqual((expired["state"], expired["reason"]),
                         ("needs-attention", "parent-wait-deadline"))
        self.assertEqual(len(self.calls), 2)

    def test_typed_budget_refusal_is_waiting_capacity_not_failure(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=1))
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(result["refused_attempt_id"], W.attempt_id(self.route, "frame-alternative"))
        self.assertEqual(result["refused_node"], "frame-alternative")
        self.assertFalse(result["spawned"])
        self.assertIn("retry_at", result)
        self.assertEqual(len(result["frame_attempts"]), 1)
        self.assertEqual(result["parent_next"], "bounded-wait")
        self.assertTrue(result["parent_next_command"].endswith("--wait"))
        self.assertNotIn("failed attempt", result["next_step"])

    def test_resume_after_capacity_relaunches_refused_attempt_exactly_once(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=1,
                             receipt=self.refusal_receipt(retry_after_seconds=1))
        first = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(first["state"], "waiting-capacity", first)
        self.assertEqual(len(self.calls), 1)  # only "frame" admitted so far

        second = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(second["state"], "preparing", second)
        self.assertEqual(len(second["frame_attempts"]), 2)
        self.assertEqual(len(self.calls), 2)  # frame-alternative admitted exactly once

        W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(len(self.calls), 2)  # both rows exist; no further launch call

    def test_resume_wait_sleeps_until_retry_then_launches_once(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=1,
                             receipt=self.refusal_receipt(retry_after_seconds=7))
        sleeps = []
        result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                               sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual(sleeps, [7])
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(len(result["frame_attempts"]), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["capacity_waited_seconds"], 7)
        self.assertEqual(W.join_selected_attempts.call_args.kwargs["timeout"], 600 - 7)

    def test_a_replacement_inside_one_wait_uses_only_what_is_left_of_the_start_window(self):
        # OpenCode r4: the frame/owner died after ~590 s of the wait, was replaced at once, and the same
        # call then waited another full window -- past its caller's timeout, so nothing came back.
        now = [1_800_000_000.0]
        timeouts = []

        def join(**kwargs):
            timeouts.append(kwargs["timeout"])
            now[0] += 590
            return {"state": "timeout", "children": []}
        replaced = [True]

        def batch(jobs, attempts, run=None):
            if replaced:
                replaced.pop()
                return set(attempts) | {"att-replacement"}, [{"replacement_attempt_id": "att-replacement"}], []
            return set(attempts), [], []
        with mock.patch.object(W, "join_selected_attempts", side_effect=join), \
                mock.patch("dispatch_replacement.advance_batch", side_effect=batch):
            result = self.start(wait=True, sleep=lambda seconds: None, clock=lambda: now[0])
        self.assertEqual(timeouts, [600, 10])
        self.assertEqual(result["observation"]["state"], "timeout")
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "parent-wait-deadline"))
        self.assertEqual(result["join_waited_seconds"], 1180)
        self.assertEqual(W._wait_budget(result), 0)
        self.assertEqual(W._wait_budget({"capacity_waited_seconds": 7, "join_waited_seconds": 3}), 590)

    def test_resume_wait_refused_again_hands_back_without_another_wait(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=3))
        sleeps = []
        armed = []
        with mock.patch("capacity_auto_resume.arm", side_effect=lambda result, *a, **k: armed.append(result)):
            result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                                   sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual(sleeps, [3])  # exactly one wait, never a retry loop
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertNotIn("parent_next", result)
        self.assertNotIn("parent_next_command", result)
        self.assertEqual(result["required_action"], "report-capacity-wait")
        self.assertEqual(result["capacity_waited_seconds"], 3)
        # The bounded automatic resume, not the parent, takes the next attempt at retry_at.
        self.assertEqual([(r["reason"], r["required_action"], r["retry_at"]) for r in armed],
                         [("launch-capacity-wait", "report-capacity-wait", result["retry_at"])])
        resumed = {"record": "/x.json", "resume_at": result["retry_at"], "state": "armed", "cause": "launch-capacity"}
        with mock.patch("capacity_auto_resume.arm", return_value=resumed):
            result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                                   sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual((result["required_action"], result["parent_next"], result["auto_resume"]),
                         ("wait-for-auto-resume", "end-turn", resumed))

    def test_owner_budget_refusal_is_waiting_capacity(self):
        run = self.make_run(refuse_node="owner", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=5))
        self.ready = True
        self.released = True
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(result["refused_attempt_id"], W.attempt_id(self.route, "owner"))
        self.assertEqual(result["refused_node"], "owner")
        self.assertFalse(result["spawned"])

    def test_spawned_then_exit_75_is_never_relaunched(self):
        def crashed_after_claim(command, **kwargs):
            registered = self.admit(command, **kwargs)
            receipt = registered.stdout + self.refusal_receipt(retry_after_seconds=5, child_spawned="1")
            return subprocess.CompletedProcess(command, 75, receipt, "worker crashed after claim")
        result = W.start_work(self.route, self.path, self.jobs, run=crashed_after_claim)
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        calls_after_first = len(self.calls)
        W.start_work(self.route, self.path, self.jobs, run=crashed_after_claim)
        self.assertEqual(len(self.calls), calls_after_first)  # rows exist; no relaunch

    def test_receipt_refusal_with_existing_row_trusts_registry(self):
        def raced(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == "frame-alternative":
                self.admit(command, **kwargs)  # a concurrent resume already admitted this exact aid
                return subprocess.CompletedProcess(command, 75, self.refusal_receipt(), "")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=raced)
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(len(result["frame_attempts"]), 2)

    def test_untyped_or_kill_switch_refusal_stays_needs_attention(self):
        def kill_switch(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == "frame-alternative":
                receipt = self.refusal_receipt(retryable=None, reason="model-worker-governor-denied")
                return subprocess.CompletedProcess(command, 75, receipt, "kill switch")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=kill_switch)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "frame-launch-not-admitted")

    def test_no_harness_declaring_top_is_frame_harness_unavailable_with_no_attempt(self):
        def undeclared(command, **kwargs):
            receipt = ("status=unavailable\ntop_undeclared=claude,codex,opencode\n"
                       "check=failed\nreason=frame-harness-unavailable\nchild_spawned=0\n")
            return subprocess.CompletedProcess(command, 65, receipt, "")
        result = W.start_work(self.route, self.path, self.jobs, run=undeclared)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "frame-harness-unavailable")
        self.assertEqual(result["frame_attempts"], [])
        self.assertEqual(len(result["launches"]), 1)  # the anchor leg stopped the start; nothing else launched

    def test_a_route_that_is_not_framed_starts_its_frame_pair_without_pyyaml(self):
        # Only a framed route reads frame proposals; a recipe's own frame pair needs no PyYAML.
        with mock.patch.dict(sys.modules, {"yaml": None}):
            result = self.start()
        self.assertEqual(result["state"], "preparing", result)
        self.assertEqual(len(self.calls), 2)

    def test_automatic_frames_use_real_selector_usage_gate_before_wrapper_launch(self):
        owner = load("work_start_capacity_owner", W.ROOT / "utilities/dispatch-owner.py")
        self.route.update(cwd=self.tmp.name, capability="autopilot-code", capability_mode="debug",
                          owner_model_profile="deep")
        for node in self.route["nodes"][:2]:
            node.update(model_profile="deep", role="deep maker", unit="plan/frame")
        self.route["dispatch_evidence"]["tuples"] = [
            {"child_harness": h, "status": "supported"} for h in ("claude", "codex")]
        self.path.write_text(json.dumps(self.route))
        context = {"harnesses": ["claude", "codex"],
            "policy": {"primary": ["claude", "codex"], "relief": [], "last_resort": [],
                       "promote_relief_below": 0},
            "allocation": {"strategy": "balanced", "window": 30,
                "harness_order": ["claude", "codex", "opencode"], "usage_gate_used_percent": 85}}
        binding = SimpleNamespace(route_file=str(self.path), route_id=self.route["route_id"],
            route_hash=self.route["route_hash"], route_node="frame", registry_digest="sha256:fixture",
            write_scope="shards/frame/**", completion_gate="code-frame")
        launched = []
        stamp_harness = [True]
        def wrapper(command, **kwargs):
            launched.append(Path(command[0]).parents[1].name)
            return subprocess.CompletedProcess(command, 0)
        def select(command, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = owner.main(command[2:])
            if rc == 0:
                self.admit(command)
                if stamp_harness[0]:
                    self.jobs.write_text(self.jobs.read_text().rstrip() + ",harness=" + launched[-1] + "\n")
            return subprocess.CompletedProcess(command, rc, output.getvalue(), "")
        import artifact_producer
        for headroom in (30, 60):
            with self.subTest(headroom=headroom), contextlib.ExitStack() as stack:
                self.jobs.unlink(missing_ok=True); self.calls.clear(); launched.clear()
                stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
                stack.enter_context(mock.patch.object(owner, "_authoritative_jobs", return_value=str(self.jobs)))
                stack.enter_context(mock.patch.object(owner, "_sealed_owner_context", return_value=context))
                # This case tests selector choice, while subprocess.run is the
                # mocked wrapper boundary below; avoid running a real foreground
                # owner lifecycle inside the isolated fixture.
                stack.enter_context(mock.patch.object(owner, "select_launch_lifecycle",
                                                      return_value="detached"))
                stack.enter_context(mock.patch.object(owner, "_usage", return_value=dict.fromkeys(("claude", "codex", "opencode"), "ok")))
                stack.enter_context(mock.patch.object(owner._capacity, "capacity_report", return_value={
                    "scores": {"claude": headroom, "codex": 50, "opencode": 0}, "sources": {}}))
                stack.enter_context(mock.patch.object(owner, "derive_frame_route_binding", return_value=binding))
                stack.enter_context(mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={}))
                stack.enter_context(mock.patch.object(owner.subprocess, "run", side_effect=wrapper))
                if headroom == 30:
                    stamp_harness[0] = False
                    pending = W.start_work(self.route, self.path, self.jobs, run=select)
                    self.assertEqual(pending["state"], "preparing", pending)
                    self.assertEqual(pending["reason"], "frame-first-attempt-pending")
                    self.assertEqual(launched, ["codex"])
                    self.assertEqual(len(self.calls), 1)
                    self.jobs.write_text(self.jobs.read_text().rstrip() + ",harness=codex\n")
                    stamp_harness[0] = True
                result = W.start_work(self.route, self.path, self.jobs, run=select)
                self.assertEqual(result["state"], "preparing", result)
                self.assertEqual(launched, ["codex", "claude"] if headroom < 50 else ["claude", "codex"])
                first_launch = pending["launches"][0] if headroom == 30 else result["launches"][0]
                self.assertIn("selection_source=configured-balanced", first_launch["receipt"])
                self.assertIn("selection_source=configured-balanced", result["launches"][-1]["receipt"])
                # No authorized capacity is a refusal before any model wrapper.
                self.jobs.unlink(); self.calls.clear(); launched.clear()
                with mock.patch.object(owner, "_usage", return_value=dict.fromkeys(("claude", "codex", "opencode"), "limited(reset)")):
                    refused = W.start_work(self.route, self.path, self.jobs, run=select)
                self.assertEqual(refused["state"], "needs-attention", refused)
                self.assertEqual(launched, [])
                self.assertFalse(self.jobs.exists())

    def test_public_answer_submission_releases_then_starts_only_one_owner(self):
        self.start(); self.ready = True
        def release(*args, **kwargs):
            self.assertEqual(kwargs["answers"], "actual-answers.json")
            self.released = True
            return {"state": "released", "decision": "proceed"}
        with mock.patch.object(W, "frame_interview_step", side_effect=release):
            result = self.start(interview="question.json", answers="actual-answers.json")
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)

    def test_pending_conversation_receipt_returns_without_starting_an_owner(self):
        self.start(); self.ready = True
        pending = {"state": "needs-question", "required_action": "wait-for-user-answer",
                   "human_wait": {"state": "pending", "fallback": "ordinary-conversation",
                                  "question_block": "이 순서로 진행할까요?\n- 예\n- 아니요"},
                   "parent_next": "end-turn"}
        with mock.patch.object(W, "frame_interview_step", return_value=pending):
            result = self.start(answers="expired-empty-response.json")
        self.assertEqual(result["state"], "needs-question")
        self.assertEqual(result["required_action"], "wait-for-user-answer")
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(result["human_wait"]["question_block"], "이 순서로 진행할까요?\n- 예\n- 아니요")
        self.assertFalse(result["owner_started"])
        self.assertEqual(len(self.calls), 2)  # only the existing frame pair

    def test_question_failure_after_frame_completion_does_not_promise_another_wake(self):
        self.start(); self.ready = True
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t"))
        with mock.patch.object(W, "frame_interview_step", side_effect=ValueError("bad-question")):
            result = self.start(interview="question.json")
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertNotIn("parent_next", result)
        self.assertEqual(len(self.calls), 2)

    def test_exited_owner_with_missing_work_reports_without_waiting_or_replacement(self):
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner", "workflow_completion=runtime-v1,failure_class=pass,worker_type=owner"))
        with mock.patch.object(T, "owner_workflow_gaps", return_value={"report":"completion-marker-absent"}), \
             mock.patch.object(W, "join_selected_attempts", side_effect=AssertionError("cannot wait for an absent executor")):
            result = self.start(wait=True)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "workflow-executor-exited")
        self.assertNotIn("parent_next", result)
        self.assertEqual(len(self.calls), 3)

    def test_completed_deferred_owner_still_reports_workflow_gaps(self):
        """C15 (S3a): `verdict_pass` recognizes a marker-bound deferred owner
        row (failure_class stays infrastructure) the same way a plain
        failure_class=pass row already does -- the gap check must still run,
        not be skipped because the literal failure_class isn't "pass"."""
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner",
            "workflow_completion=runtime-v1,failure_class=infrastructure,"
            "classifier_source=registered-wrapper-completion-transient-v1,"
            "completion_marker=/artifacts/.runtime/completions/one-shot.json,"
            "note=completed-marker,worker_type=owner"))
        with mock.patch.object(T, "owner_workflow_gaps", return_value={"report": "completion-marker-absent"}), \
             mock.patch.object(W, "join_selected_attempts", side_effect=AssertionError("cannot wait for an absent executor")):
            result = self.start(wait=True)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "workflow-executor-exited")

    def test_failure_conflict_or_unsealed_workflow_never_authorizes_success(self):
        self.start(); self.ready = True
        for fields in ({"verdict":"FAIL"}, {"terminal_conflict":True}, {"workflow_complete":False}):
            with self.subTest(fields=fields):
                self.statuses[W.attempt_id(self.route,"frame")] = fields
                result = self.start()
                self.assertEqual(result["state"], "needs-attention", result)
                self.assertEqual(len(self.calls), 2)
                outcome = next(r for r in result["frame_results"] if r["classification"] == "attention")
                command = outcome["recovery_command"]
                self.assertIn(W.attempt_id(self.route,"frame"), command)
                self.assertNotIn("retry", command)

    def test_exited_owner_with_pending_closure_never_promises_running_or_a_new_turn(self):
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner", "workflow_completion=runtime-v1,failure_class=pass,worker_type=owner"))
        self.ready = False
        self.statuses[W.attempt_id(self.route,"owner")] = {"workflow_complete":False}
        with mock.patch.object(T,"owner_workflow_gaps",return_value={}):
            for wait in (False,True):
                result=self.start(wait=wait)
                self.assertEqual(result["state"],"needs-attention",result)
                self.assertEqual(result["reason"],"owner-settlement-pending")
                self.assertIn(" finish ",result["result"]["recovery_command"])
                self.assertNotIn("parent_next",result)
                self.assertEqual(len(self.calls),3)

    def test_running_owner_receipt_states_the_parent_role(self):
        self.start(); self.ready = self.released = True; self.start()
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}):
            result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertIn("parent_next", result)

    def test_open_but_exited_or_unobservable_owner_is_never_reported_running(self):
        import dispatch_contract as D
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace(
            'worker_type=owner', 'worker_type=owner,pid=99999999,pid_start=100'))
        for state, reason in (('quiescent', 'owner-settlement-pending'),
                              ('unverifiable', 'owner-process-unverifiable')):
            with self.subTest(state=state), \
                 mock.patch.object(W, 'join_selected_attempts', return_value={'state': 'timeout', 'children': []}), \
                 mock.patch.object(D, 'attempt_process_quiescence',
                                   return_value=D.ProcessQuiescence(state, 'fixture')):
                result = self.start()
                self.assertEqual((result['state'], result['reason']), ('needs-attention', reason), result)
                self.assertNotIn('parent_next', result)
        self.assertEqual(len(self.calls), 3)

    def test_start_with_a_changed_pin_diagnoses_a_live_owner_on_the_old_harness(self):
        self.start(); self.ready = self.released = True; created = self.start()
        owner = created['owner_attempt_id']
        self.jobs.write_text(self.jobs.read_text().replace('attempt_id='+owner,
            'harness=codex,launch_claimed=1,replacement_original_attempt_id=att-predecessor,attempt_id='+owner))
        self.route['selection_pins'] = {'owner': {'harness': 'opencode'}}
        before = self.jobs.read_bytes()
        result = self.start()
        self.assertEqual((result['state'], result['reason'], result['requested_harness']),
                         ('needs-attention', 'owner-pin-handoff', 'opencode'))
        command = __import__('shlex').split(result['recovery_command'])
        self.assertIn('close', command)
        self.assertNotIn('--stop-resources', command)
        self.assertEqual(result['owner_attempt_id'], owner)
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertEqual(len(self.calls), 3)

    def test_an_ordinary_live_owner_keeps_its_selector_fallback(self):
        self.start(); self.ready = self.released = True; created = self.start()
        owner = created['owner_attempt_id']
        self.jobs.write_text(self.jobs.read_text().replace('attempt_id='+owner, 'harness=codex,launch_claimed=1,attempt_id='+owner))
        self.route['selection_pins'] = {'owner': {'harness': 'opencode'}}
        before = self.jobs.read_bytes()
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}):
            result = self.start()
        self.assertEqual(result['state'], 'running', result)
        self.assertNotEqual(result.get('reason'), 'pin-ignored-for-replacement')
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertEqual(len(self.calls), 3)

    def test_changed_owner_pin_is_observed_before_the_resource_watch_receipt(self):
        import dispatch_resource_wait as RESOURCE
        self.start(); self.ready = self.released = True; created = self.start()
        owner = created['owner_attempt_id']
        self.jobs.write_text(self.jobs.read_text().replace('attempt_id='+owner,
            'harness=codex,launch_claimed=1,attempt_id='+owner))
        self.route['nodes'].append({'id': 'eval-run', 'kind': 'resource-runner'})
        self.route['artifact_root'] = self.tmp.name
        W.route_authority.record_pin_change(self.route, target='owner',
            pin={'harness': 'opencode', 'model': None, 'effort': None},
            by={'harness': 'codex', 'session_id': 'parent'},
            source='fixture', tuples=[], candidates=[])
        before = self.jobs.read_bytes()
        watcher = SimpleNamespace(recover_resource_watches=mock.Mock(return_value=[{'state': 'watching'}]))
        with mock.patch.object(RESOURCE, 'supervisor', return_value=watcher):
            result = self.start()
        self.assertEqual((result['state'], result['reason'], result['requested_harness']),
                         ('needs-attention', 'owner-pin-handoff', 'opencode'))
        self.assertEqual(result['owner_attempt_id'], owner)
        self.assertIn(' close ', result['recovery_command'])
        self.assertEqual(self.jobs.read_bytes(), before)
        watcher.recover_resource_watches.assert_not_called()
        self.assertEqual(len(self.calls), 3)

    def test_completed_owner_winning_close_race_has_no_handoff_retry(self):
        completed = {'state': 'completed', 'terminal_gate_proven': True,
                     'summary': W.PIN_HANDOFF_SUMMARY}
        result = W.pin_handoff_continuation(self.route, self.path, self.jobs, completed)
        self.assertIs(result, completed)
        self.assertNotIn('recovery_command', result)

    def test_owner_exits_during_join_before_the_public_receipt(self):
        self.start(); self.ready = self.released = True; self.start()
        def close_during_join(**kwargs):
            self.jobs.write_text(self.jobs.read_text().replace("\topen\t","\tdone\t"))
            return {"state":"timeout","children":[]}
        self.statuses[W.attempt_id(self.route,"owner")] = {"workflow_complete":False}
        with mock.patch.object(W,"join_selected_attempts",side_effect=close_during_join):
            result=self.start()
            self.assertEqual(result["state"],"needs-attention",result)
            self.assertNotIn("parent_next",result)
            self.statuses[W.attempt_id(self.route,"owner")] = {}
            self.assertEqual(self.start()["state"],"completed")
        self.assertEqual(len(self.calls),3)

    def test_frame_replacement_keeps_successful_sibling_and_gate_release(self):
        self.start(); self.ready=True; self.released=True
        source=W.attempt_id(self.route,'frame');success=W.attempt_id(self.route,'frame-alternative')
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t','\tdone\t'))
        calls=[]
        def advance(jobs,attempts,**kwargs):
            calls.append(set(attempts))
            if source not in attempts:return set(attempts),[],[]
            row=next(line for line in jobs.read_text().splitlines() if 'attempt_id='+source+',' in line)
            with jobs.open('a') as f:f.write(row.replace(source,'att-frame-replacement')+'\n')
            return {success,'att-frame-replacement'},[{'original_attempt_id':source,'replacement_attempt_id':'att-frame-replacement'}],[]
        with mock.patch('dispatch_replacement.advance_batch',side_effect=advance):
            result=self.start()
        self.assertIn('att-frame-replacement',result['frame_attempts'])
        self.assertIn(success,result['frame_attempts'])
        self.assertEqual(len(self.calls),3)  # initial two frames, then the owner
        self.assertEqual(len(calls),2)
        self.assertNotIn('frame_interview',result)

    def test_owner_registered_replacement_is_resumed_without_new_owner_launch(self):
        self.start();self.ready=True;self.released=True;self.start()
        owner=W.attempt_id(self.route,'owner')
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        self.jobs.write_text(self.jobs.read_text().replace(row,row.replace('\topen\t','\tdone\t')+',note=dead-exact-pid'))
        with self.jobs.open('a') as f:f.write(row.replace(owner,'att-owner-replacement')+',replacement_original_attempt_id='+owner+',launch_claimed=0\n')
        self.ready=False;observed=[]
        def resume(jobs,aid,**kwargs):
            observed.append(aid);return {'state':'not-applicable'}
        with mock.patch('dispatch_replacement.advance',side_effect=resume):result=self.start()
        self.assertEqual(observed,['att-owner-replacement'])
        self.assertEqual(len(self.calls),3)
        self.assertEqual(result['owner_attempt_id'],'att-owner-replacement')

    def _wait_attention(self,aid):
        return {'state':'needs-attention','reason':'replacement-capacity-wait','source_attempt_id':aid,
                'node':'__owner__','harness':'claude','retry_at':'2099-01-01T00:00:00Z','usage_state':'limited(x)'}

    def test_owner_at_a_usage_limit_is_waiting_capacity_and_only_start_may_resume_it(self):
        self.start();self.ready=self.released=True;self.start()
        owner=W.attempt_id(self.route,'owner')
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        self.jobs.write_text(self.jobs.read_text().replace(
            row,row.replace('\topen\t','\tdone\t')+',note=dead-capacity,failure_class=capacity'))
        launches=len(self.calls);seen={}
        def wait(jobs,aid,**kwargs):
            seen.update(kwargs);return self._wait_attention(aid)
        with mock.patch('dispatch_replacement.advance',side_effect=wait):
            result=self.start()
        self.assertTrue(seen['resume_capacity'])          # start is the one explicit resume
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('waiting-capacity','owner-capacity-wait','resume-after-capacity'))
        self.assertEqual((result['retry_at'],result['harness'],result['source_attempt_id']),
                         ('2099-01-01T00:00:00Z','claude',owner))
        self.assertNotIn('parent_next',result);self.assertNotIn('parent_next_command',result)
        self.assertIn('from the session that owns the route',result['next_step'])
        self.assertEqual(len(self.calls),launches)

    def test_a_soft_allocation_gate_says_exactly_when_it_lifts(self):
        gate={'state':'needs-attention','reason':'replacement-capacity-wait','source_attempt_id':'att-owner',
              'node':'__owner__','harness':'codex','usage_state':'allocation-usage-gate',
              'headroom':10.0,'usage_gate_used_percent':85,'capacity_source':'live'}
        unknown=W._capacity_pause({},gate,'resume')
        self.assertEqual((unknown['state'],unknown['reason']),('waiting-capacity','owner-capacity-wait'))
        self.assertNotIn('retry_at',unknown)                  # no reset time is invented
        self.assertIn('not at a usage limit',unknown['next_step'])
        self.assertIn('after codex usage drops below the 85% gate',unknown['next_step'])
        known=W._capacity_pause({},{**gate,'retry_at':'2026-10-09T22:58:00Z'},'resume')
        self.assertEqual(known['retry_at'],'2026-10-09T22:58:00Z')
        self.assertIn('After retry_at, when the last usage window at the gate resets',known['next_step'])
        self.assertIn('from the session that owns the route',known['next_step'])

    def test_frame_replacement_held_by_a_usage_limit_waits_instead_of_failing(self):
        self.start();self.ready=True
        frame=W.attempt_id(self.route,'frame')
        seen={}
        def held(jobs,attempts,**kwargs):
            seen.update(kwargs);return set(attempts),[],[self._wait_attention(frame)]
        with mock.patch('dispatch_replacement.advance_batch',side_effect=held):
            result=self.start()
        self.assertNotIn('resume_capacity',seen)          # a supervisor-shaped call never resumes
        self.assertEqual((result['state'],result['reason']),('waiting-capacity','owner-capacity-wait'))
        self.assertEqual(result['retry_at'],'2099-01-01T00:00:00Z')

    # -- an owner its launcher closed before spawning: nothing ran, so say "start again later" ----
    def _never_started_meta(self, aid):
        return {"attempt_id": aid, "parent_sid": "parent", "worker_type": "owner", "dispatch_depth": "1",
                "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                "owner_route_hash": self.route["route_hash"], "owner_route_file": str(self.path),
                "launch_claimed": "0", "launch_outcome": "never-launched",
                "note": "dead-producer-binding-failed", "failure_class": "contract"}

    def _unstarted_run(self, reason="admission-busy"):
        """A launcher that registers the owner, then closes it before spawning (exit 73)."""
        def run(command, **kwargs):
            self.calls.append(command)
            meta = self._never_started_meta(command[command.index("--attempt-id") + 1])
            with self.jobs.open("a") as stream:
                stream.write("now\tdone\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
            return subprocess.CompletedProcess(
                command, 73, f"check=failed\nreason={reason}\ndetail=admission lock busy\nchild_spawned=0\n", "")
        return run

    def _auto_resume(self):
        """Record what start hands the automatic resume, without starting a detached process."""
        self.armed = []

        def arm(result, route_file, jobs, **kwargs):
            self.armed.append(result["reason"])
            return {"record": "/x.json", "resume_at": "2026-10-07T12:01:00Z", "state": "armed",
                    "cause": "launch-not-started"}
        return mock.patch("capacity_auto_resume.arm", side_effect=arm)

    def test_owner_that_did_not_start_is_started_again_by_the_runtime_not_harvested(self):
        self.route["nodes"] = []
        with self._auto_resume():
            result = W.start_work(self.route, self.path, self.jobs, run=self._unstarted_run())
        self.assertEqual((result["state"], result["reason"], result["required_action"]),
                         ("needs-attention", "owner-launch-not-started", "wait-for-auto-resume"), result)
        self.assertEqual(self.armed, ["owner-launch-not-started"])
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(result["launch_reason"], "admission-busy")
        self.assertEqual(result["owner_attempt_id"], W.attempt_id(self.route, "owner"))
        self.assertIn("capability-route.py", result["recovery_command"])
        self.assertIn("start", result["recovery_command"])
        self.assertNotIn("harvest", result["recovery_command"])
        self.assertIn("did not start and nothing ran", result["next_step"])
        self.assertEqual(len(self.calls), 1)   # one launch per start, however it ended

    def test_owner_preparation_busy_with_no_row_is_started_again_by_the_runtime(self):
        """dispatch-owner failed before any row existed: the typed admission-busy error."""
        self.route["nodes"] = []
        calls = self.calls

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, 65, "check=failed\nreason=admission-busy:admission lock held past 120s; "
                "nothing started; run start again later\nchild_spawned=0\n", "")
        with self._auto_resume():
            result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual((result["state"], result["reason"], result["required_action"]),
                         ("needs-attention", "owner-launch-not-admitted", "wait-for-auto-resume"), result)
        self.assertEqual((self.armed, result["parent_next"]), (["owner-launch-not-admitted"], "end-turn"))
        self.assertEqual(result["launch_reason"], "admission-busy")
        self.assertEqual(result["recovery_command"], result["resume_command"])
        self.assertIn("capability-route.py", result["recovery_command"])
        self.assertNotIn("harvest", result["recovery_command"])
        self.assertIn("nothing ran", result["next_step"])
        self.assertEqual(len(calls), 1)
        # no row exists, so the next start (the automatic one) launches the same owner again
        with self._auto_resume():
            again = W.start_work(self.route, self.path, self.jobs, run=self._unstarted_run())
        self.assertEqual(again["reason"], "owner-launch-not-started", again)
        self.assertEqual(len(calls), 2)

    def test_owner_launch_refused_for_another_reason_keeps_needs_inspection(self):
        self.route["nodes"] = []
        result = W.start_work(self.route, self.path, self.jobs, run=lambda command, **kwargs:
                              subprocess.CompletedProcess(command, 65, "check=failed\nreason=x\n", ""))
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "owner-launch-not-admitted"))
        self.assertNotEqual(result.get("required_action"), "resume-later")

    def test_outcome_of_a_never_started_owner_points_at_start(self):
        aid = W.attempt_id(self.route, "owner")
        meta = self._never_started_meta(aid)
        self.jobs.write_text("now\tdone\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        command = W._outcome(self.jobs, aid)["recovery_command"]
        self.assertIn("capability-route.py", command)
        self.assertIn(str(self.path), command)
        self.assertNotIn("harvest", command)

    def test_next_start_relaunches_an_owner_that_never_started(self):
        """The row shape of the stuck BC route: closed by its launcher, no log, sealed launch input."""
        import dispatch_replacement as R
        from dispatch_contract import parse_registry_metadata
        self.route["nodes"] = []
        self.route.update(artifact_root=self.tmp.name, cwd=self.tmp.name, capability="autopilot-code")
        self.path.write_text(json.dumps(self.route))
        aid = W.attempt_id(self.route, "owner")
        args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs, worktree=self.tmp.name,
                               route_id=self.route["route_id"], route_node="",
                               replacement_input_argv=["--start", "--attempt-id", aid, "--prompt-text", "the raw task"])
        meta = {**self._never_started_meta(aid), "attempt_schema_version": "2", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "harness": "codex",
                "log_file": str(Path(self.tmp.name) / "never-written.jsonl")}
        meta.update(parse_registry_metadata(R.seal_launch_input(args, "codex", "the raw task")))
        self.jobs.write_text("now\tdone\t" + self.tmp.name + "\t" + self.tmp.name + "\ttask\t"
                             + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        launched = []

        def run(command, **kwargs):
            launched.append(command)
            replacement = command[command.index("--attempt-id") + 1]
            record = R.claim(self.jobs, aid)   # the one claim `advance` already made, replayed
            source = R._rows(self.jobs.read_text().splitlines())[aid][1]
            replay = R.launch_input(self.jobs, aid, source)
            sealed = SimpleNamespace(**vars(args))
            sealed.attempt_id = replacement
            sealed.replacement_input_argv = R._replacement_argv(record, source, replay)
            row = {k: v for k, v in meta.items()
                   if k not in ("note", "failure_class", "launch_outcome", "replacement_input_digest")}
            row.update(attempt_id=replacement, automatic_retry_of=aid, launch_claimed="1", launch_started="1",
                       replacement_family_id=record["family_id"], replacement_original_attempt_id=aid,
                       replacement_ordinal="1", replacement_claim_digest=R._digest(record))
            row.update(parse_registry_metadata(R.seal_launch_input(sealed, "codex", "the raw task")))
            with self.jobs.open("a") as stream:
                stream.write("now\topen\t" + self.tmp.name + "\t" + self.tmp.name + "\ttask\t"
                             + ",".join(k + "=" + v for k, v in row.items()) + "\n")
            return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

        with mock.patch("dispatch_replacement._authorized"), \
             mock.patch("dispatch_replacement._reuse_snapshot", return_value={
                 "completed": [], "cycle_id": "cyc-test", "producer_id": "prod-test", "gate_releases": []}), \
             mock.patch("dispatch_replacement._logical_key",
                        side_effect=lambda r, m: {"root_route_id": "rt-root", "node": "__owner__"}), \
             mock.patch("dispatch_replacement._route", return_value=(self.path, self.route)), \
             mock.patch("dispatch_replacement._terminal_absent", side_effect=AssertionError("no log to read")), \
             mock.patch("dispatch_contract.attempt_process_quiescence",
                        return_value=SimpleNamespace(state="quiescent", reason="process-absent")), \
             mock.patch("dispatch_capacity_evidence.harness_hold", return_value=None), \
             mock.patch("dispatch_replacement_batch.command", return_value=None):
            result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(len(launched), 1, result)
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.calls, [])   # the fresh-owner launcher was not used: a replacement was
        self.assertEqual([edge["original_attempt_id"] for edge in result["replacement_lineage"]], [aid])

    def _parked_owner_row(self):
        self.start();self.ready=self.released=True;self.start()
        owner=W.attempt_id(self.route,'owner')
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        self.jobs.write_text(self.jobs.read_text().replace(
            row,row.replace('\topen\t','\tdone\t')+',note=dead-worker-blocked,failure_class=blocked'))
        self.ready=False
        return owner

    def _parked(self,status):
        return {'gate':'full-run-authorization','status':status,'epoch':1,'raised_at':'2026-09-29T01:00:00Z',
                'artifact':'/tmp/gate.md','route_file':str(self.path),'route_id':'rt-probe',
                'route_hash':'sha256:probe','gated_nodes':['full-run']}

    def test_parked_owner_waits_for_the_person_without_failure_or_wait(self):
        self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('blocked')), \
             mock.patch('dispatch_replacement.advance',side_effect=AssertionError('a blocked gate never continues')):
            result=self.start()
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('waiting-human-gate','owner-parked-at-human-gate','answer-human-gate'))
        self.assertEqual((result['gate'],result['gate_artifact']),('full-run-authorization','/tmp/gate.md'))
        for token in ('--gate full-run-authorization','--decision proceed','--jobs '+str(self.jobs),'workflow-supervisor.py'):
            self.assertIn(token,result['release_command'])
        self.assertNotIn('parent_next',result)
        self.assertIn('resume_command',result)
        import frame_interview as FI
        self.assertIn(FI.PENDING_ANSWER_RULE,result['next_step'])
        self.assertEqual(len(self.calls),3)

    def test_a_parked_receipt_points_at_the_owners_own_report_when_it_has_one(self):
        import base64
        self._parked_owner_row()
        report=self.path.parent/'owner-report.md'
        report.write_text('what the owner says it changed\n')
        encoded=base64.urlsafe_b64encode(str(report).encode()).decode().rstrip('=')
        readable={'state':'valid','verdict':'PASS','artifact_state':'readable','artifact_path_b64':encoded}
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('blocked')), \
             mock.patch('codex_dispatch_terminal.inspect_terminal_attempt',return_value=readable):
            result=self.start()
        self.assertEqual(result['state'],'waiting-human-gate')
        self.assertEqual(result['owner_report'],str(report))
        self.assertIn('owner_report',result['next_step'])
        unreadable={'state':'invalid','verdict':'-','artifact_state':'missing','artifact_path_b64':''}
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('blocked')), \
             mock.patch('codex_dispatch_terminal.inspect_terminal_attempt',return_value=unreadable):
            result=self.start()
        self.assertEqual(result['state'],'waiting-human-gate')
        self.assertNotIn('owner_report',result)
        self.assertNotIn('owner_report',result['next_step'])

    def test_parent_closure_after_parking_returns_settlement_without_launch(self):
        owner = self._parked_owner_row()
        self.jobs.write_text(self.jobs.read_text().replace('attempt_id=' + owner + ',',
                             'attempt_id=' + owner + ',parent_close_requested=1,'))
        previous_calls = list(self.calls)
        with mock.patch('dispatch_replacement.owner_parked_gate', return_value=self._parked('blocked')):
            result = W._owner_parked_response({}, self.jobs, owner)
        self.assertEqual((result['state'], result['reason'], result['next_action']),
                         ('protected', 'cancelled-by-parent', 'settle'))
        self.assertNotIn('release_command', result)
        self.assertEqual(self.calls, previous_calls)

    def test_stopped_gate_reports_without_replacement(self):
        self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('stop')), \
             mock.patch('dispatch_replacement.advance',side_effect=AssertionError('a stop never continues')):
            result=self.start()
        self.assertEqual((result['state'],result['reason']),('stopped','human-gate-stop'))
        self.assertEqual(len(self.calls),3)

    def test_revised_gate_with_parked_owner_needs_attention_without_continuation(self):
        self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('revise')), \
             mock.patch('dispatch_replacement.advance',side_effect=AssertionError('a revise never continues')):
            result=self.start()
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('needs-attention','human-gate-revise-owner-parked','report-gate-revision'))
        self.assertEqual(len(self.calls),3)

    def test_an_owner_blocked_outside_any_gate_points_at_its_answer_not_a_new_route(self):
        owner=self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=None):
            result=self.start()
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('needs-attention','owner-blocked','answer-blocked-owner'),result)
        self.assertIn('--attempt-id '+owner,result['correction_command'])
        for token in ('correction_command','--message-file','replacement owner','Do not close or recompose'):
            self.assertIn(token,result['next_step'])
        self.assertEqual(len(self.calls),3)   # nothing new launched while it waits

    def test_format_only_owner_end_waits_for_correction_without_launching(self):
        owner = self._parked_owner_row()
        original = next(line for line in self.jobs.read_text().splitlines()
                        if 'attempt_id='+owner+',' in line)
        additions = (",note=dead-contract,failure_class=contract"
                     ",classifier_source=supervisor-terminal-v1,process_exit=0"
                     ",reconcile_reason=final-handoff-invalid,terminal_event=claude-result"
                     ",log_file=" + str(Path(self.tmp.name) / "owner.log"))
        corrected = original.replace(",note=dead-worker-blocked,failure_class=blocked", "") + additions
        self.jobs.write_text(self.jobs.read_text().replace(original, corrected))
        stable = self.jobs.read_bytes()
        with mock.patch('dispatch_replacement.owner_parked_gate', return_value=None):
            results = [self.start() for _ in range(3)]
        self.assertEqual([(r['state'], r['reason'], r['required_action']) for r in results],
                         [('needs-attention', 'owner-blocked', 'answer-blocked-owner')] * 3)
        self.assertIn('correction_command', results[0])
        self.assertIn('--message-file', results[0]['next_step'])
        self.assertIn('handoff-format error', results[0]['next_step'])
        self.assertIn('owner_log_file ' + str(Path(self.tmp.name) / 'owner.log'), results[0]['next_step'])
        self.assertEqual(results[0]['owner_log_file'], str(Path(self.tmp.name) / 'owner.log'))
        self.assertNotIn('owner_report says', results[0]['next_step'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.jobs.read_bytes(), stable)

    def test_a_readable_fail_after_spent_replacement_points_to_the_working_correction(self):
        owner = self._parked_owner_row()
        self.jobs.write_text(self.jobs.read_text().replace('dead-worker-blocked', 'dead-worker-fail')
                             .replace('failure_class=blocked', 'failure_class=fail'))
        with mock.patch('dispatch_replacement.advance', return_value={
                'state': 'needs-attention', 'reason': 'automatic-replacement-exhausted'}):
            result = self.start()
        self.assertEqual((result['state'], result['reason'], result['required_action']),
                         ('needs-attention', 'owner-failed', 'answer-failed-owner'), result)
        self.assertIn('--attempt-id ' + owner, result['correction_command'])
        self.assertIn('correction_command', result['next_step'])
        self.assertIn('replacement owner', result['next_step'])
        self.assertEqual(len(self.calls), 3)

    def test_released_parked_owner_continues_through_replacement(self):
        owner=self._parked_owner_row()
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        def replace(jobs,aid,**kwargs):
            if aid!=owner:return {'state':'not-applicable'}
            with self.jobs.open('a') as f:
                f.write(row.replace(owner,'att-owner-continuation').replace('\tdone\t','\topen\t')
                        .replace(',note=dead-worker-blocked,failure_class=blocked','')
                        +',replacement_original_attempt_id='+owner+',launch_claimed=1\n')
            return {'state':'running','record':{'route_file':str(self.path)}}
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('proceed')), \
             mock.patch('dispatch_replacement.advance',side_effect=replace), \
             mock.patch('dispatch_replacement.effective_attempts',return_value=({'att-owner-continuation'},[])):
            result=self.start()
        self.assertEqual(result['state'],'running',result)
        self.assertEqual(result['owner_attempt_id'],'att-owner-continuation')

    def test_unknown_process_retains_runtime_wait_without_replacement(self):
        self.start()
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_bounded_wait_deadline_hands_back_without_another_wait_or_retry(self):
        for owner in (False, True):
            with self.subTest(owner=owner):
                self.jobs.unlink(missing_ok=True); self.calls.clear()
                if owner:
                    self.route["nodes"] = []
                result = self.start(wait=True)
                self.assertEqual(result["state"],"needs-attention",result)
                self.assertEqual(result["reason"],"parent-wait-deadline")
                self.assertEqual(result["required_action"],"report-pending-work")
                self.assertNotIn("parent_next_command",result)
                self.assertIn("runtime watchers retain",result["next_step"])
                before = len(self.calls)
                self.start()
                self.assertEqual(len(self.calls),before)

    def test_foreign_parent_is_checked_before_any_new_sibling(self):
        self.admit(["fixture", "--route-node", "frame-alternative", "--attempt-id", W.attempt_id(self.route,"frame-alternative")])
        self.jobs.write_text(self.jobs.read_text().replace("parent_sid=parent", "parent_sid=other"))
        self.calls.clear()
        result = self.start()
        self.assertEqual(result["reason"], "work-parent-recovery-required", result)
        self.assertEqual(self.calls, [])

    def test_legacy_closure_cannot_hide_runtime_owner_pending_settlement(self):
        import dispatch_terminal_commit as terminal
        self.path.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"], "terminal_gate_proven": True}))
        with mock.patch.object(W, "_rows", return_value={"att-owner": ("done", {
            "workflow_completion": "runtime-v1", "owner_route_id": self.route["route_id"]})}), \
                mock.patch.object(terminal, "owner_completion_state", return_value=terminal.CompletionState("pending")):
            self.assertEqual(self.start()["reason"], "workflow-completion-pending")
        self.assertEqual(self.calls, [])

    def test_closed_completion_reports_parent_delivery_as_information(self):
        import dispatch_terminal_commit as terminal
        self.path.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "terminal_gate_proven": True, "terminal_owner_attempt_id": "att-owner"}))
        meta = {"workflow_completion": "runtime-v1", "owner_route_id": self.route["route_id"],
                "parent_completion_delivery": "poll-fallback",
                "parent_completion_reason": "parent-identity-unmatched"}
        with mock.patch.object(W, "_rows", return_value={"att-owner": ("done", meta)}), \
                mock.patch.object(terminal, "owner_completion_state", return_value=terminal.CompletionState("complete")):
            result = self.start()
        self.assertEqual((result["state"], result["required_action"]), ("completed", "advance-completed"))
        self.assertEqual(result["completion_delivery"], [{"attempt_id": "att-owner", "carrier": "poll-fallback",
                                                        "reason": "parent-identity-unmatched"}])
        self.assertEqual(self.calls, [])

    def test_a_replaced_owner_with_nothing_to_settle_does_not_hold_a_closed_route(self):
        # The first owner stopped at a gate and was replaced; its BLOCKED row is `not-applicable`,
        # and the replacement settled the route (Claude r4 leg1, defect 2).
        import dispatch_terminal_commit as terminal
        self.path.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"], "terminal_gate_proven": True,
            "terminal_owner_attempt_id": "att-replacement"}))
        rows = {aid: ("done", {"workflow_completion": "runtime-v1", "owner_route_id": self.route["route_id"]})
                for aid in ("att-first", "att-replacement")}
        states = {"att-first": "not-applicable", "att-replacement": "complete"}
        with mock.patch.object(W, "_rows", return_value=rows), \
                mock.patch.object(terminal, "owner_completion_state",
                                  side_effect=lambda jobs, status, meta: terminal.CompletionState(states[meta["aid"]])):
            for aid, (_status, meta) in rows.items():
                meta["aid"] = aid
            result = self.start()
        self.assertEqual((result["state"], result["required_action"]), ("completed", "advance-completed"), result)
        self.assertEqual(self.calls, [])

    def test_successor_session_harvests_a_finished_route_but_not_a_live_one(self):
        # A supervisor hands the route to another session: finished attempts
        # carry only a result, so the successor reads it instead of being told
        # to recover a parent it can never become.
        self.start(); self.ready = self.released = True; self.start()
        self.assertEqual(len(self.calls), 3)
        W.default_parent_session_id.return_value = "successor"
        live = self.start()
        self.assertEqual(live["reason"], "work-parent-recovery-required", live)
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t"))
        result = self.start()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)

    def test_same_seat_successor_resumes_a_live_attempt_without_a_new_launch(self):
        # After a /clear the registered parent_sid stays the old session; the seat handover
        # lets the new session at the same pane reuse the live attempt instead of being refused.
        self.start()
        before = len(self.calls)
        W.default_parent_session_id.return_value = "successor"
        with mock.patch("dispatch_seat_handover.owns", side_effect=lambda meta, session, jobs=None: session == "successor") as owns:
            result = self.start()
        self.assertEqual(result["state"], "preparing", result)
        self.assertEqual(len(self.calls), before)
        owns.assert_called()
        self.assertIn("parent_sid=parent", self.jobs.read_text())          # the registered identity is untouched
        W.default_parent_session_id.return_value = "parent"
        with mock.patch("dispatch_seat_handover.owns", side_effect=lambda meta, session, jobs=None: session == "successor"):
            self.assertEqual(self.start()["reason"], "work-parent-recovery-required")
        W.default_parent_session_id.return_value = "successor"
        with mock.patch("dispatch_seat_handover.owns", side_effect=lambda meta, session, jobs=None: session == "successor"):
            self.assertEqual(self.start()["state"], "preparing")
        W.default_parent_session_id.return_value = "stranger"
        with mock.patch("dispatch_seat_handover.owns", side_effect=lambda meta, session, jobs=None: session == "successor"):
            self.assertEqual(self.start()["reason"], "work-parent-recovery-required")

    def test_resume_keeps_native_parent_when_gateway_transport_has_advanced(self):
        self.start()
        with mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "1", "AGENT_DISPATCH_CHILD": "0"}), \
             mock.patch.object(W, "interactive_parent_identity", return_value=("codex", "parent")), \
             mock.patch.object(W, "probe_managed_codex_parent", return_value=SimpleNamespace(thread_id="gateway-successor")) as probe:
            self.assertEqual(self.start()["state"], "preparing")
            self.assertEqual(len(self.calls), 2)
            probe.assert_not_called()
            probe.return_value = SimpleNamespace(thread_id="sibling")
            self.assertEqual(self.start()["state"], "preparing")
            self.assertEqual(len(self.calls), 2)
            probe.side_effect = W.ManagedDispatchError("managed-gateway-not-ready")
            self.assertEqual(self.start()["state"], "preparing")
            probe.assert_not_called()
            self.assertEqual(len(self.calls), 2)

    def test_route_hash_collision_is_never_adopted(self):
        self.start()
        self.jobs.write_text(self.jobs.read_text().replace("route_hash=sha256:probe", "route_hash=sha256:other"))
        self.assertEqual(self.start()["reason"], "work-attempt-identity-conflict")
        self.assertEqual(len(self.calls), 2)

    def adapters(self):
        return [c[c.index("--adapter") + 1] if "--adapter" in c else None for c in self.calls]

    def test_an_owner_pin_stays_with_the_owner_and_does_not_pin_frame_legs(self):
        pins = {"contract_version": 1,
                "owner": {"harness": "opencode", "model": None, "effort": None}}
        self.route["selection_pins"] = pins
        for row in self.route["dispatch_evidence"]["tuples"]:
            row.update(parent_harness="opencode", launch_authority="conductor")
        self.start()
        # Owner selection is retained, while both frame legs stay automatic.
        self.assertEqual(self.adapters(), [None, None])
        self.ready = True; self.released = True
        self.start()
        self.assertEqual(self.adapters(), [None, None, "opencode"])

    def test_a_frame_pin_wins_over_the_owner_pin_for_the_frame_legs_only(self):
        self.route["selection_pins"] = {"contract_version": 1,
            "owner": {"harness": "codex", "model": None, "effort": None},
            "frame": {"harness": "opencode", "model": "provider/model", "effort": "max"}}
        for row in self.route["dispatch_evidence"]["tuples"]:
            row.update(parent_harness="codex", launch_authority="conductor")
        self.start()
        self.assertEqual(self.adapters(), ["opencode", "opencode"])
        self.ready = True; self.released = True
        self.start()
        self.assertEqual(self.adapters()[2], "codex")

    def test_start_repairs_missing_pinned_conductor_evidence_once_before_owner_launch(self):
        import route_authority as RA
        module = W._route_module()
        self.route.update(cwd=self.tmp.name, artifact_root=self.tmp.name,
                          capability="autopilot-code", capability_mode="debug", owner_model_profile="deep",
                          nodes=[{"id": "test"}], selection_pins={"contract_version": 1,
                              "owner": {"harness": "opencode", "model": None, "effort": None}})
        self.route["dispatch_evidence"]["tuples"] = [{"parent_harness": "claude", "child_harness": "codex",
                                                    "status": "supported", "launch_authority": "conductor"}]
        self.path.write_text(json.dumps(self.route))
        before = self.path.read_bytes()
        checked = {"parent_harness": "opencode", "parent_transport": "headless", "parent_sandbox": "adapter-default",
                   "child_harness": "codex", "status": "supported", "launch_authority": "conductor",
                   "checked_worktree": self.tmp.name}
        self.ready = self.released = True
        with mock.patch.object(module, "_compose_readiness", return_value={"tuples": [checked]}) as probe, \
             mock.patch.object(RA, "caller_identity", return_value=("claude", "parent")):
            first = self.start()
            second = self.start()
        self.assertEqual(probe.call_count, 1)
        self.assertTrue(first["pin_evidence"]["changed"])
        self.assertTrue(second["owner_started"])
        self.assertEqual(self.adapters(), ["opencode"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(RA.route_in_force(self.route)["dispatch_evidence"]["tuples"][-1], checked)

    def test_a_pinned_tool_the_route_never_probed_is_not_forced(self):
        self.route["selection_pins"] = {"contract_version": 1,
            "frame": {"harness": "claude", "model": None, "effort": None}}
        result = self.start()
        self.assertEqual(self.adapters(), [None, None])
        self.assertEqual(result["frame_explicit_harness"], "unavailable:claude")

    def test_a_route_without_pins_keeps_automatic_frame_selection(self):
        self.assertNotIn("selection_pins", self.route)
        result = self.start()
        self.assertEqual(self.adapters(), [None, None])
        self.assertNotIn("frame_explicit_harness", result)

    def test_quick_uses_the_sealed_candidate_pool(self):
        self.route["effective_intensity"] = "quick"
        self.route["registered_headless_candidates"] = [{"harness":"opencode","status":"supported"}]
        self.start()
        self.assertTrue(all("--adapter" not in c for c in self.calls))
        self.assertTrue(all(c[c.index("--route-evidence") + 1] == str(self.path) for c in self.calls))

    def test_owner_completion_waits_for_runtime_closure_and_reuses_attempt(self):
        self.route["nodes"] = []
        self.ready = True
        aid = W.attempt_id(self.route, "owner")
        self.statuses[aid] = {"workflow_complete":False}
        result = self.start()
        self.assertEqual(result["result"]["required_action"], "finish-workflow", result)
        self.statuses.clear()
        self.assertEqual(self.start()["state"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_claim_interruption_leaves_admitted_attempt_recoverable(self):
        def interrupted(command, **kwargs):
            result = self.admit(command, **kwargs)
            raise OSError("caller disconnected after admission")
        result = W.start_work(self.route, self.path, self.jobs, run=interrupted)
        self.assertEqual(result["state"], "needs-attention")
        self.assertEqual(result["registered_attempts"], [W.attempt_id(self.route,"frame")])
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_direct_never_spawns(self):
        self.route["effective_intensity"] = "direct"
        import artifact_producer
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                               return_value={"AGENT_ARTIFACT_OUTPUT_DIR": "/exact/artifacts"}) as prepare:
            result = self.start()
        self.assertEqual(result["state"], "inline")
        self.assertEqual(result["artifact_env"]["AGENT_ARTIFACT_OUTPUT_DIR"], "/exact/artifacts")
        prepare.assert_called_once_with(self.path, start=True, jobs=self.jobs, require_cycle=False)
        self.assertEqual(self.calls, [])

    def test_verified_resume_start_defers_owner_and_preserves_checked_fallback(self):
        import artifact_producer
        self.route.update(capability="autopilot-lab", capability_mode="setup", effective_intensity="quick",
            cwd=self.tmp.name, artifact_root=self.tmp.name,
            composed_recipe={"capability":"autopilot-lab","modes":["setup"],
                "compose":{"graph":["resume-run","run-verify"]},
                "standard_plus":{"nodes":[{"id":"resume-run"},{"id":"run-verify"}]}},
            nodes=[{"id":"resume-run"},{"id":"one-shot","worker_type":"owner","dispatch_depth":1}])
        with mock.patch.object(artifact_producer,"prepare_route_artifact_env",
                               return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(Path(self.tmp.name)/"artifacts")}), \
             mock.patch.object(W.RESOURCE_RESUME,"observation",return_value={"state":"resource-ready"}):
            ready=self.start()
        self.assertEqual(ready["state"],"resource-ready",ready)
        self.assertEqual(ready["required_action"],"start-resource")
        self.assertIn("--node resume-run",ready["resource_runner_command"])
        self.assertNotEqual(ready["parent_next"],"end-turn")
        for live in (True,False):
            with mock.patch.object(artifact_producer,"prepare_route_artifact_env",
                                   return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(Path(self.tmp.name)/"artifacts")}), \
                 mock.patch.object(W.RESOURCE_RESUME,"observation",return_value={"state":"resource-running"}), \
                 mock.patch.object(W.RESOURCE_RESUME,"supervisor_alive",return_value=live):
                waiting=self.start()
            self.assertEqual(waiting["state"],"resource-running" if live else "needs-attention")
            self.assertEqual(waiting["parent_next"],"end-turn" if live else "inspect-receipt")
        self.assertEqual(self.calls,[])
        self.assertFalse(self.jobs.exists())

    def test_verified_resume_confirmed_exit_enters_existing_once_only_owner_path(self):
        self.route.update(capability="autopilot-lab", capability_mode="setup", effective_intensity="quick",
            cwd=self.tmp.name, artifact_root=self.tmp.name,
            composed_recipe={"capability":"autopilot-lab","modes":["setup"],
                "compose":{"graph":["resume-run","run-verify"]},
                "standard_plus":{"nodes":[{"id":"resume-run"},{"id":"run-verify"}]}},
            nodes=[{"id":"resume-run"},{"id":"one-shot","worker_type":"owner","dispatch_depth":1}])
        with mock.patch.object(W.RESOURCE_RESUME,"observation",return_value={"state":"resource-succeeded"}), \
             mock.patch.object(W,"prepare_task_request",return_value=None), \
             mock.patch.object(W.RESOURCE_RESUME,"verification_prompt",return_value="\nONLY independent post-run verification"):
            first=self.start()
            again=self.start()
        self.assertEqual(first["state"],"running")
        self.assertEqual(again["state"],"running")
        self.assertEqual(len(self.calls),1)
        task=self.calls[0][self.calls[0].index("--prompt-text")+1]
        self.assertIn("ONLY independent post-run verification",task)
        self.assertNotIn("frame",self.calls[0])

    def test_closed_request_replay_never_launches_or_prepares_artifacts(self):
        import artifact_producer
        outcome = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                   "terminal_gate_proven": True}
        self.path.with_suffix(".outcome.json").write_text(json.dumps(outcome))
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("reopen")):
            for intensity in ("direct", "standard"):
                self.route["effective_intensity"] = intensity
                self.assertEqual(self.start()["state"], "completed")
        self.assertEqual(self.calls, [])
        outcome["terminal_gate_proven"] = False
        self.path.with_suffix(".outcome.json").write_text(json.dumps(outcome))
        self.assertEqual(self.start()["reason"], "route-closed-unproven")

    def test_closed_spec_publication_gap_retries_without_preparing_or_restarting_work(self):
        import artifact_producer
        self.route["capability"] = "autopilot-spec"
        artifact_root = Path(self.tmp.name) / "artifacts"
        artifact_root.mkdir()
        self.route["artifact_root"] = str(artifact_root.resolve())
        self.path.write_text(json.dumps(self.route))
        outcome = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                   "terminal_gate_proven": True}
        outcome_path = self.path.with_suffix(".outcome.json")
        outcome_path.write_text(json.dumps(outcome))
        original = outcome_path.read_bytes()
        pending = {"cycle_id": "cyc-original", "status": "pending", "reason": "shared-spec-conflict"}
        admitted = {"cycle_id": "cyc-original", "status": "admitted", "shared_reference_revision_id": "rrev-original"}
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("reopen")), \
             mock.patch.object(artifact_producer, "route_cycle_for", return_value={"cycle_id": "cyc-original"}), \
             mock.patch.object(artifact_producer, "completed_spec_publication", side_effect=[pending, admitted]) as publish:
            first = self.start()
            self.assertEqual((first["state"], first["reason"]), ("needs-attention", "shared-spec-publication-pending"))
            self.assertEqual(first["shared_publication"], pending)
            second = self.start()
            self.assertEqual(second["state"], "completed")
            self.assertEqual(second["shared_publication"], admitted)
            self.assertEqual(first["resume_command"], second["resume_command"])
            self.assertEqual(publish.call_count, 2)
            self.assertTrue(all(call.kwargs == {"cycle_id": "cyc-original", "settle": True}
                                for call in publish.call_args_list))
        self.assertEqual(outcome_path.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_closed_spec_missing_artifact_root_is_typed_pending_without_guessing_or_restarting(self):
        import artifact_producer
        self.route["capability"] = "autopilot-spec"
        outcome_path = self.path.with_suffix(".outcome.json")
        outcome_path.write_text(json.dumps({"route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "terminal_gate_proven": True}))
        original = outcome_path.read_bytes()
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("reopen")), \
             mock.patch.object(artifact_producer, "route_cycle_for", side_effect=AssertionError("guessed root")), \
             mock.patch.object(artifact_producer, "completed_spec_publication", side_effect=AssertionError("no root")):
            for root in (None, "", " "):
                if root is None:
                    self.route.pop("artifact_root", None)
                else:
                    self.route["artifact_root"] = root
                self.path.write_text(json.dumps(self.route))
                receipt = self.start()
                self.assertEqual((receipt["state"], receipt["reason"]),
                                 ("needs-attention", "shared-spec-publication-pending"))
                self.assertEqual(receipt["shared_publication"],
                                 {"status": "pending", "reason": "spec-artifact-root-unavailable"})
                self.assertEqual(receipt["launches"], [])
                self.assertFalse(receipt["owner_started"])
                self.assertEqual(outcome_path.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_closed_runtime_owner_retries_only_post_seal_publication_and_keeps_the_returned_revision(self):
        import artifact_producer
        import dispatch_terminal_commit as terminal
        self.route["capability"] = "autopilot-spec"
        self.path.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "terminal_gate_proven": True, "terminal_owner_attempt_id": "att-original"}))
        meta = {"workflow_completion": "runtime-v1", "owner_route_id": self.route["route_id"]}
        publication = {"status": "admitted", "shared_reference_revision_id": "rrev-original"}
        with mock.patch.object(W, "_rows", return_value={"att-original": ("done", meta)}), \
             mock.patch.object(terminal, "owner_completion_state", side_effect=[
                 terminal.CompletionState("pending", "shared-spec-publication-pending"), terminal.CompletionState("complete")]), \
             mock.patch.object(terminal, "settle_owner_completion", return_value=terminal.TerminalCommitResult(
                 "completed", shared_publication=publication)) as settle, \
             mock.patch.object(artifact_producer, "completed_spec_publication") as duplicate, \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("reopen")):
            result = self.start()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["shared_publication"], publication)
        settle.assert_called_once_with(self.jobs, "done", meta)
        duplicate.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_direct_start_replay_is_fenced_while_inline_finish_is_pending(self):
        import artifact_producer
        import inline_finish
        pending = {"schema": "inline_finish_v1", "inline_finish_id": "deadbeef",
                   "state": "node-completed", "intent": {"route_id": self.route["route_id"]}}
        with mock.patch.object(inline_finish, "pending_state", return_value=pending), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                               side_effect=AssertionError("must not resume preparation")):
            for intensity in ("direct", "standard"):
                self.route["effective_intensity"] = intensity
                result = self.start()
                self.assertEqual(result["state"], "needs-attention", result)
                self.assertEqual(result["reason"], "finish-pending")
                self.assertEqual(result["required_action"], "resume-inline-finish")
                self.assertEqual(result["finish_state"], "node-completed")
        self.assertEqual(self.calls, [])

    def test_closed_runtime_owner_requires_complete_settlement(self):
        import dispatch_terminal_commit as terminal
        outcome = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                   "terminal_gate_proven": True, "terminal_owner_attempt_id": "att-owner"}
        self.path.with_suffix(".outcome.json").write_text(json.dumps(outcome))
        with mock.patch.object(W, "_rows", return_value={"att-owner": ("done", {"workflow_completion": "runtime-v1"})}):
            for state in ("pending", "blocked", "unknown", "complete"):
                with mock.patch.object(terminal, "owner_completion_state", return_value=terminal.CompletionState(state)):
                    self.assertEqual(self.start()["state"], "completed" if state == "complete" else "needs-attention")
        self.assertEqual(self.calls, [])

    def test_production_selector_and_three_adapter_parsers_accept_the_public_request(self):
        router = load("work_start_router_test", W.ROOT / "utilities/capability-route.py")
        selector = load("work_start_selector_test", W.ROOT / "utilities/dispatch-owner.py")
        evidence = {"tuples": [{"parent_harness":"codex", "child_harness":h,
            "parent_transport":"headless", "parent_sandbox":"workspace-write", "launch_authority":"conductor",
            "status":"supported", "probe_source":"fixture-check", "probe_time":"2026-09-12T00:00:00Z",
            "failure_class":"", "checked_worktree":str(W.ROOT), "failure_scope":"none",
            "codex_command":"ok", "retry_on_isolated_worktree":0} for h in ("codex","opencode")]}
        env = {k:v for k,v in os.environ.items() if not k.startswith(("AGENT_DISPATCH_","AGENT_ROUTE_","AGENT_OWNER_ROUTE_","AGENT_ARTIFACT_"))}
        env["AGENT_HOME"] = str(W.ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            route = router.compose_route(capability="autopilot-code", capability_mode="dev", shape="staged",
                graph="frame,frame-alternative,test,report", slug="work-start", cwd=W.ROOT,
                artifact_root=self.tmp.name, dispatch_evidence=evidence, parent_harness="codex", profile="light",
                work_request={"text":"Run both commands and record exit 7 and exit 0.","owner_harness":"codex"},
                unassigned=True)
            router.verify_route(route, W.ROOT)
            self.path.write_text(json.dumps(route))
            for node in ("frame", "frame-alternative", "owner"):
                for harness in ("claude", "codex", "opencode"):
                    with self.subTest(node=node,harness=harness):
                        def run(command, **kwargs):
                            _, values, forwarded, _, _ = selector._parse(command[2:])
                            adapter = load("work_start_adapter_"+harness, W.ROOT / "adapters" / harness / "bin/dispatch-headless.py")
                            args = adapter.parser().parse_args(forwarded)
                            self.assertEqual(args.worker_type,"owner" if node=="owner" else "frame")
                            self.assertEqual(args.dispatch_depth,1)
                            self.assertEqual(adapter.resolve_model_settings(args)["profile"],"light")
                            if node == "owner":
                                self.assertEqual(args.prompt_text,route["work_request"]["text"])
                            else:
                                # A frame leg reads a prompt file: the request first, then the part catalogue.
                                self.assertIsNone(args.prompt_text)
                                text = Path(args.prompt_file).read_text(encoding="utf-8")
                                self.assertTrue(text.startswith(route["work_request"]["text"] + "\n"))
                                self.assertIn("## Part catalogue", text)
                            self.assertEqual(args.attempt_id,W.attempt_id(route,node))
                            return subprocess.CompletedProcess(command,0,"validated","")
                        W._start(route,self.path,self.jobs,node,harness,run)


WF = load("work_start_workflow_fixture", W.ROOT / "utilities/workflow_supervisor.test.py")


class FrameInterviewStepTest(WF.WorkflowFixture):
    """Real ledger/gate/answer/intent code; transport and producer location are isolated."""
    def setUp(self):
        super().setUp()
        self.route, self.path = self.two_stage_route(human_gate="frame-review",
            continuation={"kind": "human-gate", "gate": "frame-review"})
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("")
        self.output = self.base / "artifacts"
        self.calls = []
        self.question = {"understanding": "Run the two commands and preserve their actual results.",
            "brief": {"problem": "We need the measured results.", "outcome": "One report with both results.",
                "affected": "The report only.", "constraints": "Preserve the expected exit code 7.", "open": ""},
            "questions": []}
        self.question_file = self.base / "question.json"
        self.question_file.write_text(json.dumps(self.question))
        import artifact_producer
        for patch in (
            mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.output)}),
            mock.patch.object(WF.SUP, "create_gate_delivery", return_value=(self.base / "gate-record.json", True)),
            mock.patch.object(WF.SUP, "retire_gate_delivery", return_value="acked"),
        ):
            patch.start(); self.addCleanup(patch.stop)

    def run_command(self, argv, **kwargs):
        self.calls.append(argv[2])
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = WF.SUP.main(argv[2:])
        except (WF.SUP.SupervisorError, WF.WS.WorkflowStateError) as exc:
            rc = 64; stderr.write(str(exc))
        return subprocess.CompletedProcess(argv, rc, stdout.getvalue(), stderr.getvalue())

    def step(self, **kwargs):
        return W.frame_interview_step(self.route, self.path, self.jobs, run=self.run_command, **kwargs)

    def answers(self):
        import frame_interview as FI
        a = FI.answers_template({**self.question, "route_id": self.route["route_id"]})
        a["actor_kind"] = "user"
        a["understanding_confirmed"] = True
        path = self.base / "answers.json"
        path.write_text(json.dumps(a))
        return path

    def resolution(self):
        ledger = WF.WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        return WF.WS.human_gate_resolution(ledger.journal(), "frame-review")

    def test_nonuser_complete_answer_does_not_occupy_the_pending_human_answer(self):
        import frame_interview as FI
        import tidy_decisions
        question, answer = self.routed()
        asked = self.step(interview=self.question_file)
        registered = Path(asked["interview_file"])
        question_bytes = registered.read_bytes()
        original = json.loads(answer.read_text())
        with mock.patch.object(tidy_decisions, "record_interview_answers", return_value="recorded") as record:
            for kind in (None, "unknown", "automatic", "headless-owner"):
                with self.subTest(kind=kind):
                    supplied = {**original, "actor_kind": kind}
                    if kind is None:
                        del supplied["actor_kind"]
                    answer.write_text(json.dumps(supplied))
                    with self.assertRaisesRegex(ValueError, "gate-release-authority-refused"):
                        self.step(answers=answer)
                    self.assertEqual(registered.read_bytes(), question_bytes)
                    self.assertEqual((self.resolution()["status"], self.resolution()["epoch"]), ("blocked", 1))
                    self.assertIsNone(self.resolution()["answers"])
                    self.assertFalse((registered.parent / "answers.json").exists())
                    self.assertFalse((self.output / "shards/frame/intent.md").exists())
                    self.assertEqual((self.calls, record.call_count), (["gate"], 0))
            for payload in ({"accepted": True, "actor_kind": "user"},
                            {**FI.answers_template(json.loads(question_bytes)), "actor_kind": "user"}):
                answer.write_text(json.dumps(payload))
                self.assertEqual(self.step(answers=answer)["state"], "needs-question")
                self.assertFalse((registered.parent / "answers.json").exists())
            answer.write_text(json.dumps(original))
            released = self.step(answers=answer)
            self.assertEqual(released["state"], "released")
            self.assertEqual(self.resolution()["actor_kind"], "user")
            self.assertEqual(self.resolution()["answers"]["answers"]["go"]["choice"], 0)
            self.assertEqual(self.step(answers=answer), released)
            self.assertEqual((self.calls, record.call_count), (["gate", "release"], 1))

    def test_a_supervisor_answer_releases_frame_review_on_the_persons_behalf(self):
        question, _ = self.routed()
        asked = self.step(interview=self.question_file)
        answer = self.base / "answers.json"
        response = asked["supervisor_answers_template"]
        self.assertEqual(response["actor_kind"], "supervisor")
        response["understanding_confirmed"] = True
        for item in question["questions"]:
            self.assertIsNone(response["answers"][item["id"]]["choice"])
            response["answers"][item["id"]]["choice"] = item["recommended"]
        answer.write_text(json.dumps(response))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
            with self.assertRaisesRegex(ValueError, "gate-release-actor-refused"):
                self.step(answers=answer)                    # a registered worker is not the supervisor
        self.assertEqual(self.resolution()["status"], "blocked")
        released = self.step(answers=answer)
        self.assertEqual(released["state"], "released")
        self.assertEqual(self.resolution()["actor_kind"], "supervisor")
        self.assertEqual(self.resolution()["answers"]["answers"]["go"]["choice"], 0)
        self.assertIn("status: agreed-on-behalf\nactor_kind: supervisor", Path(released["intent_file"]).read_text())
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(Path(asked["interview_file"]).parent, Path(released["interview_file"]).parent)

    def test_registered_caller_cannot_claim_a_user_before_immutable_save(self):
        answer = self.answers()
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
            with self.assertRaisesRegex(ValueError, "gate-release-actor-refused"):
                self.step(interview=self.question_file, answers=answer)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.output / "shards/frame/round-1/answers.json").exists())

    def test_old_interview_person_authority_precedes_any_binding_before_save(self):
        self.route["human_gate_bindings"][0]["release_authority"] = "any"
        self.path.write_text(json.dumps(self.route))
        asked = self.step(interview=self.question_file)
        question = Path(asked["interview_file"])
        before = question.read_bytes()
        legacy = {**self.resolution(), "release_authority": None}
        answer = self.answers()
        response = json.loads(answer.read_text())
        response["actor_kind"] = "automatic"
        answer.write_text(json.dumps(response))
        with mock.patch.object(WF.WS, "human_gate_resolution", return_value=legacy):
            with self.assertRaisesRegex(ValueError, "gate-release-authority-refused"):
                self.step(answers=answer)
        self.assertFalse((question.parent / "answers.json").exists())
        self.assertFalse((self.output / "shards/frame/intent.md").exists())
        self.assertEqual(question.read_bytes(), before)
        self.assertEqual(self.calls, ["gate"])
        self.assertEqual(self.resolution()["status"], "blocked")

    def test_the_runtime_mapping_is_stored_with_the_question_and_the_marked_file_replays(self):
        mapped = {"question": "go", "by_option": {"Run": {
            "summary": "Run both commands.", "legs": [{"capability": "autopilot-code", "shape": "direct"}],
            "entry_approvals": [], "execution_scope": "complete"}}}

        def mark(route, jobs, interview):
            marks = any("proposal" in option for item in interview.get("questions", []) for option in item["options"])
            return (interview if "route_proposals" in interview or not marks
                    else {**interview, "route_proposals": mapped}), []
        marked = {**self.question, "questions": [{
            "id": "go", "topic": "Run it", "question": "Run both commands now?", "kind": "yes-no",
            "options": [{"label": "Run", "means": "Run them now.", "proposal": "frame"},
                        {"label": "Wait", "means": "Do nothing yet."}],
            "recommended": 0, "why": "Only you can say when."}]}
        self.question_file.write_text(json.dumps(marked))
        answer = self.base / "go.json"
        answer.write_text(json.dumps({"actor_kind": "user", "understanding_confirmed": True,
                                      "answers": {"go": {"choice": 0}}}))
        with mock.patch.object(W, "_marked_route_proposals", side_effect=mark):
            asked = self.step(interview=self.question_file)
            self.assertEqual(json.loads(Path(asked["interview_file"]).read_text())["route_proposals"], mapped)
            released = self.step(interview=self.question_file, answers=answer)
            self.assertEqual(released["state"], "released")
            self.assertEqual(self.step(interview=self.question_file, answers=answer), released)
        self.assertIn("Selected route: Run both commands.", Path(released["intent_file"]).read_text())
        self.assertEqual(self.calls, ["gate", "release"])

    def test_every_interview_error_is_reported_at_once(self):
        needs = self.step()
        import frame_interview as FI
        self.assertEqual(needs["question_example"], FI.QUESTION_EXAMPLE)
        bad = {**self.question, "questions": [
            {"id": f"q-{index}", "topic": f"topic {index}", "question": "", "kind": "?", "options": [],
             "recommended": None, "why": ""} for index in range(3)]}
        self.question_file.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid") as caught:
            self.step(interview=self.question_file)
        expected = FI.validate({**bad, "schema": FI.SCHEMA, "route_id": self.route["route_id"], "summary": "-"},
                               intensity=self.route["effective_intensity"])
        self.assertGreater(len(expected), 8)
        self.assertEqual(str(caught.exception), "frame-input-invalid: " + "; ".join(expected))
        self.assertEqual(self.calls, [])

    def test_the_interview_template_is_drafted_from_the_request_and_the_frame_brief(self):
        brief = self.output / "shards/frame/direction-brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("## 1. Problem Statement\n\nThe report loses the second result.\n\n"
                         "## 4. Direction Verdict\n\nKeep both results in one report.\n\n"
                         "## 6. Open Risks\n\nNone known.\n", encoding="utf-8")
        route = {**self.route, "work_request": {"text": "Run the two commands and keep both results. Then report."}}
        template = W.frame_interview_step(route, self.path, self.jobs, run=self.run_command)["interview_template"]
        self.assertEqual(template["understanding"], "Run the two commands and keep both results.")
        self.assertEqual(template["brief"], {"problem": "The report loses the second result.",
                                             "outcome": "Keep both results in one report.", "affected": "",
                                             "constraints": "", "open": "None known."})
        brief.unlink()                                       # no brief yet: empty fields, as before
        self.assertEqual(self.step()["interview_template"]["brief"]["problem"], "")
        self.assertEqual(self.calls, [])                     # a draft registers nothing

    def test_generated_draft_registers_unchanged_and_explicit_supervisor_answer_releases_first_time(self):
        import frame_interview as FI
        brief = self.output / "shards/frame/direction-brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("## 1. Problem\n\nThe report loses the second result.\n\n"
                         "## 4. Verdict\n\nKeep both results in one report.\n\n"
                         "## 5. Boundary\n\nPreserve the measured exit codes.\n\n"
                         "## 6. Risks\n\nNone known.\n", encoding="utf-8")
        self.route["work_request"] = {"text": "Run the two commands and keep both results."}
        draft = self.step()
        self.assertIn('"actor_kind": "supervisor"', draft["next_step"])
        self.assertIn('"actor_kind": "user"', draft["next_step"])
        self.assertEqual(draft["interview_template"]["brief"]["affected"], "")
        self.question_file.write_text(json.dumps(draft["interview_template"]))
        asked = self.step(interview=self.question_file)
        self.assertEqual(asked["state"], "needs-question")
        self.assertIn(FI.ANSWER_SOURCE_GUIDANCE, asked["next_step"])
        self.assertEqual(asked["answers_template"]["actor_kind"], "unknown")
        response = asked["supervisor_answers_template"]
        self.assertEqual(response["actor_kind"], "supervisor")
        response["understanding_confirmed"] = True
        answer = self.base / "supervisor-answer.json"
        answer.write_text(json.dumps(response))
        released = self.step(answers=answer)
        self.assertEqual(released["state"], "released")
        self.assertEqual(self.resolution()["answers"]["actor_kind"], "supervisor")
        self.assertIn("status: agreed-on-behalf\nactor_kind: supervisor",
                      Path(released["intent_file"]).read_text())
        self.assertEqual(self.calls, ["gate", "release"])

    def test_register_before_question_then_actual_answers_release_once(self):
        self.assertEqual(self.step()["state"], "needs-interview")
        asked = self.step(interview=self.question_file)
        self.assertEqual(asked["state"], "needs-question")
        import frame_interview as FI
        self.assertIn(FI.PENDING_ANSWER_RULE, asked["next_step"])
        self.assertEqual(self.resolution()["status"], "blocked")
        self.assertEqual(self.step()["state"], "needs-question")
        self.assertEqual(self.resolution()["epoch"], 1)
        result = self.step(answers=self.answers())
        self.assertEqual(result["state"], "released")
        intent = Path(result["intent_file"]).read_bytes()
        self.assertIn(b"status: agreed", intent)
        self.assertEqual(self.resolution()["answers"]["understanding_confirmed"], True)
        self.assertEqual(self.step(answers=self.answers())["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(Path(result["intent_file"]).read_bytes(), intent)

    def test_expired_empty_response_and_async_ack_keep_same_question_until_actual_reply(self):
        import frame_interview as FI
        import tidy_decisions
        question, real_answers = self.routed(approve=1)
        asked = self.step(interview=self.question_file)
        self.assertEqual(asked["required_action"], "ask-registered-question")
        expected = ("Run the two commands and preserve their actual results.\n\n"
                    "이 순서로 진행할까요?\n"
                    "- 예, 이 순서로: 예, 이 순서로\n"
                    "- 아니요, 다르게: 아니요, 다르게\n\n"
                    "긴 실험을 지금 시작해도 될까요?\n"
                    "- 예, 시작: 예, 시작\n"
                    "- 아니요, 나중에: 아니요, 나중에")
        self.assertEqual(asked["human_wait"], {"state": "pending", "fallback": "ordinary-conversation",
                                              "question_block": expected})
        self.assertNotIn("Confirm this understanding", asked["human_wait"]["question_block"])
        self.assertIn("understanding sentence verbatim as the first question", asked["next_step"])
        self.assertIn("in the same native question call when the tool accepts them all", asked["next_step"])
        self.assertIn("the runtime combines the calls", asked["next_step"])
        self.assertNotRegex(asked["next_step"], r"\b(at most|up to|no more than|maximum)\s+\d")
        registered = Path(asked["interview_file"])
        original = registered.read_bytes()
        answers_path = registered.parent / "answers.json"
        pending_file = self.base / "native-response.json"
        with mock.patch.object(tidy_decisions, "record_interview_answers") as record:
            for payload in ({"answers": {}}, {"timeout": True, "answers": {}}, {"accepted": True},
                            FI.answers_template(json.loads(original)),
                            {"answers": {"go": {"answers": []}, "run-ok": {"answers": []}}}):
                with self.subTest(payload=payload):
                    pending_file.write_text(json.dumps(payload))
                    result = self.step(answers=pending_file)
                    self.assertEqual(result["state"], "needs-question")
                    self.assertEqual(result["required_action"], "wait-for-user-answer")
                    self.assertEqual(result["parent_next"], "end-turn")
                    self.assertEqual(result["human_wait"]["question_block"], expected)
                    self.assertEqual(result["interview_file"], str(registered))
                    self.assertEqual(registered.read_bytes(), original)
                    self.assertFalse(answers_path.exists())
                    self.assertFalse((registered.parent.parent / "intent.md").exists())
                    self.assertEqual((self.resolution()["status"], self.resolution()["epoch"]), ("blocked", 1))
                    self.assertIsNone(self.resolution()["answers"])
                    self.assertEqual(self.calls, ["gate"])
                    self.assertEqual(record.call_count, 0)
            resumed = self.step()
            self.assertEqual(resumed["required_action"], "wait-for-user-answer")
            self.assertEqual(resumed["human_wait"]["question_block"], expected)
            released = self.step(answers=real_answers)
            self.assertEqual(released["state"], "released")
            self.assertEqual(self.calls, ["gate", "release"])
            self.assertEqual(record.call_count, 1)
            given = self.resolution()["answers"]
            self.assertEqual(given["answers"]["run-ok"]["choice"], 1)
            self.assertEqual(FI.approvals_given(json.loads(original), given,
                question["route_proposals"]["by_option"]["예, 이 순서로"])[0]["accepted"], False)
            intent = Path(released["intent_file"]).read_bytes()
            self.assertIn("question `run-ok`: declined".encode(), intent)
            self.assertEqual(self.step(answers=real_answers)["state"], "released")
            self.assertEqual(self.calls, ["gate", "release"])
            self.assertEqual(record.call_count, 1)
            self.assertEqual(Path(released["intent_file"]).read_bytes(), intent)

    def test_invalid_or_mixed_native_responses_do_not_disappear_into_humanwait(self):
        self.routed()
        asked = self.step(interview=self.question_file)
        registered = Path(asked["interview_file"])
        original = registered.read_bytes()
        bad = self.base / "bad-response.json"
        valid = json.loads(self.base.joinpath("routed-answers.json").read_text())
        for response in ({"route_id": "rt-foreign", "answers": {}},
                         {"round": 2, "answers": {}}, {"schema": "foreign/v1", "answers": {}},
                         {"answers": []}, {"accepted": "yes"},
                         {"answers": {"unknown": {"answers": []}}},
                         {"answers": {"go": {"choice": 50}}},
                         {"understanding_confirmed": True, "answers": {"go": {"choice": 0}}},
                         {**valid, "understanding_confirmed": 1},
                         {**valid, "answers": {"go": {"choice": False}, "run-ok": {"choice": 0}}},
                         {**valid, "accepted": True}):
            with self.subTest(response=response):
                bad.write_text(json.dumps(response))
                with self.assertRaisesRegex(ValueError, "frame-input-invalid"):
                    self.step(answers=bad)
                self.assertEqual(registered.read_bytes(), original)
                self.assertFalse((registered.parent / "answers.json").exists())
                self.assertEqual((self.resolution()["status"], self.resolution()["epoch"]), ("blocked", 1))
                self.assertEqual(self.calls, ["gate"])

    def test_explicit_stop_after_unanswered_question_keeps_its_original_meaning(self):
        self.routed()
        asked = self.step(interview=self.question_file)
        self.assertEqual(self.step()["required_action"], "wait-for-user-answer")
        stopped = self.step(decision="stop")
        self.assertEqual(stopped["state"], "cancelled")
        self.assertEqual(self.resolution()["status"], "stop")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertFalse((Path(asked["interview_file"]).parent / "answers.json").exists())
        self.assertEqual(self.step()["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_actual_approval_after_async_accepted_releases_once(self):
        self.routed()
        self.step(interview=self.question_file)
        ack = self.base / "accepted.json"
        ack.write_text('{"accepted": true}')
        self.assertEqual(self.step(answers=ack)["state"], "needs-question")
        self.assertEqual(self.resolution()["status"], "blocked")
        self.assertEqual(self.calls, ["gate"])
        real = self.base / "routed-answers.json"
        result = self.step(answers=real)
        self.assertEqual(result["state"], "released")
        self.assertIn("question `run-ok`: approved", Path(result["intent_file"]).read_text())
        self.assertEqual(self.resolution()["answers"]["answers"]["run-ok"]["choice"], 0)
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(self.step(answers=real)["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
    def routed(self, *, approve=0, route_choice=0):
        """An interview that maps its one route question to a proposal with a start approval."""
        import frame_interview as FI
        leg = {"capability": "autopilot-lab", "mode": "setup", "shape": "staged",
               "graph": ["scaffold", "smoke", "full-run", "run-verify", "handoff"]}
        proposal = {"summary": "Set the experiment up and run it", "legs": [leg],
                    "entry_approvals": [{"key": "full-run", "leg": 0, "question": "run-ok"}]}
        ask = lambda qid, text, labels: {"id": qid, "topic": qid, "question": text, "kind": "yes-no", "recommended": 0,
                                         "options": [{"label": label, "means": label, **({"approves": True} if at == 0 else {})}
                                                     for at, label in enumerate(labels)],
                                         "why": "Only you can decide this."}
        question = {**self.question, "questions": [
            ask("go", "이 순서로 진행할까요?", ["예, 이 순서로", "아니요, 다르게"]),
            ask("run-ok", "긴 실험을 지금 시작해도 될까요?", ["예, 시작", "아니요, 나중에"])],
            "route_proposals": {"question": "go", "by_option": {"예, 이 순서로": proposal}}}
        self.question_file.write_text(json.dumps(question))
        response = FI.answers_template({**question, "route_id": self.route["route_id"]})
        response["actor_kind"] = "user"
        response["understanding_confirmed"] = True
        response["answers"] = {"go": {"choice": route_choice, "note": ""}, "run-ok": {"choice": approve, "note": ""}}
        answer = self.base / "routed-answers.json"
        answer.write_text(json.dumps(response))
        return question, answer

    def test_a_route_proposals_interview_renders_the_route_and_the_approval_into_the_intent(self):
        question, answer = self.routed()
        result = self.step(interview=self.question_file, answers=answer)
        self.assertEqual(result["state"], "released")
        intent = Path(result["intent_file"]).read_text()
        self.assertIn("## Route", intent)
        self.assertIn("Selected route: Set the experiment up and run it", intent)
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run) — question `run-ok`: approved", intent)
        self.assertEqual(self.resolution()["answers"]["answers"]["run-ok"]["choice"], 0)
        self.assertEqual(self.step(answers=answer)["state"], "released")         # replay: same bytes, no rewrite
        self.assertEqual(Path(result["intent_file"]).read_text(), intent)
        self.assertEqual(self.calls, ["gate", "release"])

    def test_declining_the_approval_or_the_route_is_a_valid_answer_and_is_written_as_such(self):
        _, declined = self.routed(approve=1)
        intent = Path(self.step(interview=self.question_file, answers=declined)["intent_file"]).read_text()
        self.assertIn("question `run-ok`: declined", intent)
        self.setUp()
        _, off = self.routed(route_choice="none")
        self.question_file.write_text(json.dumps({**json.loads(self.question_file.read_text())}))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid"):          # off-menu needs its note
            self.step(interview=self.question_file, answers=off)
        answers = json.loads(off.read_text()); answers["answers"]["go"]["note"] = "Do something else."
        off.write_text(json.dumps(answers))
        intent = Path(self.step(interview=self.question_file, answers=off)["intent_file"]).read_text()
        self.assertIn("No route was selected (a different direction was chosen)", intent)

    def test_an_approval_question_without_exactly_one_approving_option_is_refused_before_a_gate_is_raised(self):
        for label, marks in (("none marked", (False, False)), ("both marked", (True, True))):
            with self.subTest(label):
                self.setUp()
                question, answer = self.routed()
                for option, mark in zip(question["questions"][1]["options"], marks):
                    option.pop("approves", None)
                    if mark:
                        option["approves"] = True
                self.question_file.write_text(json.dumps(question))
                with self.assertRaisesRegex(ValueError, "frame-input-invalid: approval question 'run-ok'"):
                    self.step(interview=self.question_file, answers=answer)
                self.assertEqual(self.calls, [])

    def test_a_route_proposals_reference_that_points_nowhere_is_refused_before_a_gate_is_raised(self):
        question, answer = self.routed()
        question["route_proposals"]["question"] = "nope"
        self.question_file.write_text(json.dumps(question))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid: route_proposals.question: no such question"):
            self.step(interview=self.question_file, answers=answer)
        self.assertEqual((self.calls, self.resolution()["status"]), ([], "not-raised"))

    def test_an_interview_without_the_field_renders_the_same_intent_as_before(self):
        import frame_interview as FI
        result = self.step(interview=self.question_file, answers=self.answers())
        intent = Path(result["intent_file"]).read_text()
        interview = json.loads(Path(result["interview_file"]).read_text())
        self.assertNotIn("route_proposals", interview)
        self.assertEqual(intent, FI.render_intent(interview, self.resolution()["answers"], now=interview["created"]))
        self.assertNotIn("## Route", intent)

    def test_a_crash_after_the_answers_or_the_intent_are_saved_resumes_to_one_release(self):
        for point in ("after-answer-save", "after-intent-render"):
            with self.subTest(point):
                self.setUp()
                question, answer = self.routed()
                def stop(name, point=point):
                    if name == point:
                        raise RuntimeError("crash at " + name)
                W.FAULT_HOOK = stop
                self.addCleanup(setattr, W, "FAULT_HOOK", None)
                with self.assertRaisesRegex(RuntimeError, "crash at " + point):
                    self.step(interview=self.question_file, answers=answer)
                W.FAULT_HOOK = None
                self.assertNotEqual(self.resolution()["status"], "proceed")      # not released yet
                result = self.step(interview=self.question_file, answers=answer)
                self.assertEqual(result["state"], "released")
                self.assertEqual(self.calls.count("release"), 1)
                self.assertIn("## Route", Path(result["intent_file"]).read_text())
                self.assertEqual(self.step(answers=answer)["state"], "released")
                self.assertEqual(self.calls.count("release"), 1)

    def test_answers_are_recorded_as_decisions_only_by_the_release_not_by_the_start_path(self):
        """D-87: `release` is the one place an answer is accepted; the start path calls it
        and must not record a second time, however often the same answers are replayed."""
        import frame_interview as FI
        import tidy_decisions
        question = {**self.question, "questions": [{
            "id": "q-scope", "topic": "How much to change",
            "question": "Fix only the approval step, or the questions too?", "kind": "choice",
            "options": [{"label": "Both (recommended)", "means": "Fix both."},
                        {"label": "Approval only", "means": "Leave the questions."}],
            "recommended": 0, "why": "Only you can weigh the wording against the schedule."}]}
        self.question_file.write_text(json.dumps(question))
        template = FI.answers_template({**question, "route_id": self.route["route_id"]})
        template["actor_kind"] = "user"
        template["understanding_confirmed"] = True
        template["answers"]["q-scope"].update(choice=0, note="go on")
        answers = self.base / "answers.json"
        answers.write_text(json.dumps(template))
        with mock.patch.object(tidy_decisions, "record_interview_answers", return_value="recorded") as record:
            self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
            self.assertEqual(record.call_count, 0)
            self.assertEqual(self.step(answers=answers)["state"], "released")
            self.assertEqual(self.step(answers=answers)["state"], "released")
            self.assertEqual(self.step(interview=self.question_file, answers=answers)["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.kwargs["actor_kind"], "user")
        interview, given = record.call_args.args
        self.assertEqual(interview["questions"][0]["id"], "q-scope")
        self.assertEqual(given["answers"]["q-scope"]["note"], "go on")

    def test_already_received_answers_register_and_release_without_reasking(self):
        result = self.step(interview=self.question_file, answers=self.answers())
        self.assertEqual(result["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_committed_answer_replay_does_not_reopen_or_write_a_sealed_cycle(self):
        import artifact_producer
        answer = self.answers()
        result = self.step(interview=self.question_file, answers=answer)
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("sealed cycle")), \
             mock.patch.object(W, "_store_once", side_effect=AssertionError("sealed write")):
            self.assertEqual(self.step(answers=answer), result)
            self.assertEqual(self.step(interview=self.question_file), result)
        self.assertEqual(self.calls, ["gate", "release"])

    def test_legacy_untyped_committed_reply_replays_without_writing_or_new_memory(self):
        import artifact_producer
        import tidy_decisions
        answer = self.answers()
        result = self.step(interview=self.question_file, answers=answer)
        current = self.resolution()
        legacy = {**current, "answers": dict(current["answers"])}
        del legacy["answers"]["actor_kind"]
        original = json.loads(Path(result["interview_file"]).read_text())
        legacy_file = self.base / "legacy-answers.json"
        legacy_file.write_text(json.dumps(legacy["answers"]))
        with mock.patch.object(WF.WS, "human_gate_resolution", return_value=legacy), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("sealed cycle")), \
             mock.patch.object(W, "_store_once", side_effect=AssertionError("history rewrite")), \
             mock.patch.object(tidy_decisions, "record_interview_answers") as memory:
            self.assertEqual(self.step(answers=legacy_file), result)
            recorded, context = W._recorded_interview(self.route, self.jobs)
            self.assertEqual(recorded, original)
            self.assertEqual(context["actor_kind"], "user")
            self.assertNotIn("actor_kind", legacy["answers"])
            memory.assert_not_called()
        self.assertEqual(self.resolution(), current)
        self.assertEqual(self.calls, ["gate", "release"])

    def test_lost_release_response_replays_exact_committed_answer(self):
        original = self.run_command
        def lose(argv, **kwargs):
            r = original(argv, **kwargs)
            if argv[2] == "release":
                return subprocess.CompletedProcess(argv, 70, "", "lost reply")
            return r
        with self.assertRaisesRegex(ValueError, "frame-release-pending"):
            W.frame_interview_step(self.route,self.path,self.jobs,interview=self.question_file,
                                  answers=self.answers(),run=lose)
        self.assertEqual(self.resolution()["status"], "proceed")
        self.assertEqual(self.step(answers=self.answers())["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_changed_answer_cannot_replace_a_committed_decision(self):
        answer = self.answers()
        self.step(interview=self.question_file, answers=answer)
        before = self.resolution()
        changed = json.loads(answer.read_text()); changed["understanding_confirmed"] = False
        changed["correction"] = "Change the scope."
        answer.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "frame-input-conflict"):
            self.step(answers=answer)
        self.assertEqual(self.resolution(), before)

    def test_invalid_or_foreign_answers_do_not_raise_a_gate(self):
        answer = self.answers()
        invalid = json.loads(answer.read_text()); invalid["route_id"] = "rt-foreign"
        answer.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid"):
            self.step(interview=self.question_file, answers=answer)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.resolution()["status"], "not-raised")

    def scope_question(self):
        question = {**self.question, "questions": [{
            "id": "q-scope", "topic": "How much to change",
            "question": "Fix only the approval step, or the questions too?", "kind": "choice",
            "options": [{"label": "Both (recommended)", "means": "Fix both."},
                        {"label": "Approval only", "means": "Leave the questions."}],
            "recommended": 0, "why": "Only you can weigh the wording against the schedule."}]}
        self.question_file.write_text(json.dumps(question))
        return question

    def test_the_persons_native_reply_is_recorded_and_the_next_start_takes_it(self):
        question = self.scope_question()
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
        shown = lambda scope_answer, restatement="예": [
            {"question": "제가 이해한 내용: " + question["understanding"], "options": ["예 (권장)", "아니오(고쳐 말하기)"],
             "answer": restatement},
            {"question": question["questions"][0]["question"], "options": ["Both (recommended)", "Approval only"],
             "answer": scope_answer}]
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, shown("Both", restatement="아니오(고쳐 말하기)")))
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, shown("")))          # nothing picked
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, [{"question": "unrelated", "answer": "x"}]))
        recorded = W.record_native_answer(self.route, self.jobs, shown("Approval only (keep the wording)"))
        answers = json.loads(recorded.read_text())
        self.assertEqual((answers["actor_kind"], answers["understanding_confirmed"]), ("user", True))
        self.assertEqual(answers["answers"]["q-scope"], {"choice": "none", "note": "Approval only (keep the wording)"})
        recorded = W.record_native_answer(self.route, self.jobs, shown("Approval only"))     # the latest reply
        self.assertEqual(json.loads(recorded.read_text())["answers"]["q-scope"], {"choice": 1, "note": ""})
        released = self.step()                                     # a bare resume: no answers file to write
        self.assertEqual(released["state"], "released", released)
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, shown("Both")))     # answered: nothing waits

    def test_a_typed_correction_is_the_restatements_answer(self):
        question = self.scope_question()
        self.step(interview=self.question_file)
        import frame_interview as FI
        interview = json.loads(Path(self.step()["interview_file"]).read_text())
        answers = FI.answers_from_native(interview, [
            {"question": question["understanding"], "options": ["예", "아니오"], "answer": "Only the approval step."},
            {"question": question["questions"][0]["question"], "options": [], "answer": "Both"}])
        self.assertEqual((answers["understanding_confirmed"], answers["correction"]), (False, "Only the approval step."))
        self.assertEqual(FI.validate_answers(interview, answers), [])

    # -- one interview answered over several native calls --------------------------------------------

    def four_questions(self):
        names = ("one", "two", "three", "four")
        question = {**self.question, "questions": [{
            "id": f"q-{name}", "topic": f"Choice {name}", "question": f"Which way for part {name}?", "kind": "choice",
            "options": [{"label": f"Left {name}", "means": "Take the left way."},
                        {"label": f"Right {name}", "means": "Take the right way."}],
            "recommended": 0, "why": "Only you can weigh this part."} for name in names]}
        self.question_file.write_text(json.dumps(question))
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
        return question

    def native_call(self, harness, question, names, *, restatement="예", answers=None):
        """The `asked` rows one real adapter reads from a native call showing these parts."""
        import frame_native_answer as N
        shown = []
        if restatement is not None:
            shown.append({"id": "restate", "question": "제가 이해한 내용: " + question["understanding"],
                          "options": ["예 (권장)", "아니오(고쳐 말하기)"], "answer": restatement})
        for name in names:
            right = (answers or {}).get(name, f"Right {name}")
            shown.append({"id": f"q-{name}", "question": f"Which way for part {name}?",
                          "options": [f"Left {name}", f"Right {name}"], "answer": right})
        tool_questions = [{"id": row["id"], "question": row["question"], "options": [{"label": label} for label in row["options"]]}
                          for row in shown]
        if harness == "claude":
            payload = {"tool_name": "AskUserQuestion", "session_id": "s", "tool_input": {"questions": tool_questions},
                       "tool_response": {"answers": {row["question"]: row["answer"] for row in shown}}}
        elif harness == "codex":
            payload = {"tool_name": "request_user_input", "session_id": "s", "tool_input": {"questions": tool_questions},
                       "tool_response": json.dumps({"answers": {row["id"]: {"answers": [row["answer"]]} for row in shown}})}
        else:
            payload = {"tool": "question", "sessionID": "s", "args": {"questions": tool_questions},
                       "answers": [[row["answer"]] for row in shown]}
        return N.asked_from(harness, payload)[1]

    def reference_answers(self, question):
        """The bytes one call showing everything writes; the file is removed again."""
        recorded = W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("one", "two", "three", "four")))
        data = recorded.read_bytes()
        recorded.unlink()
        return data

    def test_a_split_native_reply_writes_the_same_answers_file_as_one_call(self):
        question = self.four_questions()
        reference = self.reference_answers(question)
        directory = Path(self.resolution()["artifact"]).parent
        native, partial = directory / "answers.native.json", directory / "answers.native.partial.json"
        shapes = {"claude": (("one", "two", "three"), ("four",)), "codex": (("one", "two"), ("three", "four")),
                  "opencode": (("one",), ("two", "three", "four"))}
        for harness, (first, second) in shapes.items():
            with self.subTest(harness=harness):
                kept = W.record_native_answer(self.route, self.jobs, self.native_call(harness, question, first))
                self.assertEqual(kept, partial)
                self.assertFalse(native.exists())
                self.assertEqual(W.native_answer_candidate(
                    self.route, self.jobs, self.native_call(harness, question, second, restatement=None))[0], native)
                kept = W.record_native_answer(self.route, self.jobs, self.native_call(harness, question, second, restatement=None))
                self.assertEqual(kept, native)
                self.assertEqual(native.read_bytes(), reference)
                self.assertFalse(partial.exists())
                native.unlink()

    def test_the_gate_stays_pending_until_the_last_question_is_answered(self):
        question = self.four_questions()
        W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("one", "two", "three")))
        waiting = self.step()
        self.assertEqual((waiting["state"], waiting["required_action"]), ("needs-question", "wait-for-user-answer"))
        self.assertEqual(self.resolution()["status"], "blocked")
        W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("four",), restatement=None))
        released = self.step()
        self.assertEqual(released["state"], "released", released)
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertFalse((Path(released["interview_file"]).parent / "answers.native.partial.json").exists())

    def test_a_later_native_answer_to_the_same_question_wins(self):
        question = self.four_questions()
        W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("one",), answers={"one": "Left one"}))
        kept = W.record_native_answer(self.route, self.jobs, self.native_call(
            "codex", question, ("one", "two", "three", "four"), restatement=None, answers={"one": "Right one"}))
        self.assertEqual(json.loads(kept.read_text())["answers"]["q-one"], {"choice": 1, "note": ""})

    def test_a_partial_from_another_round_or_interview_is_ignored(self):
        question = self.four_questions()
        partial = Path(self.resolution()["artifact"]).parent / "answers.native.partial.json"
        W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("one", "two", "three")))
        saved = json.loads(partial.read_text())
        for key, value in (("interview_sha256", "0" * 64), ("round", saved["round"] + 1),
                           ("route_id", "rt-foreign"), ("schema", "other")):
            with self.subTest(key=key):
                partial.write_text(json.dumps({**saved, key: value}))
                kept = W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("four",), restatement=None))
                self.assertEqual(kept, partial)                     # still incomplete: the stale rows were not used
                self.assertEqual(sorted(json.loads(partial.read_text())["slots"]), ["q-four"])
        partial.write_text(json.dumps({**saved, "interview_sha256": "0" * 64}))
        kept = W.record_native_answer(self.route, self.jobs, self.native_call("claude", question, ("one", "two", "three", "four")))
        self.assertEqual(kept.name, "answers.native.json")           # a complete single call is unaffected

    def test_a_rejected_call_keeps_nothing(self):
        question = self.four_questions()
        partial = Path(self.resolution()["artifact"]).parent / "answers.native.partial.json"
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, self.native_call(
            "claude", question, ("one", "two", "three"), restatement="아니오(고쳐 말하기)")))
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, self.native_call(
            "claude", question, ("one", "two"), answers={"two": ""})))
        self.assertIsNone(W.record_native_answer(self.route, self.jobs, [{"question": "unrelated", "answer": "x"}]))
        self.assertFalse(partial.exists())

    def answers_for_scope(self):
        path = self.base / "scope-answers.json"
        path.write_text(json.dumps({"actor_kind": "user", "understanding_confirmed": True, "correction": "",
                                    "answers": {"q-scope": {"choice": 0, "note": ""}},
                                    "schema": "frame_interview_answers_v1", "route_id": self.route["route_id"], "round": 1}))
        return path

    def test_answers_without_the_envelope_release_the_registered_interview(self):
        """REPORT3 §3-5: the runtime stamps schema/route_id/round on the interview; the
        answers carry only what the person said, and the receipt says that shape."""
        self.scope_question()
        needs = self.step()
        self.assertEqual(needs["state"], "needs-interview")
        self.assertIn('"understanding_confirmed"', needs["next_step"])
        self.assertIn('"choice"', needs["next_step"])
        bare = self.base / "bare.json"
        bare.write_text(json.dumps({"actor_kind": "user", "understanding_confirmed": True, "answers": {"q-scope": {"choice": 1}}}))
        result = self.step(interview=self.question_file, answers=bare)
        self.assertEqual(result["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(self.resolution()["answers"]["answers"]["q-scope"]["choice"], 1)
        self.assertIn("**Approval only** (user's own choice)", Path(result["intent_file"]).read_text())
        self.assertEqual(self.step(answers=bare)["state"], "released")        # a lost reply replays
        self.assertEqual(self.calls, ["gate", "release"])

    def test_a_wrong_envelope_is_still_refused_and_says_what_to_put(self):
        self.scope_question()
        answer = self.base / "wrong.json"
        for key, wrong in (("schema", "other/v1"), ("route_id", "rt-foreign"), ("round", 2)):
            with self.subTest(key=key):
                answer.write_text(json.dumps({key: wrong, "understanding_confirmed": True,
                                              "answers": {"q-scope": {"choice": 0}}}))
                with self.assertRaisesRegex(ValueError, "frame-input-invalid: " + key + ":") as caught:
                    self.step(interview=self.question_file, answers=answer)
                self.assertIn('"understanding_confirmed"' if key == "schema" else "omit " + key, str(caught.exception))
                self.assertEqual(self.calls, [])
                self.assertEqual(self.resolution()["status"], "not-raised")

    def test_a_round_two_answer_without_the_envelope_is_that_rounds(self):
        self.scope_question()
        first = self.step(interview=self.question_file, answers=self.answers_for_scope(), decision="revise")
        question = first["interview_template"]
        question["understanding"] = "Run the two commands and keep both outputs in the report."
        self.question_file.write_text(json.dumps(question))
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
        answer = self.base / "round-two.json"
        answer.write_text(json.dumps({"round": 1, "understanding_confirmed": True, "answers": {"q-scope": {"choice": 0}}}))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid: round:"):
            self.step(answers=answer)
        answer.write_text(json.dumps({"actor_kind": "user", "understanding_confirmed": True, "answers": {"q-scope": {"choice": 0}}}))
        self.assertEqual(self.step(answers=answer)["state"], "released")
        self.assertEqual(self.calls, ["gate", "release", "gate", "release"])

    def test_stop_does_not_render_an_agreed_intent_or_start_anything(self):
        result = self.step(interview=self.question_file,answers=self.answers(),decision="stop")
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse((self.output / "shards/frame/intent.md").exists())
        self.assertEqual(self.step(answers=self.answers(),decision="stop")["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_stop_without_answer_and_plain_resume_preserve_cancellation(self):
        self.step(interview=self.question_file)
        self.assertEqual(self.step(decision="stop")["state"], "cancelled")
        self.assertEqual(self.step()["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_revision_registers_next_round_without_replacing_the_first(self):
        answer=self.answers()
        result=self.step(interview=self.question_file,answers=answer,decision="revise")
        before=Path(result["interview_file"]).read_bytes()
        self.assertEqual(self.step()["state"], "needs-revision")
        question=result["interview_template"]
        self.assertEqual(question["round"],2)
        question["understanding"]="Run the two commands and keep both outputs in the report."
        self.question_file.write_text(json.dumps(question))
        second=self.step(interview=self.question_file)
        self.assertEqual(second["state"], "needs-question")
        self.assertEqual(self.resolution()["epoch"],2)
        response=second["answers_template"];response["actor_kind"]="user";response["understanding_confirmed"]=True
        answer.write_text(json.dumps(response))
        self.assertEqual(self.step(answers=answer)["state"],"released")
        self.assertEqual(Path(result["interview_file"]).read_bytes(),before)
        self.assertEqual(self.calls,["gate","release","gate","release"])

    def test_revision_limit_hands_back_without_an_unusable_next_command(self):
        import frame_interview as FI
        question=self.question
        for number in range(1,FI.MAX_ROUNDS+1):
            self.question_file.write_text(json.dumps(question))
            response=FI.answers_template({**question,"route_id":self.route["route_id"]})
            response["actor_kind"]="user"
            response["understanding_confirmed"]=True
            answer=self.base/"received-answer.json";answer.write_text(json.dumps(response))
            result=self.step(interview=self.question_file,answers=answer,decision="revise")
            question=result.get("interview_template")
        self.assertEqual(result["reason"],"frame-revision-round-limit")
        self.assertIsNone(question)
        self.assertEqual(self.resolution()["status"],"revise")
        self.assertEqual(self.step()["state"],"needs-attention")

    def test_interrupted_input_publication_leaves_no_partial_question_and_replays(self):
        import artifact_receipt
        with mock.patch.object(artifact_receipt.os, "link", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                self.step(interview=self.question_file)
        self.assertFalse((self.output / "shards/frame/round-1/interview.json").exists())
        self.assertEqual(self.resolution()["status"], "not-raised")
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")


GF = load("work_start_group_fixture", W.ROOT / "utilities/artifact_workflow_groups.test.py")


class GroupContextStartTest(GF.fixture.ProducerTestBase):
    """Actual compose/admit/start/producer seams with no live roots or workers."""
    _cycles = GF.WorkflowGroupsTest._cycles
    _proposal = staticmethod(GF.WorkflowGroupsTest._proposal)

    def setUp(self):
        super().setUp()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_ARTIFACT_")}
        patch = mock.patch.dict(os.environ, clean, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.members = self._cycles(2)
        self.campaign = self.members[0]["campaign"]
        plan = GF.W.prepare(self.root, self.campaign, self._proposal(self.members))
        GF.W.apply(self.root, plan)
        self.group = plan["document"]["groups"][0]["group_id"]
        self.context = {"campaign_id": self.campaign, "group_id": self.group}
        self.env = {"AGENT_ARTIFACT_CAMPAIGN_ID": self.campaign,
                    "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": self.group}

    def compose(self, slug="group-followup", **kwargs):
        args = dict(capability="autopilot-code", capability_mode="dev", shape="direct", graph=None,
                    slug=slug, cwd=W.ROOT, artifact_root=self.root, campaign_key="workflow-test",
                    parent_cycle_id=self.members[0]["id"], parent_harness="codex", spec_read="fixture",
                    drift_verdict="within-spec", work_request={"text": "Continue the named goal.",
                                                               "owner_harness": "codex"})
        args.update(kwargs)
        route = GF.fixture.R.compose_route(**args)
        bound = GF.fixture.L.admit_runtime_route(self.root, route)
        return route, Path(bound.route_file)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and p.name != ".admission.lock"}

    def test_fresh_start_and_resume_preserve_explicit_context(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose()
        # Read serialized route as a new caller does, after the source env is gone.
        route = json.loads(path.read_text())
        self.assertEqual(route["work_request"]["workflow_group_context"], self.context)
        GF.fixture.R.verify_route(route, W.ROOT)
        before_env = dict(os.environ)
        first = W.start_work(route, path, self.jobs)
        self.assertEqual(first["state"], "inline")
        self.assertEqual(first["artifact_env"]["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], self.group)
        again = W.start_work(json.loads(path.read_text()), path, self.jobs)
        self.assertEqual(again["artifact_env"], first["artifact_env"])
        self.assertEqual(dict(os.environ), before_env)
        document = GF.W.verify(self.root, self.campaign)
        self.assertEqual(document["groups"], 1)
        declaration = json.loads(GF.W.declaration_path(self.root, self.campaign).read_text())
        self.assertEqual(len(declaration["groups"][0]["members"]), 3)

    def test_fresh_cli_process_starts_and_resumes_without_group_environment(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AGENT_ARTIFACT_", "AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_OWNER_ROUTE_"))}
        env.update(AGENT_HOME=str(W.ROOT), AGENT_DISPATCH_JOBS=str(self.jobs),
                   AGENT_DISPATCH_CALLER_HARNESS="codex")
        command = [sys.executable, str(W.ROOT / "utilities/capability-route.py"),
                   "start", "--route", str(path), "--jobs", str(self.jobs)]
        results = []
        for _ in range(2):
            run = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            result = json.loads(run.stdout)
            self.assertEqual(result["artifact_env"]["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], self.group)
            results.append(result["artifact_env"])
        self.assertEqual(results[0], results[1])

    def test_parent_only_old_request_stays_ungrouped(self):
        route, path = self.compose()
        self.assertNotIn("workflow_group_context", route["work_request"])
        result = W.start_work(route, path, self.jobs)
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", result["artifact_env"])

    def test_other_campaign_ambient_context_is_not_captured(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose(campaign_key="independent-goal", parent_cycle_id=None)
        self.assertNotIn("workflow_group_context", route["work_request"])
        result = W.start_work(route, path, self.jobs)
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", result["artifact_env"])
        self.assertNotEqual(result["artifact_env"]["AGENT_ARTIFACT_CAMPAIGN_ID"], self.campaign)

    def test_saved_context_cannot_override_different_campaign(self):
        route, path = self.compose(campaign_key="independent-goal", parent_cycle_id=None,
            work_request={"text": "task", "owner_harness": "codex", "workflow_group_context": self.context})
        before = self.snapshot()
        with self.assertRaisesRegex(GF.P.ProducerError, "workflow-group-campaign-mismatch"):
            W.start_work(route, path, self.jobs)
        self.assertEqual(self.snapshot(), before)

    def test_current_explicit_conflict_is_rejected_without_writes(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose()
        before = self.snapshot()
        with mock.patch.dict(os.environ, {**self.env, "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": "wgrp_" + "f" * 32}):
            with self.assertRaisesRegex(GF.P.ProducerError, "workflow-group-context-conflict"):
                W.start_work(route, path, self.jobs)
        self.assertEqual(self.snapshot(), before)

    def test_saved_context_is_hash_bound_and_closed_shape(self):
        with mock.patch.dict(os.environ, self.env):
            route, _ = self.compose()
        route["work_request"]["workflow_group_context"]["group_id"] = "wgrp_" + "f" * 32
        with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}):
            with self.assertRaisesRegex(ValueError, "modified route hash"):
                GF.fixture.R.verify_route(route, W.ROOT)
        for context in (None, {}, {**self.context, "guess": True},
                        {**self.context, "group_id": "title-derived"},
                        {**self.context, "campaign_id": "../campaign"}):
            with self.subTest(context=context), self.assertRaisesRegex(ValueError, "work-request-group-context-invalid"):
                W.validate_request({"text": "task", "owner_harness": None, "workflow_group_context": context})

    def test_registered_launch_preparation_uses_same_saved_context(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose(shape="solo", registered_headless_evidence=GF.fixture.registered_headless())
        # The selector invokes this same producer seam before adapter launch.
        result = GF.P.prepare_route_artifact_env(path, start=True, jobs=self.jobs)
        self.assertEqual(result["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], self.group)
        self.assertEqual(GF.P.prepare_route_artifact_env(path, start=False, jobs=self.jobs), result)


class CorrectAnswerContinuationTest(unittest.TestCase):
    """`correct` to an owner that ended BLOCKED continues its route in the same call."""

    @classmethod
    def setUpClass(cls):
        cls.router = load("work_start_correct_router_test", W.ROOT / "utilities/capability-route.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"; self.jobs.touch()
        self.answer = Path(self.tmp.name) / "answer.md"; self.answer.write_text("approved: start the full run\n")
        self.route_path = Path(self.tmp.name) / "route.json"; self.route_path.write_text("{}")

    def correct(self, *, submitted, receipt=None, current="att-owner", blocked_answers=()):
        calls = []
        def start_work(route, path, jobs, **kwargs):
            calls.append((path, jobs))
            return receipt
        argv = ["capability-route.py", "correct", "--jobs", str(self.jobs), "--attempt-id", "att-owner",
                "--message-file", str(self.answer)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                mock.patch.object(self.router, "_current_owner_attempt", return_value=current), \
                mock.patch("dispatch_owner_input.submit", return_value=submitted) as submit, \
                mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=list(blocked_answers)), \
                mock.patch.object(W, "start_work", side_effect=start_work), \
                mock.patch.object(W, "_rows", return_value={current: ("done", {"note": "dead-worker-blocked"})}), \
                mock.patch("dispatch_replacement._route", return_value=(self.route_path, {})), \
                mock.patch.object(self.router, "verify_route", side_effect=lambda route, *a, **k: route), \
                mock.patch.object(self.router, "_record_route_chain"):
            code = self.router.main()
        return code, json.loads(out.getvalue()), calls, submit

    def test_the_parent_s_answer_continues_the_route_and_returns_the_start_receipt(self):
        receipt = {"state": "running", "owner_attempt_id": "att-replacement", "parent_next": "end-turn"}
        code, result, calls, _ = self.correct(submitted={"retained": True, "request_id": "input-1"}, receipt=receipt)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(self.route_path, self.jobs)])
        self.assertEqual((result["state"], result["parent_next"], result["owner_attempt_id"]),
                         ("running", "end-turn", "att-replacement"))
        self.assertEqual(result["correction"], {"retained": True, "request_id": "input-1"})

    def test_another_session_s_answer_is_kept_and_its_parent_is_told(self):
        # `correct` returns the common start receipt; it does not publish a second notice.
        receipt = {"state": "needs-attention", "reason": "replacement-parent-identity-unproven",
                   "parent_notified": True, "parent_notice_reason": "answer-awaiting-parent",
                   "next_step": "The parent has a durable notice; this receipt does not claim a launch."}
        with mock.patch("dispatch_supervision.materialize") as told:
            code, result, calls, _ = self.correct(submitted={"retained": True}, receipt=dict(receipt))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        told.assert_not_called()
        self.assertTrue(result["parent_notified"])
        self.assertIn("durable notice", result["next_step"])
        self.assertTrue(result["correction"]["retained"])
        # When start cannot record its common handover, correct preserves that truth.
        receipt.update(parent_notified=False, next_step="The parent must use the existing route start.")
        code, result, calls, _ = self.correct(submitted={"retained": True}, receipt=dict(receipt))
        self.assertFalse(result["parent_notified"])
        self.assertIn("existing route start", result["next_step"])

    def test_an_answer_to_a_live_owner_is_only_queued(self):
        code, result, calls, _ = self.correct(submitted={"request_id": "input-1", "duplicate": False})
        self.assertEqual((code, calls), (0, []))
        self.assertNotIn("correction", result)

    def test_an_answer_queued_as_the_owner_ended_blocked_still_continues_the_route(self):
        receipt = {"state": "running", "owner_attempt_id": "att-replacement", "parent_next": "end-turn"}
        code, result, calls, _ = self.correct(submitted={"request_id": "input-1", "duplicate": False},
                                              receipt=receipt, blocked_answers=[{"id": "input-1"}])
        self.assertEqual((code, len(calls), result["state"]), (0, 1, "running"))

    def test_an_older_attempt_id_reaches_the_owner_doing_the_work_now(self):
        code, result, calls, submit = self.correct(submitted={"request_id": "input-1"}, current="att-replacement")
        self.assertEqual(submit.call_args.args[1], "att-replacement")
        self.assertEqual(result["redirected_from"], "att-owner")


class NeverStartedReplacementNormalStartTest(unittest.TestCase):
    """Normal route start follows claim lineage while never-started rows remain reservations."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.path = self.root / "route.json"
        self.route = {}
        self.calls = []
        self.ready = False
        self.released = False
        self.statuses = {}
        self.parent = mock.patch.object(W, "default_parent_session_id", return_value="parent")
        self.parent.start()
        self.addCleanup(self.parent.stop)
        import artifact_lifecycle
        import artifact_producer
        import session_tidy
        import dispatch_contract
        import dispatch_owner_input
        import dispatch_replacement
        import route_parent_close

        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.private_home = self.root / "agent-home"
        (self.private_home / "core").mkdir(parents=True)
        (self.private_home / "core" / "CORE.md").write_text("fixture\n")
        # Trust only this resolved worktree through Git's command-scoped config; keep HOME private.
        fixture_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root / "home"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(W.ROOT.resolve()),
            "AGENT_HOME": str(self.private_home),
            "AGENT_ARTIFACT_ROOT": str(self.reports),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "AGENT_CODEX_MANAGED_GATEWAY": "0",
            "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            "CODEX_THREAD_ID": "parent",
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "TMPDIR": str(self.root / "tmp"),
        }
        self.runtime_environment = mock.patch.dict(os.environ, fixture_env, clear=True)
        self.runtime_environment.start()
        self.addCleanup(self.runtime_environment.stop)
        for name in ("home", "state", "runtime", "cache", "tmp"):
            (self.root / name).mkdir(mode=0o700)
        self.join = mock.patch.object(W, "join_selected_attempts", side_effect=self.observe)
        self.join.start()
        self.addCleanup(self.join.stop)
        self.current = mock.patch.object(W, "current_delivery_state", side_effect=self.delivery)
        self.current.start()
        self.addCleanup(self.current.stop)
        self.gate = mock.patch.object(W, "owner_frame_launch_gate", side_effect=self.owner_gate)
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.pair = mock.patch.object(W, "completion_marker_gate")
        self.pair.start()
        self.addCleanup(self.pair.stop)
        self.seat_root = mock.patch.object(session_tidy, "state_root", return_value=self.root / "seat-state")
        self.seat_root.start()
        self.addCleanup(self.seat_root.stop)

        self._original_owner_continuation_processes = route_parent_close.owner_continuation_processes

        def owner_observation(meta, jobs):
            if meta.get("pid"):
                return self._original_owner_continuation_processes(meta, jobs)
            status = W._rows(jobs).get(meta.get("attempt_id"), ("done", {}))[0]
            return SimpleNamespace(state="live" if status in {"open", "running"} else "quiescent",
                                   reason="fixture-observation"), []

        self.process = mock.patch.object(route_parent_close, "owner_continuation_processes",
                                         side_effect=owner_observation)
        self.process.start()
        self.addCleanup(self.process.stop)

        self.route_module = W._route_module()
        self.producer = artifact_producer
        self.owner_input = dispatch_owner_input
        self.replacement = dispatch_replacement
        self.contract = dispatch_contract
        self.process_probe = mock.patch.object(
            dispatch_contract, "attempt_process_quiescence", side_effect=self._fixture_quiescence)
        self.process_probe.start()
        self.addCleanup(self.process_probe.stop)
        self.capacity_probe = mock.patch("dispatch_capacity_evidence.harness_hold", return_value=None)
        self.capacity_probe.start()
        self.addCleanup(self.capacity_probe.stop)
        self._prepare_route(artifact_lifecycle)
        self.launches = []
        self.delivered_prompts = {}
        self.replacement_launches = 0
        self.timeout_launches = set()
        self.close_timed_out = True

    def observe(self, **_kwargs):
        return {"state": "timeout", "children": []}

    def delivery(self, jobs, aid, **_kwargs):
        row = W._rows(jobs).get(aid)
        meta = row[1] if row else {}
        verdict = "BLOCKED" if meta.get("failure_class") == "blocked" else "PASS"
        values = dict(marker={"artifact": str(self.root / "result.md")},
                      marker_digest="sha256:fixture", row_revision="1", row_digest="sha256:fixture",
                      status=row[0] if row else "done", verdict=verdict,
                      quiescent=bool(row and row[0] == "done"), owned_children=0,
                      advanced=False, completion_proven=True)
        values.update(self.statuses.get(aid, {}))
        return CurrentDeliveryState(**values)

    def owner_gate(self, *_args, **_kwargs):
        if not self.released:
            raise W.DispatchContractError("human-gate-unreleased", "fixture-gate")

    @staticmethod
    def _fixture_quiescence(_metadata, **_kwargs):
        return SimpleNamespace(state="quiescent", reason="fixture-process-group-empty")

    def _prepare_route(self, artifact_lifecycle):
        route_module = self.route_module
        gate = {"spec_read": {"satisfied": True, "source": "fixture"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "fixture"}}
        evidence = {"tuples": [{
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": route_module.WRAPPER_PARENT_SANDBOXES["codex"][0],
            "child_harness": "codex", "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture", "probe_time": "2026-10-18T00:00:00Z", "failure_class": "",
            "checked_worktree": str(route_module.ROOT), "failure_scope": "none",
            "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}
        registered = {"candidates": [{
            "harness": harness, "transport": "headless", "surface": "registered-headless",
            "status": "supported", "probe_source": "fixture", "probe_time": "2026-10-18T00:00:00Z",
        } for harness in ("codex", "claude")]}
        route = route_module.compile_route(
            "autopilot-code", "dev", "quick", route_module.ROOT, self.reports,
            predicates=[], transport=None, tracking="tracked", tracked_gate_evidence=gate,
            registered_headless_evidence=registered, slug="fixture-task")
        route["work_request"] = {"text": "Complete the approved code task.", "owner_harness": "codex"}
        route["route_hash"] = route_module.route_hash(route)
        route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
        route_module.verify_route(route)
        self.producer.activate(
            self.reports, repository_id="repo_" + "a" * 32,
            artifact_root_id="root_" + "b" * 32,
            w7={"campaign_id": "camp_" + "c" * 32})
        binding = artifact_lifecycle.admit_runtime_route(self.reports, route)
        self.path = Path(binding.route_file)
        self.route = json.loads(self.path.read_text())
        self.owner_node_id = next(node["id"] for node in self.route["nodes"]
                                  if node.get("worker_type") == "owner")
        self.producer.begin(
            self.reports, route_file=self.path, capability="autopilot-code", intensity="quick",
            jobs=self.root / "no-dispatch-registry.log")

    def _argv(self, aid, parent_sid, parent_harness="codex"):
        return ["--start", "--attempt-id", aid, "--worktree", self.route["cwd"],
                "--route-file", str(self.path), "--route-id", self.route["route_id"],
                "--route-hash", self.route["route_hash"], "--capability", self.route["capability"],
                "--capability-mode", self.route["capability_mode"], "--intensity", "quick",
                "--dispatch-depth", "1", "--worker-type", "owner", "--unit", "_kernel/owner",
                "--route-node", self.owner_node_id,
                "--parent-session-id", parent_sid, "--parent-harness", parent_harness,
                "--prompt-text", self.route["work_request"]["text"]]

    def _metadata(self, aid, parent_sid="parent", parent_harness="codex", task=None, argv=None,
                  *, prior=None, record=None, route_source=None):
        from types import SimpleNamespace
        route = self.route
        task = task or route["work_request"]["text"]
        argv = argv or self._argv(aid, parent_sid, parent_harness)
        args = SimpleNamespace(
            attempt_id=aid, jobs_path=self.jobs, worktree=route["cwd"], route_id=route["route_id"],
            route_hash=route["route_hash"], route_file=str(self.path), route_node=self.owner_node_id,
            parent_session_id=parent_sid, parent_harness=parent_harness, parent_transport="headless",
            parent_sandbox=self.route_module.WRAPPER_PARENT_SANDBOXES[parent_harness][0],
            launch_authority="conductor",
            sandbox="workspace-write", permission_mode="default", capability=route["capability"],
            capability_mode=route["capability_mode"], intensity="quick", dispatch_depth=1,
            worker_type="owner", harness="codex", transport="headless",
            execution_surface="registered-headless", registered_worker=True,
            fallback_hop="same-harness-headless", unit="_kernel/owner",
            replacement_input_argv=argv,
            owner_route_binding=SimpleNamespace(route_id=route["route_id"]))
        metadata = {
            "attempt_id": aid, "attempt_schema_version": "2", "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "owner", "unit": "_kernel/owner", "harness": "codex",
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "route_file": str(self.path), "route_node": self.owner_node_id, "owner_route_id": route["route_id"],
            "owner_route_hash": route["route_hash"], "owner_route_file": str(self.path),
            "parent_sid": parent_sid, "parent_harness": parent_harness,
            "parent_transport": "headless", "capability": route["capability"],
            "capability_mode": route["capability_mode"], "intensity": "quick",
            "supervisor_lease_nonce": "fixture-" + aid,
        }
        if prior:
            metadata.update(automatic_retry_of=prior,
                            replacement_family_id=record["family_id"],
                            replacement_original_attempt_id=prior, replacement_ordinal="1",
                            replacement_claim_digest=self.replacement._digest(record))
        metadata.update(self.contract.parse_registry_metadata(
            self.replacement.seal_launch_input(args, "codex", task)))
        return metadata

    def _register(self, aid, *, parent_sid="parent", parent_harness="codex", prior=None,
                  record=None, source=None, started=True, claimed=True):
        if prior:
            source = source or self.replacement._rows(self.jobs.read_text().splitlines())[prior][1]
            replay = self.replacement.launch_input(self.jobs, prior, source)
            import dispatch_seat_handover
            parent_sid = dispatch_seat_handover.effective_parent(source, self.jobs) or parent_sid
            parent_harness = dispatch_seat_handover.effective_parent_harness(source, self.jobs) or parent_harness
            parent_values = {"parent_session_id": parent_sid, "parent_harness": parent_harness}
            argv = self.replacement._replacement_argv(record, source, replay, parent_values)
            task = self.replacement._replacement_task(record, source, replay)
        else:
            argv = self._argv(aid, parent_sid, parent_harness)
            task = self.route["work_request"]["text"]
        metadata = self._metadata(aid, parent_sid, parent_harness, task, argv,
                                  prior=prior, record=record)
        if prior:
            admitted = self.replacement.admission(self.jobs, self.jobs.read_text().splitlines(), metadata)
            self.assertEqual(admitted, record)
        row = "\t".join(("now", "open", self.route["cwd"], self.route["cwd"], "fixture-owner",
                          ",".join(f"{key}={value}" for key, value in metadata.items())))
        self.assertTrue(self.contract.claim_attempt_row(self.jobs, aid, row, launch=False))
        if claimed:
            self.assertTrue(self.contract.claim_attempt_row(self.jobs, aid, row, launch=True))
        if started:
            self.assertTrue(self.contract.annotate_attempt_row(self.jobs, aid, {"launch_started": "1"}))
            self.owner_input.initialize_owner_input(self.jobs, aid, "codex-active-turn")
        return metadata, task

    def _seed_initial_owner(self):
        aid = "att-owner-A"
        self._register(aid)
        return aid

    def _finish_blocked(self, aid):
        self.owner_input.initialize_owner_input(self.jobs, aid, "codex-active-turn")
        channel = self.owner_input.OwnerInput(self.jobs, aid, "fixture-thread-" + aid,
                                               "codex-active-turn", lambda _event: None)
        self.assertTrue(channel.terminal_boundary())
        self.assertTrue(self.contract.close_attempt_row(
            self.jobs, aid, "dead-worker-blocked", evidence={"failure_class": "blocked"}))
        channel.close()
        state = self.owner_input.inspect(self.jobs, aid)
        self.assertFalse(state["accepting"])
        self.assertFalse(state["supervisor_live"])

    def _store(self, aid, text, request_id):
        receipt = self.owner_input.submit(self.jobs, aid, text, request_id)
        self.assertTrue(receipt["retained"])
        return receipt

    def _native_blocked_owner(self):
        original = self._metadata
        def with_native_carrier(aid, *args, **kwargs):
            metadata = original(aid, *args, **kwargs)
            metadata["parent_completion_delivery"] = "codex-native-queue"
            return metadata
        self._metadata = with_native_carrier
        aid = self._seed_initial_owner()
        self._finish_blocked(aid)
        return aid

    def _ack_native_blocked_result(self, aid):
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import (acknowledged_attempt_receipt, current_attempt_row,
            pending_record_identity, unseal_delivery_receipt)
        row = current_attempt_row(self.jobs, aid)
        metadata = row.metadata
        record = pending.read(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"])
        if record is None:
            receipt = unseal_delivery_receipt(metadata["delivery_receipt_b64"])
            route_id, route_node, parent_attempt = pending_record_identity(metadata, self.jobs)
            record = pending.create(self.jobs.parent,
                recipient_kind=metadata["delivery_recipient_kind"], recipient_key=metadata["parent_sid"],
                delivery_id=metadata["delivery_id"], session_generation=metadata.get("session_generation", ""),
                session_generation_supported=metadata.get("session_generation_supported", "0"),
                attempt_ids=[aid], parent_attempt_id=parent_attempt, route_id=route_id,
                route_node=route_node, receipt=receipt,
                receipt_digest=metadata["delivery_receipt_digest"],
                row_revisions={aid: metadata["delivery_row_revision"]})
        self.assertIsNotNone(record, {key: metadata.get(key) for key in
            ("parent_completion_delivery", "parent_sid", "delivery_intent", "delivery_id",
             "delivery_persistence_refused")})
        if record["state"] == "pending":
            record = pending.claim(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"],
                claim_owner="fixture-parent", lease_seconds=30, jobs=self.jobs)
        if record["state"] != "acked":
            pending.record_send(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"],
                source="fixture-native-prompt", event_id="fixture-parent-turn")
            pending.ack(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"],
                acked_by="codex-native-prompt:" + metadata["parent_sid"])
        receipt = acknowledged_attempt_receipt(self.jobs, aid, recipient_sid=metadata["parent_sid"],
            parent_completion_delivery=metadata["parent_completion_delivery"])
        self.assertIsNotNone(receipt)

    def _controlled_runner(self, command, **_kwargs):
        aid = command[command.index("--attempt-id") + 1]
        prior = command[command.index("--automatic-retry-of") + 1] if "--automatic-retry-of" in command else ""
        self.launches.append({"attempt_id": aid, "prior": prior, "command": command})
        if not prior:
            self._register(aid)
            return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")
        self.replacement_launches += 1
        source = self.replacement._rows(self.jobs.read_text().splitlines())[prior][1]
        binding = self.replacement.source_binding(self.jobs, source)
        record = self.replacement._read(self.replacement._record_path(self.jobs, binding[0]))
        started = self.replacement_launches not in self.timeout_launches
        _metadata, task = self._register(
            aid, parent_sid=source.get("parent_sid", "parent"),
            parent_harness=source.get("parent_harness", "codex"), prior=prior,
            record=record, source=source, started=started, claimed=True)
        if started:
            from types import SimpleNamespace
            instructions = self.replacement.recovery_instructions(SimpleNamespace(
                automatic_retry_of=prior, attempt_id=aid, jobs_path=self.jobs, worker_type="owner"))
            self.delivered_prompts[aid] = task + instructions
        if self.replacement_launches in self.timeout_launches:
            if self.close_timed_out:
                self.assertTrue(self.contract.close_attempt_row(
                    self.jobs, aid, "dead-governor-reservation-transfer",
                    evidence={"failure_class": "contract"}))
                self.assertTrue(self.contract.annotate_attempt_row(
                    self.jobs, aid, {"launch_outcome": "never-launched"}))
            raise subprocess.TimeoutExpired(command, timeout=60, output=b"admission/registry contention")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def _cli_start(self, runner=None):
        import contextlib
        import io

        module = self.route_module
        stdout, stderr = io.StringIO(), io.StringIO()
        defaults = dict(W.start_work.__kwdefaults__ or {})
        W.start_work.__kwdefaults__ = {**defaults, "run": runner or self._controlled_runner}
        try:
            with mock.patch.object(sys, "argv", [str(W.ROOT / "utilities/capability-route.py"),
                    "start", "--route", str(self.path), "--jobs", str(self.jobs)]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = module.main()
        finally:
            W.start_work.__kwdefaults__ = defaults
        self.assertIn(code, (None, 0), stderr.getvalue() + stdout.getvalue())
        records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
        self.assertTrue(records, stderr.getvalue() + stdout.getvalue())
        return records[-1]

    def _append_legacy_race_event(self, **event):
        path = self.root / "legacy-race-events.jsonl"
        raw = (json.dumps(event, sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)

    def _legacy_fenced_runner(self, command, **_kwargs):
        """Launch the fixture payload only through the common registry fence."""
        aid = command[command.index("--attempt-id") + 1]
        prior = command[command.index("--automatic-retry-of") + 1]
        self._append_legacy_race_event(kind="runner-invoked", attempt_id=aid, prior=prior)

        source = self.replacement._rows(self.jobs.read_text().splitlines())[prior][1]
        binding = self.replacement.source_binding(self.jobs, source)
        record = self.replacement._read(self.replacement._record_path(self.jobs, binding[0]))
        replay = self.replacement.launch_input(self.jobs, prior, source)
        import dispatch_seat_handover
        parent_sid = dispatch_seat_handover.effective_parent(source, self.jobs) or "parent"
        parent_harness = dispatch_seat_handover.effective_parent_harness(source, self.jobs) or "codex"
        argv = self.replacement._replacement_argv(record, source, replay, {
            "parent_session_id": parent_sid, "parent_harness": parent_harness})
        task = self.replacement._replacement_task(record, source, replay)
        metadata = self._metadata(aid, parent_sid, parent_harness, task, argv,
                                  prior=prior, record=record)
        row = "\t".join(("now", "open", self.route["cwd"], self.route["cwd"], "fixture-owner",
                          ",".join(f"{key}={value}" for key, value in metadata.items())))

        if not self.contract.claim_attempt_row(self.jobs, aid, row, launch=False):
            existing = self.replacement._rows(self.jobs.read_text().splitlines()).get(aid)
            self.assertIsNotNone(existing, "duplicate caller must find the stable registered D row")
            self.assertEqual(existing[1].get("automatic_retry_of"), prior)
            self.assertEqual(existing[1].get("replacement_claim_digest"),
                             self.replacement._digest(record))
            self._append_legacy_race_event(kind="registered-row-reused", attempt_id=aid,
                                           prior=prior)
            return subprocess.CompletedProcess(
                command, 0, "registered=1 started=1 child_spawned=0\n", "")

        from types import SimpleNamespace
        instructions = self.replacement.recovery_instructions(SimpleNamespace(
            automatic_retry_of=prior, attempt_id=aid, jobs_path=self.jobs, worker_type="owner"))
        prompt = task + instructions
        prompt_path = self.root / f"{aid}.prompt.txt"
        received_path = self.root / f"{aid}.payload-input.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        launch_fence = W.ROOT / "utilities/launch-fence.py"
        payload_code = (
            "from pathlib import Path; import sys, time; "
            "Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes()); time.sleep(30)"
        )

        def spawn(gate_fd):
            return subprocess.Popen(
                [sys.executable, str(launch_fence), "--parent-pid", str(os.getpid()),
                 "--gate-fd", str(gate_fd), "--", sys.executable, "-c", payload_code,
                 str(prompt_path), str(received_path)],
                pass_fds=(gate_fd,), start_new_session=True, env=os.environ.copy())

        proc, identity = self.contract.spawn_claimed_attempt(
            self.jobs, aid, parent_binding=None, spawn=spawn)
        self._legacy_race_children.append(proc)
        self.assertTrue(self.contract.annotate_attempt_row(
            self.jobs, aid, {"launch_started": "1"}))
        self.owner_input.initialize_owner_input(self.jobs, aid, "codex-active-turn")
        self._append_legacy_race_event(kind="fenced-payload-started", attempt_id=aid,
                                       prior=prior, pid=proc.pid, pid_start=identity["pid_start"])
        return subprocess.CompletedProcess(
            command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def _legacy_race_start_caller(self, barrier, release, results):
        import traceback
        self._legacy_race_children = []
        try:
            barrier.wait(timeout=20)
            receipt = self._cli_start(runner=self._legacy_fenced_runner)
            results.put({"ok": True, "receipt": receipt})
        except BaseException:
            results.put({"ok": False, "error": traceback.format_exc()})
        finally:
            release.wait(timeout=30)
            for proc in self._legacy_race_children:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                self._append_legacy_race_event(kind="payload-cleaned", pid=proc.pid,
                                               returncode=proc.returncode)

    def _cli_correct(self, aid, message):
        import contextlib
        import io

        answer = self.root / "corrected-answer.md"
        answer.write_text(message)
        module = self.route_module
        stdout, stderr = io.StringIO(), io.StringIO()
        defaults = dict(W.start_work.__kwdefaults__ or {})
        W.start_work.__kwdefaults__ = {**defaults, "run": self._controlled_runner}
        try:
            with mock.patch.object(sys, "argv", [str(W.ROOT / "utilities/capability-route.py"),
                    "correct", "--attempt-id", aid, "--jobs", str(self.jobs),
                    "--message-file", str(answer)]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = module.main()
        finally:
            W.start_work.__kwdefaults__ = defaults
        self.assertIn(code, (None, 0), stderr.getvalue() + stdout.getvalue())
        records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
        self.assertTrue(records, stderr.getvalue() + stdout.getvalue())
        return records[-1]

    def _handover(self, aid, successor="successor"):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        meta = self.replacement._rows(self.jobs.read_text().splitlines())[aid][1]
        seat = tidy.Seat("pane", "fixture-owner-seat", "wY:p5")
        binding = handover.binding_of(self.jobs, meta)
        snapshot = {"schema": handover.SCHEMA,
                    "seat": {"kind": "pane", "key": seat.key, "pane": seat.pane},
                    "from": {"sid": meta["parent_sid"]}, "bindings": [binding]}
        tidy.atomic_write_json(handover._snapshot_path(seat.key), snapshot)
        ledger = tidy._ledger_path(seat)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"event": "handover", "from": meta["parent_sid"],
                                     "sid": successor, "harness": "codex", "ts": 2,
                                     "bindings": [binding]}) + "\n")

    def _build_unstarted_chain(self, *, leave_open=False, correction_count=1,
                               legacy_owner_claim=False):
        owner_a = self._seed_initial_owner()
        self._finish_blocked(owner_a)
        self._store(owner_a, "correction for B", "input-a")
        receipt_b = self._cli_start()
        owner_b = receipt_b["owner_attempt_id"]
        self.assertNotEqual(owner_b, owner_a)
        self._finish_blocked(owner_b)
        self._store(owner_b, "correction for C", "input-b")
        for index in range(2, correction_count + 1):
            self._store(owner_b, f"legacy correction {index}", f"input-b-{index}")
        self.timeout_launches.add(self.replacement_launches + 1)
        self.close_timed_out = not leave_open
        if legacy_owner_claim:
            logical_key = self.replacement._logical_key
            with mock.patch.object(self.replacement, "_logical_key",
                    side_effect=lambda route, meta: {
                        **logical_key(route, meta), "after_capacity": owner_b}):
                receipt_c = self._cli_start()
        else:
            receipt_c = self._cli_start()
        self.assertEqual(receipt_c["reason"], "replacement-launch-timeout", receipt_c)
        owner_c = self.launches[-1]["attempt_id"]
        c_fields, c_meta = self.replacement._rows(self.jobs.read_text().splitlines())[owner_c]
        self.assertEqual(c_meta.get("launch_claimed"), "1")
        self.assertNotEqual(c_meta.get("launch_started"), "1")
        effective, _lineage = self.replacement.effective_attempts(self.jobs, {owner_a})
        self.assertEqual(effective, {owner_b})
        self.assertEqual(self.route_module._current_owner_attempt(self.jobs, owner_a), owner_b)
        if leave_open:
            return owner_a, owner_b, owner_c, receipt_c
        self.assertEqual(c_fields[1], "done")
        self.assertEqual(c_meta.get("launch_outcome"), "never-launched")
        self.assertEqual(c_meta.get("note"), "dead-governor-reservation-transfer")
        return owner_a, owner_b, owner_c, receipt_c

    def _ack_exact_blocked_result(self, attempt):
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import current_attempt_row, materialize_after_terminal_close
        row = current_attempt_row(self.jobs, attempt)
        metadata = row.metadata
        self.assertEqual(metadata.get("parent_completion_delivery"), "codex-native-queue")
        materialize_after_terminal_close(self.jobs, attempt)
        record = pending.read(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"])
        self.assertIsNotNone(record)
        if record["state"] == "pending":
            record = pending.claim(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"],
                                   claim_owner="fixture-parent", lease_seconds=30, jobs=self.jobs)
        if record["state"] != "acked":
            pending.record_send(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"],
                                source="fixture-native-prompt", event_id="fixture-parent-turn")
            record = pending.ack(self.jobs.parent, metadata["parent_sid"], metadata["delivery_id"],
                                 acked_by="codex-native-prompt:" + metadata["parent_sid"])
        self.assertEqual(record["state"], "acked")
        return record

    def test_normal_start_harvests_native_ack_then_starts_one_existing_answer(self):
        owner = self._native_blocked_owner()
        self._ack_native_blocked_result(owner)
        self._store(owner, "use the smaller fixture", "input-native-start")
        result = self._cli_start()
        self.assertIn("owner_attempt_id", result, result)
        replacement = result["owner_attempt_id"]
        self.assertNotEqual(replacement, owner)
        self.assertTrue(result["owner_started"], result)
        self.assertEqual(result["blocked_result_harvest"]["status"], "exact-result-consumed")
        self.assertIn("use the smaller fixture", self.delivered_prompts[replacement])
        launches = len(self.launches)
        replay = self._cli_start()
        self.assertEqual(replay["owner_attempt_id"], replacement)
        self.assertEqual(len(self.launches), launches)

    def test_normal_start_harvests_native_ack_then_reports_real_blocker_without_answer(self):
        owner = self._native_blocked_owner()
        self._ack_native_blocked_result(owner)
        launches = len(self.launches)
        result = self._cli_start()
        self.assertEqual((result["reason"], result["required_action"]),
                         ("owner-blocked", "answer-blocked-owner"), result)
        self.assertEqual(result["blocked_result_harvest"]["status"], "exact-result-consumed")
        self.assertIn("correction_command", result)
        self.assertEqual(len(self.launches), launches)
        self.assertNotIn("dispatch-harvest.py", result.get("next_step", ""))

    def test_normal_start_native_answer_does_not_require_or_forge_transport_ack(self):
        import dispatch_pending_delivery as pending
        owner = self._native_blocked_owner()
        before = self.replacement._rows(self.jobs.read_text().splitlines())[owner][1]
        original = pending.read(self.jobs.parent, before["parent_sid"], before["delivery_id"])
        self.assertNotEqual((original or {}).get("state"), "acked")
        self._store(owner, "continue the approved fixture", "input-native-unacked")
        result = self._cli_start()
        self.assertTrue(result["owner_started"], result)
        self.assertNotEqual(result["owner_attempt_id"], owner)
        after = pending.read(self.jobs.parent, before["parent_sid"], before["delivery_id"])
        self.assertNotEqual((after or {}).get("state"), "acked")
        self.assertIn("continue the approved fixture", self.delivered_prompts[result["owner_attempt_id"]])

    def test_completed_node_changed_without_revise_names_that_action_not_a_retry(self):
        import dispatch_contract as D
        import shlex
        owner = self._native_blocked_owner()
        self._store(owner, "continue after the spec edit", "input-unproven")
        evidence = str(self.root / "spec dir" / "eval-spec.md")
        refusal = self.replacement._completion_unproven(
            {"route_id": "rt-fixture"}, {"id": "eval-spec"}, {"evidence": {"path": evidence}},
            D.GateCurrency("revised-unrecorded", "completion-evidence-revised-unrecorded"))
        before = len(self.launches)
        with mock.patch.object(self.replacement, "_reuse_snapshot", side_effect=refusal):
            result = self._cli_start()
        self.assertEqual(result["reason"], "replacement-completion-unproven", result)
        self.assertEqual(len(self.launches), before)
        self.assertIn("revise --route rt-fixture --node eval-spec "
                      f"--evidence {shlex.quote(evidence)}", result["next_step"])
        self.assertIn("--reason", result["next_step"])
        self.assertNotIn("launch settles", result["next_step"])
        self.assertNotIn("next_action", result)
        attention = result["replacement_attention"][0]
        self.assertEqual((attention["completion_node"], attention["completion_currency"],
                          attention["completion_evidence"]),
                         ("eval-spec", "revised-unrecorded", evidence))

    def test_full_blocked_chain_normal_start_carries_late_correction_and_replays_once(self):
        owner_a, owner_b, owner_c, _ = self._build_unstarted_chain()
        late = self._store(owner_b, "late correction after C reservation", "input-late")
        self.assertTrue(late["retained"])
        self._handover(owner_b)
        W.default_parent_session_id.return_value = "successor"
        os.environ["CODEX_THREAD_ID"] = "successor"

        first = self._cli_start()
        owner_d = first["owner_attempt_id"]
        self.assertNotEqual(owner_d, owner_c)
        self.assertTrue(first["owner_started"], first)
        self.assertEqual(self.launches[-1]["attempt_id"], owner_d)
        prompt = self.delivered_prompts[owner_d]
        for correction in ("correction for C", "late correction after C reservation"):
            self.assertIn(correction, prompt)

        rows = self.replacement._rows(self.jobs.read_text().splitlines())
        self.assertEqual((rows[owner_a][0][1], rows[owner_a][1]["note"], rows[owner_a][1]["failure_class"]),
                         ("done", "dead-worker-blocked", "blocked"))
        self.assertEqual((rows[owner_b][0][1], rows[owner_b][1]["note"], rows[owner_b][1]["failure_class"]),
                         ("done", "dead-worker-blocked", "blocked"))
        self.assertIn(owner_c, rows)
        self.assertEqual(self.replacement.recovery_delivery_state(self.jobs, owner_c), "accepted")
        effective, lineage = self.replacement.effective_attempts(self.jobs, {owner_a})
        self.assertEqual(effective, {owner_d})
        self.assertEqual(lineage[0]["replacement_attempt_id"], owner_d)
        self.assertEqual(self.route_module._current_owner_attempt(self.jobs, owner_a), owner_d)

        source = self.replacement._rows(self.jobs.read_text().splitlines())[owner_c][1]
        binding = self.replacement.source_binding(self.jobs, source)
        record = self.replacement._read(self.replacement._record_path(self.jobs, binding[0]))
        replay_input = self.replacement.launch_input(self.jobs, owner_c, source)
        import dispatch_seat_handover
        parent_sid = dispatch_seat_handover.effective_parent(source, self.jobs)
        parent_harness = dispatch_seat_handover.effective_parent_harness(source, self.jobs)
        alternate = "att-competing-successor"
        argv = self.replacement._replacement_argv(record, source, replay_input, {
            "parent_session_id": parent_sid, "parent_harness": parent_harness})
        task = self.replacement._replacement_task(record, source, replay_input)
        metadata = self._metadata(alternate, parent_sid, parent_harness, task, argv,
                                  prior=owner_c, record=record)
        competing_row = "\t".join(("now", "open", self.route["cwd"], self.route["cwd"], "fixture-owner",
                                   ",".join(f"{key}={value}" for key, value in metadata.items())))
        with self.assertRaises(self.contract.DispatchContractError) as caught:
            self.contract.claim_attempt_row(self.jobs, alternate, competing_row, launch=True)
        self.assertEqual(caught.exception.reason, "automatic-replacement-exhausted")
        self.assertNotIn(alternate, self.replacement._rows(self.jobs.read_text().splitlines()))

        launches_before_replay = len(self.launches)
        replay = self._cli_start()
        self.assertEqual(replay["owner_attempt_id"], owner_d)
        self.assertTrue(replay["owner_started"])
        self.assertEqual(len(self.launches), launches_before_replay)

    def test_confirmed_handover_refuses_live_start_for_old_parent_and_allows_successor(self):
        owner_a = self._seed_initial_owner()
        self._handover(owner_a, "successor")
        launches_before = len(self.launches)
        with mock.patch.object(W, "default_parent_session_id", return_value="parent"):
            refused = self._cli_start()
        self.assertEqual(refused.get("reason"), "work-parent-recovery-required", refused)
        self.assertEqual(len(self.launches), launches_before)
        with mock.patch.object(W, "default_parent_session_id", return_value="successor"):
            admitted = self._cli_start()
        self.assertEqual(admitted.get("owner_attempt_id"), owner_a, admitted)
        self.assertIn(admitted.get("state"), {"running", "preparing"}, admitted)
        self.assertEqual(len(self.launches), launches_before)

    def test_off_chair_start_retains_correction_without_launching(self):
        owner_a, owner_b, owner_c, _ = self._build_unstarted_chain()
        self._store(owner_b, "off-chair retained correction", "input-off-chair")
        self._handover(owner_b)
        W.default_parent_session_id.return_value = "stranger"
        os.environ["CODEX_THREAD_ID"] = "stranger"
        before = len(self.launches)
        receipt = self._cli_start()
        self.assertEqual(receipt["reason"], "replacement-parent-identity-unproven", receipt)
        self.assertEqual(len(self.launches), before)
        self.assertIn(owner_c, self.replacement._rows(self.jobs.read_text().splitlines()))
        self.assertEqual(self.owner_input.retained(self.jobs, owner_b)[-1]["id"], "input-off-chair")

    def test_open_unknown_reservation_does_not_launch_beside_it(self):
        owner_a, owner_b, owner_c, _ = self._build_unstarted_chain(leave_open=True)
        self._handover(owner_b)
        W.default_parent_session_id.return_value = "successor"
        os.environ["CODEX_THREAD_ID"] = "successor"
        before = len(self.launches)
        receipt = self._cli_start()
        self.assertEqual(receipt.get("owner_attempt_id"), owner_c)
        self.assertFalse(receipt.get("owner_started"))
        self.assertEqual(len(self.launches), before)
        self.assertEqual(self.replacement._rows(self.jobs.read_text().splitlines())[owner_c][0][1], "open")
        self.assertIn(receipt["state"], {"running", "preparing"})

    def test_unverified_cleanup_keeps_never_started_reservation_and_blocks_successor(self):
        import route_parent_close

        _owner_a, owner_b, owner_c, _ = self._build_unstarted_chain()
        self._handover(owner_b)
        W.default_parent_session_id.return_value = "successor"
        os.environ["CODEX_THREAD_ID"] = "successor"
        before = len(self.launches)
        with mock.patch.object(route_parent_close, "owner_continuation_processes",
                return_value=(SimpleNamespace(state="unverifiable", reason="fixture-cleanup-pending"), [])):
            result = self._cli_start()
        self.assertEqual(result["reason"], "replacement-process-unverifiable", result)
        self.assertEqual(len(self.launches), before)
        row = self.replacement._rows(self.jobs.read_text().splitlines())[owner_c]
        self.assertEqual((row[0][1], row[1].get("launch_claimed"), row[1].get("launch_outcome")),
                         ("done", "1", "never-launched"))
        self.assertIsNone(self.replacement.source_reservation(self.jobs, owner_c))

    def test_started_blocked_successor_keeps_its_result_and_is_not_retried(self):
        owner_a = self._seed_initial_owner()
        self._finish_blocked(owner_a)
        self._store(owner_a, "approved correction for B", "input-a")
        receipt_b = self._cli_start()
        owner_b = receipt_b["owner_attempt_id"]
        self._finish_blocked(owner_b)
        self._store(owner_b, "approved correction for C", "input-b")
        receipt_c = self._cli_start()
        owner_c = receipt_c["owner_attempt_id"]
        self.assertTrue(receipt_c["owner_started"], receipt_c)
        self._finish_blocked(owner_c)
        before = len(self.launches)
        result = self._cli_start()
        self.assertEqual((result["state"], result["reason"], result["required_action"]),
                         ("needs-attention", "owner-blocked", "answer-blocked-owner"), result)
        self.assertEqual(len(self.launches), before)
        self.assertEqual(self.replacement._rows(self.jobs.read_text().splitlines())[owner_c][1]["note"],
                         "dead-worker-blocked")


class ModernNeverStartedOwnerNormalStartTest(NeverStartedReplacementNormalStartTest):
    """Standard node-less owners retain a launcher PID after exact cleanup."""

    def _prepare_route(self, artifact_lifecycle):
        module = self.route_module

        def tuple_for(child):
            return {
                "parent_harness": "codex", "parent_transport": "headless",
                "parent_sandbox": module.WRAPPER_PARENT_SANDBOXES["codex"][0],
                "child_harness": child, "launch_authority": "conductor", "status": "supported",
                "probe_source": "fixture-probe", "probe_time": "2026-10-18T00:00:00Z",
                "failure_class": "", "checked_worktree": str(module.ROOT.resolve()),
                "failure_scope": "none", "codex_command": "ok" if child == "codex" else "not-applicable",
                "retry_on_isolated_worktree": 0,
            }

        evidence = {
            "tuples": [tuple_for("codex"), tuple_for("claude")],
            "native_subagent": [{
                "harness": "codex", "transport": "headless",
                "execution_surface": "codex-native-subagent", "registered_worker": False,
                "status": "supported", "check_source": "fixture-native-check",
            }],
        }
        route = module.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="staged",
            graph="execute,test,report", slug="fixture-modern-owner", cwd=module.ROOT,
            artifact_root=self.reports, intensity="standard", spec_read="fixture",
            drift_verdict="within-spec", tracking="tracked", artifact_guard="fixture",
            dispatch_evidence=evidence, parent_harness="codex",
            work_request={"text": "Complete the approved code task.", "owner_harness": "codex"},
            unassigned=True,
        )
        module.verify_route(route)
        self.producer.activate(
            self.reports, repository_id="repo_" + "a" * 32,
            artifact_root_id="root_" + "b" * 32,
            w7={"campaign_id": "camp_" + "c" * 32})
        binding = artifact_lifecycle.admit_runtime_route(self.reports, route)
        self.path = Path(binding.route_file)
        self.route = json.loads(self.path.read_text())
        self.owner_node_id = None
        self.producer.begin(
            self.reports, route_file=self.path, capability="autopilot-code", intensity="standard",
            jobs=self.root / "no-dispatch-registry.log")

    def _argv(self, aid, parent_sid, parent_harness="codex"):
        return ["--start", "--attempt-id", aid, "--worktree", self.route["cwd"],
                "--route-evidence", str(self.path), "--slug", "fixture-modern-owner",
                "--capability", self.route["capability"], "--capability-mode", self.route["capability_mode"],
                "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
                "--unit", "_kernel/owner", "--parent-session-id", parent_sid,
                "--parent-harness", parent_harness, "--prompt-text", self.route["work_request"]["text"]]

    def _metadata(self, aid, parent_sid="parent", parent_harness="codex", task=None, argv=None,
                  *, prior=None, record=None, route_source=None):
        from types import SimpleNamespace
        route = self.route
        task = task or route["work_request"]["text"]
        argv = argv or self._argv(aid, parent_sid, parent_harness)
        args = SimpleNamespace(
            attempt_id=aid, attempt_schema_version="2", jobs_path=self.jobs,
            worktree=route["cwd"], route_id=None, route_hash=None, route_file=None, route_node=None,
            parent_session_id=parent_sid, parent_harness=parent_harness, parent_transport="headless",
            parent_sandbox=self.route_module.WRAPPER_PARENT_SANDBOXES[parent_harness][0],
            launch_authority="conductor", sandbox="workspace-write", permission_mode="default",
            capability=route["capability"], capability_mode=route["capability_mode"], intensity="standard",
            dispatch_depth=1, worker_type="owner", harness="codex", transport="headless",
            execution_surface="registered-headless", registered_worker=True,
            fallback_hop="same-harness-headless", unit="_kernel/owner", replacement_input_argv=argv,
            owner_route_binding=SimpleNamespace(route_id=route["route_id"]))
        metadata = {
            "attempt_id": aid, "attempt_schema_version": "2", "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "owner", "unit": "_kernel/owner", "harness": "codex",
            "owner_route_id": route["route_id"], "owner_route_hash": route["route_hash"],
            "owner_route_file": str(self.path), "parent_sid": parent_sid,
            "parent_harness": parent_harness, "parent_transport": "headless",
            "capability": route["capability"], "capability_mode": route["capability_mode"],
            "intensity": "standard", "supervisor_lease_nonce": "fixture-" + aid,
            "parent_completion_delivery": "codex-managed-gateway",
        }
        if prior:
            metadata.update(automatic_retry_of=prior,
                            replacement_family_id=record["family_id"],
                            replacement_original_attempt_id=prior, replacement_ordinal="1",
                            replacement_claim_digest=self.replacement._digest(record))
        metadata.update(self.contract.parse_registry_metadata(
            self.replacement.seal_launch_input(args, "codex", task)))
        return metadata

    def _controlled_runner(self, command, **kwargs):
        if "--automatic-retry-of" in command:
            self.assertEqual(Path(command[1]).name, "dispatch-owner.py")
            self.assertNotIn("--route-node", command)
        close_attempt = self.contract.close_attempt_row

        def close_after_fixture_carrier(jobs, aid, note, *, evidence=None):
            if note == "dead-governor-reservation-transfer":
                carrier = subprocess.Popen(["/bin/sleep", "0.2"], start_new_session=True)
                try:
                    pid_ns = self.contract.process_namespace_identity(carrier.pid)
                    self.assertTrue(self.contract.annotate_attempt_row(jobs, aid, {
                        "pid": str(carrier.pid),
                        "pid_start": str(self.contract.process_start_ticks(carrier.pid)),
                        "pgid": str(carrier.pid), "pid_ns": pid_ns,
                        "pid_observer_ns": self.contract.process_namespace_identity(),
                    }))
                    carrier.wait(timeout=5)
                finally:
                    if carrier.poll() is None:
                        carrier.kill()
                        carrier.wait(timeout=5)
                self.assertTrue(self.contract.annotate_attempt_row(
                    jobs, aid, {"launch_outcome": "never-launched"}))
            closed = close_attempt(jobs, aid, note, evidence=evidence)
            if note == "dead-governor-reservation-transfer":
                c_meta = self.replacement._rows(Path(jobs).read_text().splitlines())[aid][1]
                self.assertTrue(c_meta.get("pid"))
                self.assertTrue(c_meta.get("pid_start"))
                self.assertTrue(c_meta.get("pid_ns"))
                self.assertNotIn("route_file", c_meta)
                self.assertNotIn("route_node", c_meta)
                self.assertTrue(c_meta.get("owner_route_file"))
                self.assertTrue(self.contract._settled_cleanup_receipt(c_meta))
            return closed

        with mock.patch.object(self.contract, "close_attempt_row", side_effect=close_after_fixture_carrier):
            return super()._controlled_runner(command, **kwargs)

    def _build_legacy_claim_with_settled_attention(self):
        owner_a, owner_b, owner_c, _ = self._build_unstarted_chain(
            correction_count=6, legacy_owner_claim=True)
        rows = self.replacement._rows(self.jobs.read_text().splitlines())
        c_fields, c_meta = rows[owner_c]
        binding = self.replacement.source_binding(self.jobs, rows[owner_b][1])
        record = self.replacement._read(self.replacement._record_path(self.jobs, binding[0]))
        self.assertEqual(record["logical_node"], {
            "root_route_id": self.route["route_id"], "node": "__owner__", "after_capacity": owner_b})
        self.assertEqual(record["ordinal"], 1)
        self.assertEqual(record["proof"]["death_kind"], self.replacement.CORRECTED)
        self.assertTrue(self.replacement.never_started(c_fields[1], c_meta))
        self.assertTrue(c_meta.get("pid"))
        self.assertTrue(c_meta.get("pid_start"))
        self.assertTrue(c_meta.get("pid_ns"))
        self.assertTrue(self.contract._settled_cleanup_receipt(c_meta))
        self.replacement._settle_recovery_attention(self.jobs, owner_b, {
            "reason": "replacement-parent-identity-unproven"})
        self.assertEqual(self.replacement.recovery_delivery_state(self.jobs, owner_b), "settled")
        return owner_a, owner_b, owner_c

    def _build_claim_only_operational_shape(self):
        owner_a, owner_b, owner_c, _ = self._build_unstarted_chain(correction_count=1)
        rows = self.replacement._rows(self.jobs.read_text().splitlines())
        logical = self.replacement._logical_key(self.route, rows[owner_c][1])
        logical = {**logical, "after_death": owner_c}
        with mock.patch.object(self.replacement, "_logical_key", return_value=logical), \
             mock.patch.object(self.replacement, "death_kind", return_value="silent"):
            claim_cd = self.replacement.claim(self.jobs, owner_c, resume_capacity=True)
        self.assertEqual(claim_cd["logical_node"], logical)
        self.assertEqual(claim_cd["proof"]["death_kind"], "silent")
        self.assertNotIn("carried_continuation", claim_cd["proof"])
        self.assertEqual(claim_cd["original_attempt_id"], owner_c)
        self.assertNotIn(claim_cd["replacement_attempt_id"],
                         self.replacement._rows(self.jobs.read_text().splitlines()))
        self.replacement._settle_recovery_attention(self.jobs, owner_b, {
            "reason": "replacement-parent-identity-unproven"})
        self.replacement._settle_recovery_attention(self.jobs, owner_c, {
            "reason": "replacement-parent-identity-unproven"})
        rows = self.replacement._rows(self.jobs.read_text().splitlines())
        self.assertEqual([rows[owner_a][0][1], rows[owner_b][0][1]], ["done", "done"])
        self.assertEqual(self.replacement.recovery_delivery_state(self.jobs, owner_b), "settled")
        self.assertEqual(self.replacement.recovery_delivery_state(self.jobs, owner_c), "settled")
        attention_paths = [self.replacement._directory(self.jobs) / "attention/by-source" / (aid + ".json")
                           for aid in (owner_b, owner_c)]
        claim_paths = [self.replacement._record_path(self.jobs, self.replacement.source_reservation(
            self.jobs, aid)["family_id"]) for aid in (owner_b, owner_c)]
        preserved = {path: path.read_bytes() for path in [*attention_paths, *claim_paths]}
        return owner_a, owner_b, owner_c, claim_cd, preserved

    def _assert_legacy_owner_started_with_all_answers(
            self, owner_a, owner_b, owner_c, receipt, *, expected_answers=7):
        self.assertTrue(receipt.get("owner_started"), receipt)
        owner_d = receipt["owner_attempt_id"]
        self.assertNotEqual(owner_d, owner_c)
        self.assertEqual(self.launches[-1]["attempt_id"], owner_d)
        self.assertEqual(self.launches[-1]["prior"], owner_c)
        answers = self.owner_input.retained(self.jobs, owner_b)
        self.assertEqual(len(answers), expected_answers)
        answer_ids = [answer["id"] for answer in answers]
        self.assertEqual(len(answer_ids), len(set(answer_ids)))
        prompt = self.delivered_prompts[owner_d]
        positions = []
        for answer in answers:
            positions.append(prompt.index(answer["text"]))
        self.assertEqual(positions, sorted(positions))
        effective, lineage = self.replacement.effective_attempts(self.jobs, {owner_a})
        self.assertEqual(effective, {owner_d})
        self.assertEqual(lineage[0]["replacement_attempt_id"], owner_d)
        rows = self.replacement._rows(self.jobs.read_text().splitlines())
        for aid in (owner_a, owner_b):
            self.assertEqual((rows[aid][0][1], rows[aid][1].get("note"),
                              rows[aid][1].get("failure_class")),
                             ("done", "dead-worker-blocked", "blocked"))
        self.assertEqual((rows[owner_c][0][1], rows[owner_c][1].get("launch_outcome"),
                          rows[owner_c][1].get("launch_started")),
                         ("done", "never-launched", None))
        next_edge = self.replacement.source_binding(self.jobs, rows[owner_c][1])
        next_claim = self.replacement._read(
            self.replacement._record_path(self.jobs, next_edge[0]))
        self.assertEqual((next_claim["original_attempt_id"], next_claim["replacement_attempt_id"]),
                         (owner_c, owner_d))
        return owner_d

    def test_legacy_settled_owner_normal_start_reuses_one_launch_and_carries_seven_answers(self):
        owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        self._store(owner_b, "legacy correction 7", "input-b-7")
        attention_path = self.replacement._directory(self.jobs) / "attention/by-source" / (owner_b + ".json")
        source = self.replacement._rows(self.jobs.read_text().splitlines())[owner_b][1]
        binding = self.replacement.source_binding(self.jobs, source)
        claim_path = self.replacement._record_path(self.jobs, binding[0])
        attention_before, claim_before = attention_path.read_bytes(), claim_path.read_bytes()

        first = self._cli_start()
        owner_d = self._assert_legacy_owner_started_with_all_answers(owner_a, owner_b, owner_c, first)
        launches_after_first = len(self.launches)
        replay = self._cli_start()
        self.assertEqual(replay["owner_attempt_id"], owner_d)
        self.assertTrue(replay["owner_started"], replay)
        self.assertEqual(len(self.launches), launches_after_first)
        self.assertEqual(attention_path.read_bytes(), attention_before)
        self.assertEqual(claim_path.read_bytes(), claim_before)

    def test_claim_only_operational_shape_starts_existing_successor_with_all_retained_answers(self):
        owner_a, owner_b, owner_c, claim_cd, preserved = self._build_claim_only_operational_shape()
        for index in range(2, 9):
            self._store(owner_b, f"operational correction {index}", f"input-b-{index}")
        answers = self.owner_input.retained(self.jobs, owner_b)
        self.assertEqual(len(answers), 8)
        claim_bc = self.replacement._read(self.replacement._record_path(
            self.jobs, self.replacement.source_reservation(self.jobs, owner_b)["family_id"]))
        self.assertEqual(len(claim_bc["proof"]["corrections"]), 1)

        receipt = self._cli_start()
        owner_d = self._assert_legacy_owner_started_with_all_answers(
            owner_a, owner_b, owner_c, receipt, expected_answers=8)
        self.assertEqual(owner_d, claim_cd["replacement_attempt_id"])
        self.assertEqual(len([item for item in self.launches if item["attempt_id"] == owner_d]), 1)
        for path, original in preserved.items():
            self.assertEqual(path.read_bytes(), original, str(path))

    def test_silent_legacy_claim_walk_uses_each_predecessor_launch_fence(self):
        _owner_a, owner_b, owner_c, claim_cd, _preserved = self._build_claim_only_operational_shape()
        owner_d = claim_cd["replacement_attempt_id"]
        self._register(owner_d, prior=owner_c, record=claim_cd, started=False, claimed=False)
        self.assertTrue(self.contract.close_attempt_row(
            self.jobs, owner_d, "dead-governor-reservation-transfer",
            evidence={"failure_class": "contract"}))
        self.assertTrue(self.contract.annotate_attempt_row(
            self.jobs, owner_d, {"launch_outcome": "never-launched"}))
        rows = self.replacement._rows(self.jobs.read_text().splitlines())
        self.assertTrue(self.replacement.never_started(rows[owner_d][0][1], rows[owner_d][1]))
        carried = self.replacement._unstarted_replacement_claim(self.jobs, owner_d)
        self.assertEqual((carried["original_attempt_id"], carried["replacement_attempt_id"]),
                         (owner_b, owner_c))

    def test_identity_refusal_hands_exact_edge_to_parent_carrier_and_resumes_once(self):
        import capacity_auto_resume as capacity
        import dispatch_session_sweep as sweep

        owner_a, owner_b, owner_c, claim_cd, preserved = self._build_claim_only_operational_shape()
        for index in range(2, 9):
            self._store(owner_b, f"carrier correction {index}", f"input-b-{index}")

        launches_before = len(self.launches)
        automatic = self.replacement.advance(self.jobs, owner_b)
        self.assertEqual(automatic["state"], "not-applicable")
        self.assertEqual(len(self.launches), launches_before)

        W.default_parent_session_id.return_value = "stranger"
        os.environ["CODEX_THREAD_ID"] = "stranger"
        refused = self._cli_start()
        self.assertEqual(refused["reason"], "replacement-parent-identity-unproven", refused)
        self.assertEqual(len(self.launches), launches_before)
        self.assertTrue(refused["parent_notified"], refused)
        for path, original in preserved.items():
            self.assertEqual(path.read_bytes(), original, str(path))

        W.default_parent_session_id.return_value = "parent"
        os.environ["CODEX_THREAD_ID"] = "parent"
        meta_b = self.replacement._rows(self.jobs.read_text().splitlines())[owner_b][1]
        recipient = meta_b["parent_completion_delivery"]
        spawned = []
        real_arm = capacity.arm
        arm_epoch = capacity._epoch("2026-10-19T00:00:00Z")

        def controlled_arm(result, route_file, jobs, *, environ=None):
            return real_arm(result, route_file, jobs, environ=environ,
                            spawn=lambda argv, **kw: spawned.append((argv, kw["env"])),
                            now=lambda: arm_epoch)

        with mock.patch.object(capacity, "arm", side_effect=controlled_arm):
            claimed, _ = sweep.sweep_deliver(self.jobs.parent, recipient, "parent")
        self.assertEqual(len(claimed), 1)
        continued = claimed[0]["parent_continuation"]
        self.assertEqual((continued["state"], continued["cause"]), ("armed", "replacement"))
        self.assertEqual(len(spawned), 1)
        resume_path = Path(continued["record"])
        resume_record = json.loads(resume_path.read_text())
        self.assertEqual(resume_record["source_attempt_id"], owner_b)
        self.assertEqual(resume_record["cause"], "replacement")

        def controlled_start(argv, **_kwargs):
            self.assertEqual(argv[2:4], ["start", "--route"])
            receipt = self._cli_start()
            return SimpleNamespace(returncode=0, stdout=json.dumps(receipt) + "\n", stderr="")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "parent",
                                          "AGENT_DISPATCH_CALLER_HARNESS": "codex"}, clear=False):
            capacity.run(resume_path, now=lambda: capacity._epoch(resume_record["retry_at"]) + 1,
                         sleep=lambda _seconds: self.fail("replacement start was delayed"),
                         call=controlled_start)
        owner_d = claim_cd["replacement_attempt_id"]
        self.assertEqual(self.launches[-1]["attempt_id"], owner_d)
        self.assertEqual(self.launches[-1]["prior"], owner_c)
        self.assertEqual(len([launch for launch in self.launches if launch["attempt_id"] == owner_d]), 1)
        prompt = self.delivered_prompts[owner_d]
        for answer in self.owner_input.retained(self.jobs, owner_b):
            self.assertIn(answer["text"], prompt)
        for path, original in preserved.items():
            self.assertEqual(path.read_bytes(), original, str(path))

        launches_after_resume = len(self.launches)
        repeated = self._cli_start()
        self.assertEqual(repeated["owner_attempt_id"], owner_d)
        self.assertEqual(len(self.launches), launches_after_resume)

    def test_claim_only_operational_shape_concurrent_parent_starts_share_one_fenced_d(self):
        import multiprocessing

        owner_a, owner_b, owner_c, claim_cd, preserved = self._build_claim_only_operational_shape()
        for index in range(2, 9):
            self._store(owner_b, f"race correction {index}", f"input-b-{index}")
        context = multiprocessing.get_context("fork")
        barrier, release, results = context.Barrier(2), context.Event(), context.Queue()
        callers = [context.Process(target=self._legacy_race_start_caller,
                                   args=(barrier, release, results)) for _ in range(2)]
        try:
            for caller in callers:
                caller.start()
            outcomes = [results.get(timeout=90) for _ in callers]
            self.assertTrue(all(item.get("ok") for item in outcomes), outcomes)
            events = [json.loads(line) for line in
                      (self.root / "legacy-race-events.jsonl").read_text().splitlines()]
            started = [event for event in events if event["kind"] == "fenced-payload-started"]
            self.assertEqual(len(started), 1, events)
            self.assertEqual(started[0]["attempt_id"], claim_cd["replacement_attempt_id"])
            self.assertEqual(started[0]["prior"], owner_c)
            payload_pid = started[0]["pid"]
            os.kill(payload_pid, 0)
            payload_input = self.root / f"{claim_cd['replacement_attempt_id']}.payload-input.txt"
            deadline = time.monotonic() + 5
            while not payload_input.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(payload_input.exists())
            delivered = payload_input.read_text(encoding="utf-8")
            for answer in self.owner_input.retained(self.jobs, owner_b):
                self.assertIn(answer["text"], delivered)
            for path, original in preserved.items():
                self.assertEqual(path.read_bytes(), original, str(path))
        finally:
            release.set()
            for caller in callers:
                if caller.pid is not None:
                    caller.join(timeout=20)
                    if caller.is_alive():
                        caller.terminate()
                        caller.join(timeout=5)
            results.close()
            results.join_thread()
        self.assertTrue(all(caller.exitcode == 0 for caller in callers),
                        [caller.exitcode for caller in callers])

    def test_legacy_settled_owner_concurrent_starts_share_one_fenced_payload_and_seven_answers(self):
        import multiprocessing

        owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        self._store(owner_b, "legacy correction 7", "input-b-7")
        attention_path = self.replacement._directory(self.jobs) / "attention/by-source" / (owner_b + ".json")
        source_b = self.replacement._rows(self.jobs.read_text().splitlines())[owner_b][1]
        binding_b = self.replacement.source_binding(self.jobs, source_b)
        claim_path = self.replacement._record_path(self.jobs, binding_b[0])
        legacy_claim = self.replacement._read(claim_path)
        self.assertEqual((legacy_claim["original_attempt_id"],
                          legacy_claim["replacement_attempt_id"]), (owner_b, owner_c))
        attention_before, claim_before = attention_path.read_bytes(), claim_path.read_bytes()

        context = multiprocessing.get_context("fork")
        barrier, release, results = context.Barrier(2), context.Event(), context.Queue()
        callers = [context.Process(target=self._legacy_race_start_caller,
                                   args=(barrier, release, results)) for _ in range(2)]
        outcomes = []
        try:
            for caller in callers:
                caller.start()
            outcomes = [results.get(timeout=90) for _ in callers]
            self.assertTrue(all(item.get("ok") for item in outcomes), outcomes)
            receipts = [item["receipt"] for item in outcomes]
            events = [json.loads(line) for line in (self.root / "legacy-race-events.jsonl").read_text().splitlines()]
            starts = [event for event in events if event["kind"] == "fenced-payload-started"]
            self.assertEqual(len(starts), 1, events)
            owner_d = starts[0]["attempt_id"]
            self.assertNotIn(owner_d, {owner_a, owner_b, owner_c})
            payload_pid = starts[0]["pid"]
            os.kill(payload_pid, 0)

            rows = self.replacement._rows(self.jobs.read_text().splitlines())
            self.assertEqual(rows[owner_d][1].get("automatic_retry_of"), owner_c)
            for receipt in receipts:
                referenced = {receipt.get("owner_attempt_id")}
                referenced.update(item.get("replacement_attempt_id")
                                  for item in receipt.get("replacement_lineage", []))
                referenced.update(item.get("record", {}).get("replacement_attempt_id")
                                  for item in receipt.get("replacement_attention", []))
                self.assertIn(owner_d, referenced,
                              {key: receipt.get(key) for key in
                               ("owner_attempt_id", "replacement_lineage", "replacement_attention")})
            self.assertEqual((rows[owner_a][0][1], rows[owner_a][1].get("note"),
                              rows[owner_a][1].get("failure_class")),
                             ("done", "dead-worker-blocked", "blocked"))
            self.assertEqual((rows[owner_b][0][1], rows[owner_b][1].get("note"),
                              rows[owner_b][1].get("failure_class")),
                             ("done", "dead-worker-blocked", "blocked"))
            self.assertEqual((rows[owner_c][0][1], rows[owner_c][1].get("launch_outcome"),
                              rows[owner_c][1].get("launch_started")),
                             ("done", "never-launched", None))
            self.assertEqual((rows[owner_d][1].get("launch_claimed"),
                              rows[owner_d][1].get("launch_started"),
                              rows[owner_d][1].get("pid")), ("1", "1", str(payload_pid)))

            next_edge = self.replacement.source_binding(self.jobs, rows[owner_c][1])
            next_claim = self.replacement._read(self.replacement._record_path(self.jobs, next_edge[0]))
            self.assertEqual((next_claim["original_attempt_id"], next_claim["replacement_attempt_id"]),
                             (owner_c, owner_d))
            effective, lineage = self.replacement.effective_attempts(self.jobs, {owner_a})
            self.assertEqual(effective, {owner_d})
            self.assertEqual(lineage[0]["replacement_attempt_id"], owner_d)
            self.assertEqual(attention_path.read_bytes(), attention_before)
            self.assertEqual(claim_path.read_bytes(), claim_before)

            answers = self.owner_input.retained(self.jobs, owner_b)
            self.assertEqual(len(answers), 7)
            payload_input = self.root / f"{owner_d}.payload-input.txt"
            deadline = time.monotonic() + 5
            while not payload_input.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(payload_input.exists(), "the released fixture payload must read D's prompt")
            delivered = payload_input.read_text(encoding="utf-8")
            self.assertEqual(delivered, (self.root / f"{owner_d}.prompt.txt").read_text(encoding="utf-8"))
            for answer in answers:
                self.assertIn(answer["text"], delivered)
        finally:
            release.set()
            for caller in callers:
                if caller.pid is not None:
                    caller.join(timeout=20)
                    if caller.is_alive():
                        caller.terminate()
                        caller.join(timeout=5)
            results.close()
            results.join_thread()

        self.assertTrue(all(caller.exitcode == 0 for caller in callers),
                        [caller.exitcode for caller in callers])
        events = [json.loads(line) for line in (self.root / "legacy-race-events.jsonl").read_text().splitlines()]
        cleaned = [event for event in events if event["kind"] == "payload-cleaned"]
        self.assertEqual(len(cleaned), 1, events)
        self.assertEqual(cleaned[0]["pid"], payload_pid)
        self.assertIsNotNone(cleaned[0]["returncode"])

    def test_legacy_settled_owner_correct_carries_eight_retained_answers(self):
        owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        self._store(owner_b, "legacy correction 7", "input-b-7")
        self.assertEqual(len(self.owner_input.retained(self.jobs, owner_b)), 7)

        corrected = self._cli_correct(owner_b, "legacy correction 8")
        owner_d = self._assert_legacy_owner_started_with_all_answers(
            owner_a, owner_b, owner_c, corrected, expected_answers=8)
        self.assertTrue(corrected["correction"]["retained"])
        self.assertEqual(corrected["owner_attempt_id"], owner_d)

    def test_legacy_settled_owner_same_seat_successor_can_resume(self):
        owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        self._store(owner_b, "legacy correction 7", "input-b-7")
        self._handover(owner_b)
        W.default_parent_session_id.return_value = "successor"
        os.environ["CODEX_THREAD_ID"] = "successor"

        receipt = self._cli_start()
        self._assert_legacy_owner_started_with_all_answers(owner_a, owner_b, owner_c, receipt)

    def test_legacy_settled_owner_correct_starts_with_the_new_answer_in_one_call(self):
        owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        corrected = self._cli_correct(owner_b, "legacy correction 7")
        owner_d = self._assert_legacy_owner_started_with_all_answers(owner_a, owner_b, owner_c, corrected)
        self.assertTrue(corrected["correction"]["retained"])
        self.assertEqual(corrected["owner_attempt_id"], owner_d)

    def test_legacy_settled_owner_still_rejects_a_different_parent(self):
        _owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        self._store(owner_b, "legacy correction 7", "input-b-7")
        W.default_parent_session_id.return_value = "stranger"
        os.environ["CODEX_THREAD_ID"] = "stranger"
        before = len(self.launches)

        receipt = self._cli_start()
        self.assertEqual(receipt["reason"], "replacement-parent-identity-unproven", receipt)
        self.assertEqual(len(self.launches), before)
        self.assertIn(owner_c, self.replacement._rows(self.jobs.read_text().splitlines()))
        self.assertEqual(self.replacement.recovery_delivery_state(self.jobs, owner_b), "settled")

    def test_legacy_settled_owner_does_not_create_a_successor_without_explicit_resume(self):
        _owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        result = self.replacement.advance(self.jobs, owner_b)
        self.assertEqual(result["state"], "not-applicable")
        with self.assertRaises(self.contract.DispatchContractError) as caught:
            self.replacement.claim(self.jobs, owner_c)
        self.assertEqual(caught.exception.reason, "replacement-duty-settled")
        self.assertIsNone(self.replacement.source_reservation(self.jobs, owner_c))

    def test_legacy_settled_owner_requires_the_exact_cleanup_receipt(self):
        _owner_a, owner_b, owner_c = self._build_legacy_claim_with_settled_attention()
        self._store(owner_b, "legacy correction 7", "input-b-7")
        before = len(self.launches)

        with mock.patch.object(self.contract, "_settled_cleanup_receipt", return_value=False):
            receipt = self._cli_start()

        self.assertEqual(len(self.launches), before)
        self.assertIn(owner_c, self.replacement._rows(self.jobs.read_text().splitlines()))
        self.assertEqual(self.replacement.recovery_delivery_state(self.jobs, owner_b), "settled")
        self.assertEqual(receipt.get("owner_attempt_id"), owner_c)
        self.assertFalse(receipt.get("owner_started"), receipt)


class CoupledReviewSelfDispositionTest(unittest.TestCase):
    """Public correct/complete, real replacement authority and strict ship consumer."""

    provenance_blocked = False
    historical_home = None
    baseline_home = None
    record_cli = False

    def setUp(self):
        self.fixtures = load("coupled_review_fixtures", W.ROOT / "utilities/capability_route.test.py")
        self.f = self.fixtures.HistoricalReviewSelfDispositionTest()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.historical_fixture(legacy=True, ship=True, provenance_blocked=self.provenance_blocked,
                                  append_hash=self.provenance_blocked,historical_home=self.historical_home)
        self.cr = self.fixtures.R
        self.d = self.fixtures.D
        self.jobs = self.f.jobs
        import dispatch_replacement as replacement
        import dispatch_owner_input as owner_input
        self.replacement, self.owner_input = replacement, owner_input
        self.owner = self.cr._find_attempt_row_metadata(self.jobs, "att-owner")
        self.owner.update(harness="codex", artifact_root=str(self.f.artifact),
                          capability="autopilot-ship", capability_mode="default", intensity="standard",
                          launch_claimed="1", launch_started="1")
        args = SimpleNamespace(attempt_id="att-owner", jobs_path=self.jobs, worktree=str(self.f.repo),
            capability="autopilot-ship", capability_mode="default", intensity="standard", worker_type="owner",
            dispatch_depth=1, parent_session_id="parent", parent_harness="codex", parent_transport="interactive",
            execution_surface="registered-headless", registered_worker=True, fallback_hop="same-harness-headless",
            replacement_input_argv=["--worker-type", "owner", "--dispatch-depth", "1", "--parent-session-id", "parent",
                                    "--capability", "autopilot-ship", "--capability-mode", "default", "--intensity", "standard"],
            owner_route_binding=SimpleNamespace(route_id=self.f.route["route_id"]))
        self.args = args
        self.owner.update(self.d.parse_registry_metadata(replacement.seal_launch_input(args, "codex", "Continue unchanged deploy")))
        self.write_owner(self.owner, "running")
        owner_input.initialize_owner_input(self.jobs, "att-owner", "app-server-turn")
        self.assertTrue(self.d.close_attempt_row(self.jobs,"att-owner","dead-worker-blocked",
                                                evidence={"failure_class":"blocked"}))
        self.owner=self.cr._find_attempt_row_metadata(self.jobs,"att-owner")
        self.answer = self.f.output / "owner-answer.md"; self.answer.write_text("The installed fix supports exact settled self disposition.\n")
        self.request_path = Path(self.f.prepared["request"])
        self.request = json.loads(self.request_path.read_text()); self.request_bytes = self.request_path.read_bytes()
        self.prepare_security()
        self.native_calls = []
        self.native_children = []

    def write_owner(self, metadata, status):
        lines = [line for line in self.jobs.read_text().splitlines()
                 if self.d.parse_registry_metadata(line.split("\t")[5]).get("attempt_id") != metadata["attempt_id"]]
        lines.append("\t".join(["2026-10-12T00:00:00Z", status, str(self.f.repo), str(self.f.repo), "owner",
                                  ",".join(f"{k}={v}" for k,v in metadata.items() if not k.startswith("_"))]))
        self.jobs.write_text("\n".join(lines)+"\n")

    def prepare_security(self):
        node = next(n for n in self.f.route["nodes"] if n["id"] == "security-review")
        evidence = self.f.output / "security.json"
        evidence.write_text(json.dumps({"verdict":"PASS", "findings":[], "gate_met":True}))
        meta = dict(self.f.meta, attempt_id="att-security", route_node=node["id"], log_file=str(self.f.base/"security.jsonl"))
        # The normal completion writer receives the real settled process
        # identity, rather than a late identity overwrite without a receipt.
        child=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],stdin=subprocess.PIPE,
                               start_new_session=True,env={**os.environ,"AGENT_DISPATCH_ATTEMPT_ID":"att-security"})
        meta.update(self.d.process_launch_identity(child.pid))
        child.communicate(timeout=5)
        with self.jobs.open("a") as handle:
            handle.write("\t".join([self.cr.datetime.now(self.cr.timezone.utc).isoformat(), "running", str(self.f.repo), str(self.f.repo), "security",
                                       ",".join(f"{k}={v}" for k,v in meta.items())])+"\n")
        self.cr.complete_node(self.f.route,node,node["id"],evidence,jobs=self.jobs,attempt_id="att-security")
        self.security_bytes=evidence.read_bytes()

    def cli(self, arguments, env):
        if arguments[0]=="complete":
            result=subprocess.run([sys.executable,str(W.ROOT/"utilities/capability-route.py"),*arguments],
                                  env=env,text=True,capture_output=True)
            if self.record_cli:
                print("fixture_cli",json.dumps({"argv":[sys.executable,str(W.ROOT/"utilities/capability-route.py"),*arguments],
                    "exit":result.returncode,"actor":env.get("AGENT_DISPATCH_ATTEMPT_ID"),
                    "records":[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]}))
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        out=io.StringIO();err=io.StringIO()
        with mock.patch.dict(os.environ,env,clear=True), mock.patch.object(sys,"argv",["capability-route.py",*arguments]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code=self.cr.main()
        if self.record_cli:
            print("fixture_cli",json.dumps({"argv":[sys.executable,str(W.ROOT/"utilities/capability-route.py"),*arguments],
                "exit":code or 0,"records":[json.loads(line) for line in out.getvalue().splitlines() if line.startswith('{')]}))
        self.assertIn(code,(None,0),err.getvalue()+out.getvalue())
        return [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]

    def native_owner_launch(self, command, **kwargs):
        """Fake only native launch; preserve replacement admission and route validation."""
        self.assertEqual(Path(command[1]).name,"dispatch-owner.py")
        self.native_calls.append(command)
        self.launch_env = dict(kwargs.get("env") or {})
        aid=command[command.index("--attempt-id")+1]
        source_id=command[command.index("--automatic-retry-of")+1]
        source=self.replacement._rows(self.jobs.read_text().splitlines())[source_id][1]
        binding=self.replacement.source_binding(self.jobs,source)
        record=self.replacement._read(self.replacement._record_path(self.jobs,binding[0]))
        replay=self.replacement.launch_input(self.jobs,source_id,source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=aid
        args.replacement_input_argv=self.replacement._replacement_argv(record,source,replay)
        meta={k:v for k,v in source.items() if k not in ("note","failure_class","launch_outcome","replacement_input_digest")}
        meta.update(attempt_id=aid,automatic_retry_of=source_id,replacement_original_attempt_id=source_id,
                    replacement_family_id=record["family_id"],replacement_claim_digest=self.replacement._digest(record),
                    replacement_ordinal="1",launch_claimed="1",launch_started="1")
        meta.update(self.d.parse_registry_metadata(self.replacement.seal_launch_input(args,"codex",replay["task"])))
        self.replacement.admission(self.jobs,self.jobs.read_text().splitlines(),meta)
        child=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],stdin=subprocess.PIPE,
                               start_new_session=True,env={**kwargs["env"],"AGENT_DISPATCH_ATTEMPT_ID":aid})
        self.addCleanup(child.communicate,timeout=5)
        self.native_children.append(child)
        meta.update(pid=str(child.pid),pid_start=str(self.d.process_start_ticks(child.pid)),pgid=str(child.pid),
                    pid_ns=self.d.process_namespace_identity(child.pid),
                    pid_observer_ns=self.d.process_namespace_identity(child.pid))
        self.write_owner(meta,"running")
        self.owner_input.initialize_owner_input(self.jobs,aid,"app-server-turn")
        return SimpleNamespace(returncode=0,stdout="started=1\n",stderr="")

    def test_public_correction_replacement_disposition_and_unchanged_strict_ship_consumption(self):
        import ship_parent_deploy
        deploy=next(n for n in self.f.route["nodes"] if n["id"]=="deploy")
        with self.assertRaisesRegex(ValueError,"independent-review-required"):
            ship_parent_deploy.reviews(self.f.route,deploy,self.jobs,self.request)
        env={k:v for k,v in os.environ.items() if not k.startswith(("AGENT_","CODEX_","CLAUDE_","OPENCODE_"))}
        env.update(CODEX_THREAD_ID="parent",AGENT_DISPATCH_JOBS=str(self.jobs),AGENT_ARTIFACT_CHECKPOINT="off")
        defaults=dict(W.start_work.__kwdefaults__)
        W.start_work.__kwdefaults__["run"]=self.native_owner_launch
        self.addCleanup(setattr,W.start_work,"__kwdefaults__",defaults)
        receipt=self.cli(["correct","--jobs",str(self.jobs),"--attempt-id","att-owner","--message-file",str(self.answer)],env)[-1]
        self.assertEqual(receipt["state"],"running",receipt)
        self.assertEqual(receipt["parent_next"],"bounded-wait",receipt)
        self.assertIn("--wait",receipt["parent_next_command"])
        current=self.cr._current_owner_attempt(self.jobs,"att-owner")
        self.assertNotEqual(current,"att-owner")
        self.assertEqual(len(self.native_calls),1)
        complete=["complete","--route",str(self.f.path),"--node",self.f.node["id"],"--jobs",str(self.jobs),
                  "--attempt-id","att-review-self","--reviewer-attempt","att-review-self","--evidence",str(self.f.evidence)]
        env["AGENT_DISPATCH_ATTEMPT_ID"]=current
        marker=self.cli(complete,env)[0]
        self.assertEqual(marker["review_independence"],"independent")
        if self.baseline_home:
            before=(self.f.directory/"release-review.json").read_bytes()
            result=subprocess.run(argv,env={**env,"AGENT_HOME":str(self.baseline_home)},text=True,capture_output=True)
            self.assertEqual(result.returncode,64,result.stderr)
            self.assertIn("review-self-disposition-basis-invalid",result.stderr)
            self.assertEqual((self.f.directory/"release-review.json").read_bytes(),before)
            print("old_reader_cli",json.dumps({"argv":argv,"exit":result.returncode,"stderr":result.stderr.strip(),
                "published_successor_unchanged":True}))
        self.assertEqual(self.cli(complete,env)[0],marker)
        self.assertEqual(marker["round_census"]["verdict_rounds"],3)
        self.assertEqual(marker["round_census"]["verdictless_rounds"],1)
        self.assertEqual(marker["round_census"],self.f.origin["round_census"])
        marker_path=ship_parent_deploy.DC._route_module().completion_dir(self.f.route["route_id"],jobs=self.jobs)/"release-review.json"
        self.assertEqual(json.loads(marker_path.read_text()),marker,str(marker_path))
        self.assertEqual(ship_parent_deploy.exact_attempt_row(self.jobs,"att-review-self").metadata["worker_type"],"review")
        verified_request,_=ship_parent_deploy.load_request(self.request_path)
        proof=ship_parent_deploy.reviews(self.f.route,deploy,self.jobs,verified_request)
        self.assertEqual({p["attempt_id"] for p in proof.values()},{"att-review-self","att-security"})
        for review_id in ("security-review","release-review"):
            current_marker=json.loads((self.f.directory/(review_id+".json")).read_text())
            self.assertEqual(current_marker["round_census"]["verdict_rounds"],3)
            self.assertEqual(current_marker["round_census"]["verdictless_rounds"],1)
        self.assertEqual(self.request_path.read_bytes(),self.request_bytes)
        for path,before in self.f.preserved.items():
            if path!=self.jobs:self.assertEqual(path.read_bytes(),before)
        before={p:p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()}
        for actor in ("att-foreign",""):
            refused={**env,"AGENT_DISPATCH_ATTEMPT_ID":actor,"CODEX_THREAD_ID":"foreign" if actor else ""}
            result=subprocess.run([sys.executable,str(W.ROOT/"utilities/capability-route.py"),*complete],
                                  env=refused,text=True,capture_output=True)
            self.assertEqual(result.returncode,64,result.stderr)
            self.assertIn("actor",result.stderr)
            self.assertEqual(before,{p:p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()})

    def test_foreign_and_unknown_parent_keep_correction_without_replacement(self):
        for parent in ("foreign",""):
            with self.subTest(parent=parent):
                env={k:v for k,v in os.environ.items() if not k.startswith(("AGENT_","CODEX_","CLAUDE_","OPENCODE_"))}
                env.update(CODEX_THREAD_ID=parent,AGENT_DISPATCH_JOBS=str(self.jobs),AGENT_ARTIFACT_CHECKPOINT="off")
                receipt=self.cli(["correct","--jobs",str(self.jobs),"--attempt-id","att-owner","--message-file",str(self.answer)],env)[-1]
                self.assertEqual(receipt["reason"],"replacement-parent-identity-unproven",receipt)
                self.assertTrue(receipt["correction"]["retained"])
                self.assertEqual(self.cr._current_owner_attempt(self.jobs,"att-owner"),"att-owner")
                self.assertEqual(self.native_calls,[])

    def test_real_upstream_revision_supersedes_disposition_without_reviving_review(self):
        self.test_public_correction_replacement_disposition_and_unchanged_strict_ship_consumption()
        node=next(n for n in self.f.route["nodes"] if n["id"]=="release-setup")
        evidence=self.f.output/"setup.md";evidence.write_text("prepared release setup\n")
        axes={"attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],"transport":"headless",
              "execution_surface":"inline","registered_worker":False,"fallback_hop":"inline"}
        if not self.provenance_blocked:
            self.cr.complete_node(self.f.route,node,node["id"],evidence,
                                  attempt_id="att-setup-inline",explicit_attempt_metadata=axes)
        evidence.write_text("changed release setup\n")
        current=self.cr._current_owner_attempt(self.jobs,"att-owner")
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":current}):
            transition=self.cr.publish_revision_locked(self.f.route,node["id"],evidence,basis="owner-correction",
                reason="fixture upstream changed",author_attempt_id=current,jobs=self.jobs)
        self.assertIn("release-review",transition["tombstoned"])
        canonical=self.f.directory/"release-review.json"
        self.assertEqual(self.d.gate_currency(self.f.route,self.f.node,canonical,observe=True).state,"superseded")
        before={p:p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()}
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":current}),self.assertRaises(ValueError):
            self.cr.complete_node(self.f.route,self.f.node,"release-review",self.f.evidence,
                                  jobs=self.jobs,attempt_id="att-review-self",review_claim=self.f.claim)
        self.assertEqual(before,{p:p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()})


class CoupledBlockedReviewDispositionTest(CoupledReviewSelfDispositionTest):
    """Historical BLOCKED/Markdown terminal with separately bound JSON, public CLIs."""

    provenance_blocked = True

    def test_original_owner_blocked_replacement_and_correction_complete_first(self):
        import shlex
        import ship_parent_deploy
        env={k:v for k,v in os.environ.items() if not k.startswith(("AGENT_","CODEX_","CLAUDE_","OPENCODE_"))}
        env.update(CODEX_THREAD_ID="parent",AGENT_DISPATCH_JOBS=str(self.jobs),AGENT_ARTIFACT_CHECKPOINT="off")
        env.update(OWNER_ROUTE_SENTINEL="direct-correct",CODEX_DISPATCH_SANDBOX_FORCE="danger-full-access")
        defaults=dict(W.start_work.__kwdefaults__)
        W.start_work.__kwdefaults__["run"]=self.native_owner_launch
        self.addCleanup(setattr,W.start_work,"__kwdefaults__",defaults)
        correct=["correct","--jobs",str(self.jobs),"--attempt-id","att-owner","--message-file",str(self.answer)]
        for index in range(2):
            receipt=self.cli(correct,env)[-1]
            self.assertEqual(receipt["state"],"running",receipt)
            self.assertEqual(self.launch_env.get("OWNER_ROUTE_SENTINEL"), "direct-correct")
            self.assertEqual(self.launch_env.get("CODEX_DISPATCH_SANDBOX_FORCE"), "danger-full-access")
            self.assertEqual(receipt["parent_next"],"bounded-wait")
            argv=shlex.split(receipt["parent_next_command"])
            self.assertEqual(Path(argv[1]).name,"capability-route.py")
            # Shorten only the isolated fixture's wait window. The returned
            # command, join, process observation and authority remain real.
            with mock.patch.object(W,"START_WINDOW_SECONDS",0.01):
                wait=self.cli(argv[2:],env)[-1]
            self.assertEqual(wait["reason"],"parent-wait-deadline",wait)
            self.assertNotIn("parent_next_command",wait)
            current=self.cr._current_owner_attempt(self.jobs,"att-owner")
            self.assertEqual(len(self.native_calls),index+1)
            if index == 0:
                self.native_children[-1].communicate(timeout=5)
                metadata=self.cr._find_attempt_row_metadata(self.jobs,current)
                report=self.f.output/"owner7b.md";report.write_text("provenance remains blocked\n")
                log=self.f.base/"owner7b.jsonl"
                log.write_text(json.dumps({"type":"item.completed","item":{"type":"agent_message",
                    "text":f"artifact: {report}\nverdict: BLOCKED\nblocker: provenance disposition unavailable"}})+"\n"+
                    json.dumps({"type":"turn.completed"})+"\n")
                metadata.update(note="dead-worker-blocked",failure_class="blocked",log_file=str(log))
                self.write_owner(metadata,"done")
                original_replacement=current
        self.assertNotEqual(current,original_replacement)
        complete=["complete","--route",str(self.f.path),"--node",self.f.node["id"],"--jobs",str(self.jobs),
                  "--attempt-id","att-review-self","--reviewer-attempt","att-review-self","--evidence",str(self.f.evidence)]
        # The current model's first fixture action is ordinary completion;
        # parent wait processing above is the parent's own action.
        owner_env={**env,"AGENT_DISPATCH_ATTEMPT_ID":current}
        marker=self.cli(complete,owner_env)[0]
        self.assertEqual(marker["reviewer_disposition"]["actor"],current)
        self.assertEqual(marker["review_independence"],"independent")
        deploy=next(n for n in self.f.route["nodes"] if n["id"]=="deploy")
        ship_parent_deploy.reviews(self.f.route,deploy,self.jobs,self.request)
        self.assertEqual(self.request_path.read_bytes(),self.request_bytes)
        for path,raw in self.f.preserved.items():
            if path!=self.jobs:self.assertEqual(path.read_bytes(),raw)

    def test_format_only_replacement_waits_then_corrects_through_public_cli(self):
        import dispatch_supervisor_terminal as terminal
        import dispatch_replacement
        env={k:v for k,v in os.environ.items() if not k.startswith(("AGENT_","CODEX_","CLAUDE_","OPENCODE_"))}
        env.update(CODEX_THREAD_ID="parent",AGENT_DISPATCH_JOBS=str(self.jobs),AGENT_ARTIFACT_CHECKPOINT="off",
                   OWNER_ROUTE_SENTINEL="direct-correct",CODEX_DISPATCH_SANDBOX_FORCE="danger-full-access")
        defaults=dict(W.start_work.__kwdefaults__)
        W.start_work.__kwdefaults__["run"]=self.native_owner_launch
        self.addCleanup(setattr,W.start_work,"__kwdefaults__",defaults)

        first=self.cli(["correct","--jobs",str(self.jobs),"--attempt-id","att-owner",
                        "--message-file",str(self.answer)],env)[-1]
        self.assertEqual(first["state"],"running",first)
        self.assertEqual(len(self.native_calls),1)
        self.assertEqual(self.launch_env.get("OWNER_ROUTE_SENTINEL"),"direct-correct")
        self.assertEqual(self.launch_env.get("CODEX_DISPATCH_SANDBOX_FORCE"),"danger-full-access")

        malformed=(f"artifact: {self.f.output/'replacement-report.md'}\n"
                   "verdict: BLOCKED — explanation\nblocker: format-only owner result")
        classification=terminal.classify_codex_result(malformed)
        self.assertEqual(classification.note,"dead-contract")
        report=self.f.output/"replacement-report.md"
        report.write_text("format-only owner result\n")
        log=self.f.base/"format-only-owner.jsonl"
        log.write_text(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":malformed}})+"\n"+
                       json.dumps({"type":"turn.completed"})+"\n")
        first_owner=self.cr._current_owner_attempt(self.jobs,"att-owner")
        self.native_children[-1].communicate(timeout=5)
        metadata=self.cr._find_attempt_row_metadata(self.jobs,first_owner)
        metadata.update(note=classification.note,**classification.evidence(),log_file=str(log))
        self.write_owner(metadata,"done")

        starts=[self.cli(["start","--route",str(self.f.path),"--jobs",str(self.jobs)],env)[-1]
                for _ in range(2)]
        for receipt in starts:
            self.assertEqual((receipt["state"],receipt["reason"],receipt["required_action"]),
                             ("needs-attention","owner-blocked","answer-blocked-owner"),receipt)
            self.assertIn("correction_command",receipt)
        self.assertEqual(len(self.native_calls),1)

        corrected=self.cli(["correct","--jobs",str(self.jobs),"--attempt-id",first_owner,
                            "--message-file",str(self.answer)],env)[-1]
        self.assertEqual(corrected["state"],"running",corrected)
        self.assertEqual(len(self.native_calls),2)
        self.assertEqual(self.launch_env.get("OWNER_ROUTE_SENTINEL"),"direct-correct")
        self.assertEqual(self.launch_env.get("CODEX_DISPATCH_SANDBOX_FORCE"),"danger-full-access")
        current=self.cr._current_owner_attempt(self.jobs,"att-owner")
        source_meta=self.cr._find_attempt_row_metadata(self.jobs,first_owner)
        family,_,_=dispatch_replacement.source_binding(self.jobs,source_meta)
        record=dispatch_replacement._read(dispatch_replacement._record_path(self.jobs,family))
        self.assertEqual(record["proof"]["source_result"],"CONTRACT")
        recovery_args=SimpleNamespace(automatic_retry_of=first_owner,attempt_id=current,
                                      jobs_path=str(self.jobs),worker_type="owner")
        recovery=dispatch_replacement.recovery_instructions(recovery_args)
        self.assertIn(self.answer.read_text().strip(),recovery)
        self.assertEqual(self.cr._find_attempt_row_metadata(self.jobs,first_owner)["note"],"dead-contract")

    def test_actual_parent_only_normal_complete_and_strict_unchanged_32pins(self):
        import ship_parent_deploy
        env={k:v for k,v in os.environ.items() if not k.startswith(("AGENT_","CODEX_","CLAUDE_","OPENCODE_"))}
        env.update(CODEX_THREAD_ID="parent",AGENT_DISPATCH_JOBS=str(self.jobs),AGENT_ARTIFACT_CHECKPOINT="off")
        complete=["complete","--route",str(self.f.path),"--node",self.f.node["id"],"--jobs",str(self.jobs),
                  "--attempt-id","att-review-self","--reviewer-attempt","att-review-self","--evidence",str(self.f.evidence)]
        self.assertNotIn("AGENT_DISPATCH_ATTEMPT_ID",env)
        if self.baseline_home:
            argv=[sys.executable,str(Path(self.baseline_home)/"utilities/capability-route.py"),*complete]
            result=subprocess.run(argv,env={**env,"AGENT_HOME":str(self.baseline_home)},text=True,capture_output=True)
            self.assertEqual(result.returncode,64,result.stderr)
            self.assertIn("review-self-disposition-terminal-unverified",result.stderr)
            print("baseline_cli",json.dumps({"argv":argv,"exit":result.returncode,"stderr":result.stderr.strip()}))
            deploy=next(n for n in self.f.route["nodes"] if n["id"]=="deploy")
            with self.assertRaisesRegex(ValueError,"independent-review-required"):
                ship_parent_deploy.reviews(self.f.route,deploy,self.jobs,self.request)
        marker=self.cli(complete,env)[0]
        self.assertEqual(marker["review_independence"],"independent")
        deploy=next(n for n in self.f.route["nodes"] if n["id"]=="deploy")
        proof=ship_parent_deploy.reviews(self.f.route,deploy,self.jobs,self.request)
        self.assertEqual(set(proof),{"release-review","security-review"})
        self.assertEqual(len(self.request["command"]["inputs"]),32)
        self.assertEqual(self.request_path.read_bytes(),self.request_bytes)
        for path,raw in self.f.preserved.items():
            if path!=self.jobs:self.assertEqual(path.read_bytes(),raw)
        if self.record_cli:
            import hashlib
            print("fixture_preservation",json.dumps({"request_sha256":hashlib.sha256(self.request_bytes).hexdigest(),
                "pins":len(self.request["command"]["inputs"]),"original_bytes_unchanged":True,
                "round_census":marker["round_census"],"terminal":"BLOCKED","strict_reviews":list(proof)}))


class BlockedOwnerAutoHarvestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.attempt = "att-blocked-auto-harvest"
        self.route = {"route_id": "rt-blocked-harvest", "route_hash": "sha256:blocked-harvest"}
        self.metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "1", "worker_type": "owner",
            "attempt_id": self.attempt, "harness": "codex", "owner_route_id": self.route["route_id"],
            "owner_route_hash": self.route["route_hash"], "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "parent_sid": "parent",
            "parent_completion_delivery": "codex-stop-hook", "delivery_intent": "1",
            "supervisor_lease_nonce": "lease-nonce",
            "launch_started": "1", "launch_outcome": "started", "note": "dead-worker-blocked",
            "failure_class": "blocked",
        }
        self.jobs.write_text("t\tdone\t/repo\t/wt\ttask\t" + ",".join(
            f"{key}={value}" for key, value in self.metadata.items()) + "\n")

    def _start(self, stored_answer=False):
        import dispatch_contract
        import dispatch_owner_input
        if stored_answer:
            # Model the retained user answer created before a normal start.
            with mock.patch.object(dispatch_owner_input, "_answers_blocked_owner", return_value=True):
                path = dispatch_owner_input._path(self.jobs, self.attempt)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"schema_version": 1, "attempt_id": self.attempt,
                    "target": dispatch_owner_input._digest({key: self.metadata.get(key, "") for key in (
                        "attempt_id", "harness", "owner_route_id", "owner_route_hash", "route_id",
                        "route_hash", "parent_sid", "supervisor_lease_nonce")}),
                    "requests": [{"id": "input-1", "digest": "answer-digest", "text": "use the smaller batch",
                                  "state": "retained", "events": []}],
                    "thread_id": dispatch_owner_input.PLACEHOLDER_THREAD,
                    "transport": "codex-active-turn", "accepting": True}))
        calls = []

        def exact_harvest(command, **kwargs):
            calls.append(command)
            self.assertIn("--status", command)
            self.assertEqual(command[command.index("--status") + 1], "all")
            self.assertEqual(command[command.index("--attempt-id") + 1], self.attempt)
            fields = self.jobs.read_text().splitlines()[-1].split("\t")
            metadata = dict(item.split("=", 1) for item in fields[5].split(","))
            metadata["parent_completion_harvested"] = "1"
            fields[5] = ",".join(f"{key}={value}" for key, value in sorted(metadata.items()))
            self.jobs.write_text("\t".join(fields) + "\n")
            return subprocess.CompletedProcess(command, 0,
                "parent_completion_receipt=consumed\n", "")

        with mock.patch.object(W.route_authority, "answerable_owner_end", return_value="BLOCKED"):
            result = W._harvest_unread_blocked_owner(self.route, self.jobs, exact_harvest)
        self.assertEqual(result, {"ok": True, "attempt_id": self.attempt,
                                  "status": "exact-result-consumed"})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(W._harvest_unread_blocked_owner(self.route, self.jobs, exact_harvest))
        self.assertEqual(len(calls), 1)
        if stored_answer:
            self.assertEqual(dispatch_owner_input.retained(self.jobs, self.attempt)[0]["id"], "input-1")
        return result

    def test_normal_start_harvests_terminal_blocked_result_without_answer(self):
        self._start(stored_answer=False)

    def test_normal_start_harvest_preserves_a_stored_answer(self):
        self._start(stored_answer=True)


if __name__ == "__main__":
    unittest.main()
