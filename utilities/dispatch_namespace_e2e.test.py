#!/usr/bin/env python3
"""Codex-only regression for the transient PID-namespace completion lifecycle."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_completion_join as JOIN  # noqa: E402
from codex_dispatch_terminal import terminal_envelope_observed  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ROUTE = load(ROOT / "utilities" / "capability-route.py", "pidns_route_fixture")
WRAPPER = ROOT / "adapters" / "codex" / "bin" / "dispatch-headless.py"


class CodexNamespaceE2E(unittest.TestCase):
    def enter_pid_namespace(self) -> bool:
        if os.environ.get("HEARTING_PIDNS_CHILD") == "1":
            return False
        bwrap = shutil.which("bwrap")
        required = os.environ.get("HEARTING_REQUIRE_PIDNS") == "1"
        if not bwrap:
            if required:
                self.fail("bubblewrap is required for the PID namespace regression")
            self.skipTest("bubblewrap is unavailable")
        with tempfile.TemporaryDirectory() as dev_dir:
            Path(dev_dir, "null").write_bytes(b"")
            base = [
                bwrap,
                "--die-with-parent",
                "--unshare-pid",
                "--ro-bind", "/", "/",
                "--proc", "/proc",
                "--bind", dev_dir, "/dev",
                "--bind", "/tmp", "/tmp",
            ]
            probe = subprocess.run([*base, "true"], text=True, capture_output=True)
            if probe.returncode:
                if required:
                    self.fail("bubblewrap PID namespace unavailable: " + probe.stderr.strip())
                self.skipTest("bubblewrap PID namespace unavailable: " + probe.stderr.strip())
            env = {
                **os.environ,
                "HEARTING_PIDNS_CHILD": "1",
                "HEARTING_HOST_PID_NS": os.readlink("/proc/self/ns/pid"),
            }
            result = subprocess.run(
                [
                    *base,
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "CodexNamespaceE2E.test_pass_receipt_marker_and_join_are_ordered",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return True

    def test_pass_receipt_marker_and_join_are_ordered(self) -> None:
        if self.enter_pid_namespace():
            return
        host_namespace = os.environ["HEARTING_HOST_PID_NS"]
        inner_namespace = os.readlink("/proc/self/ns/pid")
        self.assertNotEqual(inner_namespace, host_namespace)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                check=True,
            )
            (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

            artifact_root = base / ".agent_reports"
            artifact_root.mkdir()
            evidence = artifact_root / "pass.md"
            evidence.write_text("codex namespace PASS\n", encoding="utf-8")
            agent_home = base / "agent-home"
            (agent_home / "core").mkdir(parents=True)
            (agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
            jobs = base / "jobs.log"
            logs = base / "logs"

            gate = {
                "spec_read": {"satisfied": True, "source": "fixture"},
                "drift_verdict": "within-spec",
                "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "fixture"},
            }
            dispatch = {"tuples": [{
                "parent_harness": "codex",
                "parent_transport": "headless",
                "parent_sandbox": "workspace-write",
                "child_harness": "codex",
                "launch_authority": "conductor",
                "status": "supported",
                "probe_source": "codex-pidns-fixture",
                "probe_time": "2026-08-16T00:00:00Z",
                "failure_class": "",
                "checked_worktree": str(repo.resolve()),
                "failure_scope": "none",
                "codex_command": "ok",
                "retry_on_isolated_worktree": 0,
            }], "native_subagent": []}
            route = ROUTE.compile_route(
                "autopilot-code",
                "dev",
                "standard",
                repo,
                artifact_root,
                signals=["shared-contract"],
                transport="headless",
                tracking="tracked",
                tracked_gate_evidence=gate,
                dispatch_evidence=dispatch,
            )
            route_path = base / "route.json"
            route_path.write_text(json.dumps(route), encoding="utf-8")
            node = next(item for item in route["nodes"] if item["id"] == "plan")
            fixture_env = {
                **os.environ,
                "AGENT_HOME": str(agent_home),
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
            }
            fixture_env.pop("AGENT_DISPATCH_JOBS", None)
            for predecessor in node.get("depends_on", []):
                predecessor_evidence = artifact_root / f"{predecessor}.md"
                predecessor_evidence.write_text("fixture predecessor\n", encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "utilities" / "capability-route.py"),
                        "complete",
                        "--route", str(route_path),
                        "--node", predecessor,
                        "--evidence", str(predecessor_evidence),
                        "--attempt-id", f"att-inline-{predecessor}",
                        "--dispatch-depth", "2",
                        "--transport", "interactive",
                        "--execution-surface", "inline",
                        "--registered-worker", "0",
                        "--fallback-hop", "inline",
                    ],
                    env=fixture_env,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

            parent = subprocess.Popen(["sleep", "60"])
            self.addCleanup(lambda: parent.poll() is None and parent.kill())
            parent_start = (Path("/proc") / str(parent.pid) / "stat").read_text().split()[21]
            jobs.write_text(
                f"2026-08-16T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,harness=codex,"
                "runtime_sandbox=workspace-write,attempt_id=att-parent-pidns,"
                f"pid={parent.pid},pid_start={parent_start}\n",
                encoding="utf-8",
            )

            fakebin = base / "bin"
            fakebin.mkdir()
            fake = fakebin / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "text = 'artifact: %s\\nverdict: PASS\\nblocker: none' % os.environ['FAKE_EVIDENCE']\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}), flush=True)\n"
                "print(json.dumps({'type':'turn.completed'}), flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            wrapper = load(WRAPPER, "codex_pidns_wrapper")
            attempt = "att-codex-pidns-pass"
            argv = [
                "dispatch-headless.py", "--start",
                "--worktree", str(repo),
                "--slug", "codex-pidns-plan",
                "--capability", "autopilot-code",
                "--capability-mode", route["capability_mode"],
                "--worker-mode", node["unit"],
                "--intensity", route["effective_intensity"],
                "--dispatch-depth", "2",
                "--parent", "owner",
                "--worker-role", "code-plan",
                "--owner", "autopilot-code",
                "--jobs", str(jobs),
                "--log-dir", str(logs),
                "--attempt-id", attempt,
                "--parent-harness", "codex",
                "--parent-transport", "headless",
                "--parent-sandbox", "workspace-write",
                "--launch-authority", "conductor",
                "--nested-eligibility", "supported",
                "--eligibility-source", "codex-pidns-fixture",
                "--fallback-ordinal", "1",
                "--route-file", str(route_path),
                "--route-id", route["route_id"],
                "--route-hash", route["route_hash"],
                "--route-node", node["id"],
                "--registry-digest", route["registry_digest"],
                "--write-scope", ";".join(node["write_scope"]),
                "--unit", node["unit"],
                "--model-role", node["role"],
                "--model-profile", node["model_profile"],
                "--foreground-timeout", "10",
                "--prompt-text", "fixture",
            ]
            env = {
                **os.environ,
                "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                "AGENT_HOME": str(agent_home),
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_DISPATCH_CHILD": "1",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-pidns",
                "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1",
                "FAKE_EVIDENCE": str(evidence),
                "XDG_STATE_HOME": str(base / "state"),
            }
            output = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
                stack.enter_context(mock.patch.object(
                    wrapper, "check_runtime_projection", return_value=0
                ))
                stack.enter_context(mock.patch.object(
                    wrapper, "ensure_runtime_home_projection", return_value=None
                ))
                stack.enter_context(mock.patch.object(
                    wrapper,
                    "launch_summary_owner",
                    return_value={"summary_owner": "pidns-fixture"},
                ))
                stack.enter_context(redirect_stdout(output))
                code = wrapper.main(argv)

            self.assertEqual(code, 0, output.getvalue())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                published = JOIN.exact_attempt_row(jobs, attempt)
                if terminal_envelope_observed(published.metadata.get("log_file")):
                    break
                time.sleep(0.005)
            else:
                self.fail("Codex terminal envelope was not published")
            blocked_receipt = JOIN.join_batch(
                jobs=jobs,
                parent_attempt_id="att-parent-pidns",
                interval=0.005,
                timeout=0.03,
            )
            self.assertEqual(blocked_receipt["state"], "timeout", blocked_receipt)
            self.assertEqual(
                blocked_receipt["children"][0]["reason"],
                "process-unverifiable",
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                finished = JOIN.exact_attempt_row(jobs, attempt)
                if finished.status == "done":
                    break
                time.sleep(0.02)
            else:
                self.fail("detached receipt watcher did not close the PASS attempt")
            parent.terminate()
            parent.wait(timeout=5)
            self.assertEqual(finished.status, "done")
            self.assertEqual(finished.metadata.get("note"), "completed-marker")
            self.assertEqual(finished.metadata.get("failure_class"), None)
            self.assertEqual(finished.metadata.get("pid_scope"), "namespace-local")
            self.assertEqual(finished.metadata.get("pid_ns"), inner_namespace)
            self.assertEqual(finished.metadata.get("pid_observer_ns"), inner_namespace)
            self.assertEqual(
                finished.metadata.get("launch_outcome"),
                "governed-process-group-drained",
            )
            self.assertEqual(finished.metadata.get("group_reap_proof"), "pgid-empty-v1")
            self.assertEqual(finished.metadata.get("group_reap_pgid"), finished.metadata.get("pid"))
            self.assertEqual(
                finished.metadata.get("attempt_descendant_proof"),
                "attempt-tagged-empty-v1",
            )
            marker = Path(finished.metadata["completion_marker"])
            self.assertTrue(marker.is_file(), marker)
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(marker_value["attempt_id"], attempt)

            ready = JOIN.join_batch(
                jobs=jobs,
                parent_attempt_id="att-parent-pidns",
                interval=0.01,
                timeout=1,
            )
            self.assertEqual(ready["state"], "ready")
            self.assertEqual(ready["children"][0]["required_action"], "advance-completed")


if __name__ == "__main__":
    unittest.main()
