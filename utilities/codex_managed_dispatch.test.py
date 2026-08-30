#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "codex_managed_dispatch", HERE / "codex_managed_dispatch.py"
)
MANAGED = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MANAGED
SPEC.loader.exec_module(MANAGED)


class StatusServer:
    def __init__(
        self,
        path: Path,
        *,
        thread_id: str = "thread-1",
        thread_ancestors: list[str] | None = None,
        sibling_thread_ids: list[str] | None = None,
        witnessed_thread_id: str = "",
        binding_source: str = "initial",
        status_value: str = "ready",
        approval_owner: str = "tui",
        upstream_clients: int = 1,
    ) -> None:
        self.path = path
        self.thread_id = thread_id
        self.thread_ancestors = thread_ancestors or []
        self.sibling_thread_ids = sibling_thread_ids or []
        self.witnessed_thread_id = witnessed_thread_id
        self.binding_source = binding_source
        self.status_value = status_value
        self.approval_owner = approval_owner
        self.upstream_clients = upstream_clients
        self.request: dict[str, object] | None = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        self.listener.listen(1)
        self.worker = threading.Thread(target=self._serve, daemon=True)
        self.worker.start()

    def _serve(self) -> None:
        connection, _ = self.listener.accept()
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        self.request = json.loads(bytes(data).split(b"\n", 1)[0])
        response = {
            "schema_version": 1,
            "status": self.status_value,
            "epoch": 7,
            "thread_id": self.thread_id,
            "thread_ancestors": self.thread_ancestors,
            "active_turn_id": "",
            "approval_owner": self.approval_owner,
            "upstream_clients": self.upstream_clients,
            "witnessed_thread_id": self.witnessed_thread_id,
            "sibling_thread_ids": self.sibling_thread_ids,
            "binding_source": self.binding_source,
            "tui_connected": self.status_value == "ready",
        }
        connection.sendall(
            (json.dumps(response, separators=(",", ":")) + "\n").encode()
        )
        connection.close()

    def close(self) -> None:
        self.listener.close()
        self.worker.join(timeout=2)


class ManagedDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        os.chmod(self.base, 0o700)
        self.control = self.base / "control.sock"

    @staticmethod
    def env(control: Path, thread_id: str = "thread-1") -> dict[str, str]:
        return {
            "AGENT_CODEX_MANAGED_GATEWAY": "1",
            "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "codex",
            "AGENT_CODEX_MANAGED_CONTROL_SOCKET": str(control),
            "CODEX_THREAD_ID": thread_id,
        }

    def test_probe_uses_control_only_and_requires_tui_approval_owner(self) -> None:
        server = StatusServer(self.control)
        self.addCleanup(server.close)
        binding = MANAGED.probe_managed_codex_parent(
            parent_harness="codex",
            parent_session_id="thread-1",
            environ=self.env(self.control),
        )
        self.assertEqual(binding.thread_id, "thread-1")
        self.assertEqual(binding.epoch, 7)
        self.assertEqual(
            server.request, {"schema_version": 1, "op": "status"}
        )

    def test_probe_rejects_group_or_world_accessible_control_socket(self) -> None:
        server = StatusServer(self.control)
        self.addCleanup(server.close)
        os.chmod(self.control, 0o660)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(str(raised.exception), "managed-control-permissions-unsafe")

    def test_probe_rejects_foreign_thread(self) -> None:
        server = StatusServer(self.control, thread_id="thread-foreign")
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(str(raised.exception), "managed-gateway-not-ready")

    def test_probe_accepts_gateway_witnessed_fork_and_resolves_successor(self) -> None:
        server = StatusServer(
            self.control,
            thread_id="thread-2",
            thread_ancestors=["thread-1"],
        )
        self.addCleanup(server.close)
        binding = MANAGED.probe_managed_codex_parent(
            parent_harness="codex",
            parent_session_id="thread-1",
            environ=self.env(self.control),
        )
        self.assertEqual(binding.thread_id, "thread-2")
        self.assertEqual(binding.inherited_thread_id, "thread-1")
        self.assertTrue(binding.thread_advanced)

    def test_probe_rejects_unwitnessed_thread_switch(self) -> None:
        server = StatusServer(
            self.control,
            thread_id="thread-2",
            thread_ancestors=["thread-unrelated"],
        )
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(str(raised.exception), "managed-gateway-not-ready")

    def test_reason_class_expected_thread_not_witnessed(self) -> None:
        server = StatusServer(
            self.control, thread_id="thread-2", thread_ancestors=[]
        )
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(
            raised.exception.reason_class, "expected-thread-not-witnessed"
        )

    def test_reason_class_lineage_mismatch(self) -> None:
        server = StatusServer(
            self.control,
            thread_id="thread-2",
            thread_ancestors=["thread-unrelated"],
            sibling_thread_ids=["thread-1"],
        )
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(raised.exception.reason_class, "lineage-mismatch")

    def test_reason_class_tui_disconnected(self) -> None:
        server = StatusServer(self.control, status_value="disconnected")
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(raised.exception.reason_class, "tui-disconnected")

    def test_reason_class_approval_owner_mismatch(self) -> None:
        server = StatusServer(self.control, approval_owner="user")
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(
            raised.exception.reason_class, "approval-owner-mismatch"
        )

    def test_reason_class_upstream_client_count_invalid(self) -> None:
        server = StatusServer(self.control, upstream_clients=2)
        self.addCleanup(server.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-1",
                environ=self.env(self.control),
            )
        self.assertEqual(
            raised.exception.reason_class, "upstream-client-count-invalid"
        )

    def test_sibling_cannot_obtain_a_bindings_probe(self) -> None:
        # B is witnessed (present in sibling_thread_ids) but never becomes the
        # binding, so B's own probe against A's status fails lineage-mismatch
        # rather than silently succeeding with A's identity. Two independent
        # control sockets stand in for two probes issued against the one
        # live gateway's unchanged status at different moments.
        sibling_control = self.base / "control-sibling.sock"
        server_for_b = StatusServer(
            sibling_control,
            thread_id="thread-A",
            sibling_thread_ids=["thread-B"],
        )
        self.addCleanup(server_for_b.close)
        with self.assertRaises(MANAGED.ManagedDispatchError) as raised:
            MANAGED.probe_managed_codex_parent(
                parent_harness="codex",
                parent_session_id="thread-B",
                environ=self.env(sibling_control, thread_id="thread-B"),
            )
        self.assertEqual(raised.exception.reason_class, "lineage-mismatch")
        server_for_a = StatusServer(self.control, thread_id="thread-A")
        self.addCleanup(server_for_a.close)
        binding = MANAGED.probe_managed_codex_parent(
            parent_harness="codex",
            parent_session_id="thread-A",
            environ=self.env(self.control, thread_id="thread-A"),
        )
        self.assertEqual(binding.thread_id, "thread-A")

    def test_sealed_batch_is_order_independent_and_registry_delivery_exact(self) -> None:
        first = MANAGED.sealed_batch_id("thread-1", {"att-a", "att-b"})
        second = MANAGED.sealed_batch_id("thread-1", {"att-b", "att-a"})
        self.assertEqual(first, second)
        jobs = self.base / "jobs.log"
        jobs.write_text(
            "2026-07-27T00:00:00Z\topen\t/r\t/w\tchild\t"
            "attempt_id=att-a,parent_completion_delivery=codex-managed-gateway\n",
            encoding="utf-8",
        )
        self.assertEqual(
            MANAGED.registered_parent_delivery(jobs, "att-a"),
            MANAGED.MANAGED_PARENT_DELIVERY,
        )

    def test_sidecar_command_carries_exact_session_and_no_child_content(self) -> None:
        jobs = self.base / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        binding = MANAGED.ManagedGatewayBinding(
            self.control, "thread-1", 1
        )
        fake = mock.Mock(pid=4242)
        with mock.patch.object(
            MANAGED.subprocess, "Popen", return_value=fake
        ) as popen:
            launched = MANAGED.launch_managed_completion_sidecar(
                binding=binding,
                jobs=jobs,
                parent_session_id="thread-1",
                attempt_ids={"att-only"},
                environ={"AGENT_CODEX_MANAGED_COMPLETION_TIMEOUT": "60"},
            )
        command = popen.call_args.args[0]
        self.assertIn("--parent-session-id", command)
        self.assertIn("thread-1", command)
        self.assertIn("att-only", command)
        self.assertNotIn("RAW_CHILD", " ".join(command))
        self.assertEqual(launched.pid, 4242)
        self.assertEqual(launched.log_file.stat().st_mode & 0o777, 0o600)

    def test_both_child_wrappers_stamp_codex_parent_adapter_on_register(self) -> None:
        for adapter in ("codex", "claude"):
            with self.subTest(adapter=adapter):
                case = self.base / adapter
                repo = case / "repo"
                state = case / "state"
                reports = case / ".agent_reports"
                repo.mkdir(parents=True)
                state.mkdir()
                reports.mkdir()
                os.chmod(state, 0o700)
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                control = state / "control.sock"
                server = StatusServer(control)
                jobs = case / "jobs.log"
                logs = case / "logs"
                attempt = f"att-managed-{adapter}-fixture"
                command = [
                    sys.executable,
                    str(
                        MANAGED.ROOT
                        / "adapters"
                        / adapter
                        / "bin"
                        / "dispatch-headless.py"
                    ),
                    "--register",
                    "--worktree",
                    str(repo),
                    "--slug",
                    f"managed-{adapter}",
                    "--capability",
                    "autopilot-code",
                    "--capability-mode",
                    "dev",
                    "--intensity",
                    "standard",
                    "--dispatch-depth",
                    "1",
                    "--worker-type",
                    "owner",
                    "--attempt-id",
                    attempt,
                    "--jobs",
                    str(jobs),
                    "--log-dir",
                    str(logs),
                    "--completion-delivery",
                    "poll",
                ]
                if adapter == "codex":
                    command += ["--model", "gpt-5.6-sol", "--reasoning", "low"]
                else:
                    command += ["--model", "sonnet", "--effort", "low"]
                environment = {
                    **os.environ,
                    **self.env(control),
                    "AGENT_HOME": str(MANAGED.ROOT),
                    "AGENT_ARTIFACT_ROOT": str(reports),
                    "AGENT_DISPATCH_CHILD": "0",
                }
                try:
                    result = subprocess.run(
                        command,
                        text=True,
                        capture_output=True,
                        env=environment,
                        timeout=20,
                    )
                finally:
                    server.close()
                self.assertEqual(
                    result.returncode, 0, result.stderr + result.stdout
                )
                self.assertIn(
                    "parent_completion_delivery=codex-managed-gateway",
                    result.stdout,
                )
                registry = jobs.read_text(encoding="utf-8")
                self.assertIn(
                    "parent_completion_delivery=codex-managed-gateway",
                    registry,
                )
                self.assertIn(f"harness={adapter}", registry)
                self.assertIn("parent_sid=thread-1", registry)

    def test_both_child_wrappers_leave_claude_parent_on_claude_adapter(self) -> None:
        for adapter in ("codex", "claude"):
            with self.subTest(adapter=adapter):
                case = self.base / f"claude-parent-{adapter}"
                repo = case / "repo"
                reports = case / ".agent_reports"
                repo.mkdir(parents=True)
                reports.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                jobs = case / "jobs.log"
                attempt = f"att-claude-parent-{adapter}-fixture"
                command = [
                    sys.executable,
                    str(
                        MANAGED.ROOT
                        / "adapters"
                        / adapter
                        / "bin"
                        / "dispatch-headless.py"
                    ),
                    "--register",
                    "--worktree",
                    str(repo),
                    "--slug",
                    f"claude-parent-{adapter}",
                    "--capability",
                    "autopilot-code",
                    "--capability-mode",
                    "dev",
                    "--intensity",
                    "standard",
                    "--dispatch-depth",
                    "1",
                    "--worker-type",
                    "owner",
                    "--parent-harness",
                    "claude",
                    "--parent-session-id",
                    "claude-session-1",
                    "--attempt-id",
                    attempt,
                    "--jobs",
                    str(jobs),
                    "--log-dir",
                    str(case / "logs"),
                    "--completion-delivery",
                    "poll",
                ]
                if adapter == "codex":
                    command += ["--model", "gpt-5.6-sol", "--reasoning", "low"]
                else:
                    command += ["--model", "sonnet", "--effort", "low"]
                inherited = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("AGENT_CODEX_MANAGED")
                    and key not in {"CODEX_THREAD_ID", "CODEX_SESSION_ID"}
                }
                environment = {
                    **inherited,
                    "CLAUDE_CODE_SESSION_ID": "claude-session-1",
                    "AGENT_HOME": str(MANAGED.ROOT),
                    "AGENT_ARTIFACT_ROOT": str(reports),
                    "AGENT_DISPATCH_CHILD": "0",
                }
                result = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    env=environment,
                    timeout=20,
                )
                self.assertEqual(
                    result.returncode, 0, result.stderr + result.stdout
                )
                self.assertIn(
                    "parent_completion_delivery=claude-parent-runtime",
                    result.stdout,
                )
                registry = jobs.read_text(encoding="utf-8")
                self.assertIn(
                    "parent_completion_delivery=claude-parent-runtime",
                    registry,
                )
                self.assertIn(f"harness={adapter}", registry)
                self.assertNotIn("codex-managed-gateway", registry)


if __name__ == "__main__":
    unittest.main()
