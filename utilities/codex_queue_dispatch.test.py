#!/usr/bin/env python3
"""Tests for selecting and starting exact native-queue completion sidecars."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import codex_queue_dispatch as QUEUE
import codex_queue_delivery as DELIVERY


class QueueDispatchTest(unittest.TestCase):
    def test_endpoint_prefers_legacy_session_appserver_for_existing_managed_panes(self):
        socket = QUEUE.resolve_queue_socket({
            "AGENT_CODEX_MANAGED_CONTROL_SOCKET": "/tmp/session/managed-control.sock",
            "CODEX_HOME": "/unused/home",
        })
        self.assertEqual(socket, Path("/tmp/session/app-server.sock"))

    def test_new_unmanaged_session_uses_native_control_socket(self):
        socket = QUEUE.resolve_queue_socket({"CODEX_HOME": "/tmp/codex-home"})
        self.assertEqual(socket, Path("/tmp/codex-home/app-server-control/app-server-control.sock"))

    def test_source_runtime_home_alias_resolves_daemon_beside_private_cli_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_home = root / "runtime-home"
            private_home = root / "private-profile"
            daemon_root = root / "codex-daemon-uid"
            control = runtime_home / "app-server-control"
            control.mkdir(parents=True)
            daemon_root.mkdir(mode=0o700)
            socket_target = daemon_root / "daemon-socket"
            socket_alias = control / "app-server-control.sock"
            socket_alias.symlink_to(socket_target)

            source_endpoint = QUEUE.resolve_queue_socket({"CODEX_HOME": str(runtime_home)})
            private_endpoint = QUEUE.resolve_queue_socket({"CODEX_HOME": str(private_home)})

            self.assertEqual(source_endpoint, socket_alias)
            self.assertEqual(DELIVERY.resolve_endpoint(source_endpoint), socket_target)
            self.assertEqual(private_endpoint,
                             private_home / "app-server-control" / "app-server-control.sock")
            self.assertNotEqual(private_endpoint, source_endpoint)

    def test_sidecar_uses_exact_thread_and_attempts_without_gateway_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            fake = mock.Mock(pid=4567)
            with mock.patch.object(QUEUE.subprocess, "Popen", return_value=fake) as popen:
                launched = QUEUE.launch_codex_queue_completion_sidecar(
                    jobs=jobs,
                    parent_session_id="0100000e-0000-7000-8000-000000000016",
                    attempt_ids={"att-one", "att-two"},
                    environ={"CODEX_HOME": str(root), "AGENT_CODEX_QUEUE_COMPLETION_TIMEOUT": "60"},
                )
            command = popen.call_args.args[0]
            self.assertIn("--queue-socket", command)
            self.assertIn("--parent-session-id", command)
            self.assertIn("0100000e-0000-7000-8000-000000000016", command)
            self.assertIn("att-one", command)
            self.assertIn("att-two", command)
            self.assertNotIn("--control-socket", command)
            self.assertNotIn("--gateway-epoch", command)
            self.assertEqual(launched.pid, 4567)
            self.assertEqual(launched.log_file.stat().st_mode & 0o777, 0o600)

    def test_rejects_unbounded_attempt_set_and_non_session_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            for thread, attempts in (("not-a-thread", {"att"}),
                                     ("0100000e-0000-7000-8000-000000000016",
                                      {f"att-{i}" for i in range(5)})):
                with self.subTest(thread=thread, count=len(attempts)), \
                     self.assertRaises(QUEUE.ManagedDispatchError):
                    QUEUE.launch_codex_queue_completion_sidecar(
                        jobs=jobs, parent_session_id=thread, attempt_ids=attempts,
                        environ={"CODEX_HOME": str(root)},
                    )


if __name__ == "__main__":
    unittest.main()
