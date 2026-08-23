#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "utilities" / "codex-managed-completion.py"
PARENT = "att-parent"
SESSION = "thread-managed-parent"


def row(
    attempt_id: str,
    *,
    harness: str,
    status: str = "done",
    parent: str = PARENT,
) -> str:
    return (
        f"2026-07-27T00:00:00Z\t{status}\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        f"attempt_id={attempt_id},parent_attempt_id={parent},"
        f"harness={harness},note=RAW_CHILD_SENTINEL\n"
    )


def session_row(attempt_id: str, *, harness: str) -> str:
    return (
        f"2026-07-27T00:00:00Z\tdone\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "launch_claimed=1,launch_outcome=never-launched,"
        "parent_completion_delivery=codex-managed-gateway,"
        f"attempt_id={attempt_id},parent_sid={SESSION},harness={harness},"
        "note=RAW_SESSION_CHILD_SENTINEL\n"
    )


class ControlServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.request: dict[str, Any] | None = None
        self.called = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        self.request = json.loads(bytes(data).split(b"\n", 1)[0])
        self.called.set()
        connection.sendall(
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "accepted",
                        "delivery_id": "dlv-fixture",
                        "action": "start",
                        "replay": False,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        connection.close()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=2)


class ManagedCompletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.control_path = self.base / "control.sock"
        self.join = self.base / "fake_join.py"
        self.join.write_text(
            """\
import json, sys
mode = sys.argv[1]
if '--parent-session-id' in sys.argv:
    identity_key = 'parent_session_id'
    parent = sys.argv[sys.argv.index('--parent-session-id') + 1]
else:
    identity_key = 'parent_attempt_id'
    parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
state = 'timeout' if mode == 'timeout' else 'ready'
children = [
    {
        'attempt_id': attempt,
        'status': 'open' if mode in {'timeout', 'terminal'} else 'done',
        'readiness': 'ready' if state == 'ready' else 'pending',
        'reason': (
            'terminal-observed' if mode == 'terminal'
            else 'registry-closed' if state == 'ready'
            else 'process-alive'
        ),
        'required_action': (
            'complete-open' if mode in {'timeout', 'terminal'}
            else 'advance-completed'
        ),
        'slug': 'child',
    }
    for attempt in attempts
]
print(json.dumps({
    'schema_version': 2,
    'state': state,
    identity_key: parent,
    'children': children,
}))
raise SystemExit(3 if state == 'timeout' else 0)
""",
            encoding="utf-8",
        )

    def command(
        self,
        attempts: list[str],
        *,
        mode: str = "ready",
        batch: str = "batch-1",
    ) -> list[str]:
        command = [
            sys.executable,
            str(SIDECAR),
            "--control-socket",
            str(self.control_path),
            "--jobs",
            str(self.jobs),
            "--parent-attempt-id",
            PARENT,
            "--sealed-batch-id",
            batch,
            "--interval",
            "0.01",
            "--timeout",
            "0.1",
            "--join-command",
            f"{sys.executable} {self.join} {mode}",
        ]
        for attempt in attempts:
            command += ["--attempt-id", attempt]
        return command

    def session_command(
        self,
        attempts: list[str],
        *,
        retry_window: float = 0.0,
        launch_ready_timeout: float = 1.0,
    ) -> list[str]:
        command = [
            sys.executable,
            str(SIDECAR),
            "--control-socket",
            str(self.control_path),
            "--jobs",
            str(self.jobs),
            "--parent-session-id",
            SESSION,
            "--thread-id",
            SESSION,
            "--sealed-batch-id",
            "batch-session",
            "--interval",
            "0.01",
            "--timeout",
            "0.1",
            "--launch-ready-timeout",
            str(launch_ready_timeout),
            "--delivery-retry-window",
            str(retry_window),
            "--delivery-retry-interval",
            "0.02",
            "--join-command",
            f"{sys.executable} {self.join} ready",
        ]
        for attempt in attempts:
            command += ["--attempt-id", attempt]
        return command

    def test_codex_and_claude_children_share_one_bounded_receipt(self) -> None:
        attempts = ["att-codex", "att-claude"]
        self.jobs.write_text(
            row(attempts[0], harness="codex")
            + row(attempts[1], harness="claude"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.command(attempts),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        children = server.request["receipt"]["children"]
        self.assertEqual(server.request["receipt"]["schema_version"], 2)
        self.assertEqual(server.request["receipt"]["job_registry"], str(self.jobs))
        self.assertEqual(
            {child["harness"] for child in children},
            {"codex", "claude"},
        )
        self.assertEqual(
            {child["required_action"] for child in children},
            {"advance-completed"},
        )
        encoded = json.dumps(server.request)
        self.assertNotIn("RAW_CHILD_SENTINEL", encoded)
        self.assertLessEqual(
            len(
                json.dumps(
                    server.request["receipt"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
            2048,
        )

    def test_symlinked_registry_is_rejected_before_gateway(self) -> None:
        target = self.base / "real-jobs.log"
        target.write_text(row("att-one", harness="codex"), encoding="utf-8")
        self.jobs.symlink_to(target)
        result = subprocess.run(
            self.command(["att-one"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("jobs-path-invalid", result.stdout)
        self.assertFalse(self.control_path.exists())

    def test_timeout_never_connects_to_gateway(self) -> None:
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(
            row(attempts[0], harness="codex", status="open")
            + row(attempts[1], harness="claude", status="open"),
            encoding="utf-8",
        )
        result = subprocess.run(
            self.command(attempts, mode="timeout"),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "timeout")
        self.assertFalse(self.control_path.exists())

    def test_terminal_observed_open_child_keeps_actionable_status(self) -> None:
        self.jobs.write_text(
            row("att-terminal", harness="codex", status="open"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.command(["att-terminal"], mode="terminal"),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        child = server.request["receipt"]["children"][0]
        self.assertEqual(child["status"], "open")
        self.assertEqual(child["reason"], "terminal-observed")
        self.assertEqual(child["required_action"], "complete-open")

    def test_foreign_or_missing_attempt_fails_before_gateway(self) -> None:
        self.jobs.write_text(
            row("att-current", harness="codex")
            + row(
                "att-foreign",
                harness="claude",
                parent="att-foreign-parent",
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            self.command(["att-current", "att-foreign"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "rejected")
        self.assertFalse(self.control_path.exists())

    def test_duplicate_attempt_argument_fails_closed(self) -> None:
        self.jobs.write_text(
            row("att-one", harness="codex"), encoding="utf-8"
        )
        result = subprocess.run(
            self.command(["att-one", "att-one"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("attempt-set-invalid", result.stdout)

    def test_direct_managed_codex_and_claude_children_use_hashed_parent(self) -> None:
        attempts = ["att-direct-codex", "att-direct-claude"]
        self.jobs.write_text(
            session_row(attempts[0], harness="codex")
            + session_row(attempts[1], harness="claude"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.session_command(attempts),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        self.assertEqual(server.request["thread_id"], SESSION)
        self.assertTrue(
            server.request["parent_attempt_id"].startswith("parent-session-")
        )
        encoded = json.dumps(server.request)
        self.assertNotIn("RAW_SESSION_CHILD_SENTINEL", encoded)
        self.assertEqual(
            {child["harness"] for child in server.request["receipt"]["children"]},
            {"codex", "claude"},
        )

    def test_retryable_disconnect_reconnect_sends_once_after_server_appears(self) -> None:
        self.jobs.write_text(
            session_row("att-reconnect", harness="codex"),
            encoding="utf-8",
        )
        holder: dict[str, ControlServer] = {}

        def delayed_server() -> None:
            import time
            time.sleep(0.08)
            holder["server"] = ControlServer(self.control_path)

        thread = threading.Thread(target=delayed_server)
        thread.start()
        result = subprocess.run(
            self.session_command(["att-reconnect"], retry_window=1.0),
            text=True,
            capture_output=True,
            timeout=5,
        )
        thread.join(timeout=2)
        server = holder["server"]
        self.addCleanup(server.close)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        self.assertEqual(server.request["sealed_batch_id"], "batch-session")

    def test_prelaunched_sidecar_waits_for_atomic_worker_claim(self) -> None:
        unclaimed = session_row("att-prelaunch", harness="codex").replace(
            "\tdone\t", "\topen\t"
        ).replace(
            "launch_claimed=1,launch_outcome=never-launched,",
            "launch_claimed=0,",
        )
        claimed = unclaimed.replace("launch_claimed=0", "launch_claimed=1")
        self.jobs.write_text(unclaimed, encoding="utf-8")
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)

        def claim_worker() -> None:
            import time
            time.sleep(0.08)
            self.jobs.write_text(claimed, encoding="utf-8")

        thread = threading.Thread(target=claim_worker)
        thread.start()
        result = subprocess.run(
            self.session_command(["att-prelaunch"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))

    def test_never_launched_registration_fails_before_gateway(self) -> None:
        never_launched = session_row(
            "att-never-launched", harness="claude"
        ).replace("launch_claimed=1", "launch_claimed=0")
        self.jobs.write_text(never_launched, encoding="utf-8")
        result = subprocess.run(
            self.session_command(
                ["att-never-launched"], launch_ready_timeout=0.1
            ),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("registered-child-never-launched", result.stdout)
        self.assertFalse(self.control_path.exists())


if __name__ == "__main__":
    unittest.main()
