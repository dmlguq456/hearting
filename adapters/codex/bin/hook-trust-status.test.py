#!/usr/bin/env python3
"""Tests for authoritative Codex hook current-hash trust inspection."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "hook_trust_status", HERE / "hook-trust-status.py"
)
TRUST = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = TRUST
SPEC.loader.exec_module(TRUST)


class FakeServer:
    def __init__(self, result):
        self.result = result
        self.closed = False

    def request(self, method, _params):
        if method == "initialize":
            return {}
        if method == "hooks/list":
            return self.result
        raise AssertionError(method)

    def notify(self, method):
        if method != "initialized":
            raise AssertionError(method)

    def close(self):
        self.closed = True


class HookTrustStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hooks = self.root / "hooks.json"
        self.hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "python stop.py",
                                        "timeout": 600,
                                    }
                                ]
                            }
                        ],
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [
                                    {"type": "command", "command": "python guard.py"}
                                ],
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

    def result(self, stop_status="trusted", include_pretool=True):
        hooks = [
            {
                "sourcePath": str(self.hooks),
                "eventName": "stop",
                "enabled": True,
                "trustStatus": stop_status,
            }
        ]
        if include_pretool:
            hooks.append(
                {
                    "sourcePath": str(self.hooks),
                    "eventName": "pre_tool_use",
                    "enabled": True,
                    "trustStatus": "trusted",
                }
            )
        return {"data": [{"cwd": str(self.root), "hooks": hooks}]}

    def inspect(self, result):
        fake = FakeServer(result)
        with mock.patch.object(TRUST, "AppServer", return_value=fake):
            value = TRUST.inspect_trust(
                hooks_file=self.hooks,
                cwd=self.root,
                command=["codex", "app-server"],
            )
        self.assertTrue(fake.closed)
        return value

    def test_all_current_hashes_are_authoritatively_trusted(self):
        trusted, reason, events = self.inspect(self.result())
        self.assertTrue(trusted)
        self.assertEqual(reason, "current-hash-trusted")
        self.assertEqual(events, ["pre_tool_use", "stop"])

    def test_modified_current_hash_requires_review(self):
        trusted, reason, events = self.inspect(self.result(stop_status="modified"))
        self.assertFalse(trusted)
        self.assertEqual(reason, "current-hash-not-trusted")
        self.assertEqual(events, ["stop"])

    def test_discovery_set_mismatch_fails_closed(self):
        trusted, reason, events = self.inspect(self.result(include_pretool=False))
        self.assertFalse(trusted)
        self.assertEqual(reason, "definition-set-mismatch")
        self.assertEqual(events, ["pre_tool_use", "stop"])

    def test_app_server_request_timeout_terminates_unresponsive_child(self):
        server = TRUST.AppServer(
            [sys.executable, "-c", "import time; time.sleep(10)"], self.root
        )
        with self.assertRaisesRegex(TRUST.HookTrustError, "app-server-timeout"):
            server.request("initialize", {}, timeout=0.05)
        self.assertIsNotNone(server.process.poll())

    def test_parent_definition_proof_is_reported_with_typed_paths(self):
        ledger = self.root / "ledger.json"
        lock = self.root / "ledger.lock"
        proof = mock.Mock(
            eligible=True,
            reason="parent-definition-proven",
            parent_start_ms=2001,
            definition_ms=2000,
        )
        output = io.StringIO()
        with mock.patch.object(
            TRUST, "inspect_trust",
            return_value=(True, "current-hash-trusted", ["pre_tool_use", "stop"]),
        ), mock.patch.object(
            TRUST, "prove_parent_definition", return_value=proof,
        ) as parent_proof, redirect_stdout(output):
            rc = TRUST.main(
                [
                    "--hooks-file", str(self.hooks),
                    "--cwd", str(self.root),
                    "--parent-session-id", "parent-v7",
                    "--ledger-path", str(ledger),
                    "--lock-path", str(lock),
                ]
            )
        self.assertEqual(rc, 0)
        parent_proof.assert_called_once_with(
            "parent-v7", hooks_path=self.hooks.absolute(),
            ledger_path=ledger, lock_path=lock,
        )
        self.assertIn("parent_definition=proven", output.getvalue())
        self.assertIn("parent_reason=parent-definition-proven", output.getvalue())
        self.assertIn("status=trusted", output.getvalue())

    def test_unproven_parent_cannot_be_overridden_by_current_hash_trust(self):
        proof = mock.Mock(
            eligible=False,
            reason="parent-older-than-definition",
            parent_start_ms=1999,
            definition_ms=2000,
        )
        output = io.StringIO()
        with mock.patch.object(
            TRUST, "inspect_trust",
            return_value=(True, "current-hash-trusted", ["pre_tool_use", "stop"]),
        ), mock.patch.object(
            TRUST, "prove_parent_definition", return_value=proof,
        ), redirect_stdout(output):
            rc = TRUST.main(
                [
                    "--hooks-file", str(self.hooks),
                    "--cwd", str(self.root),
                    "--parent-session-id", "older-parent",
                ]
            )
        self.assertEqual(rc, 3)
        self.assertIn("parent_definition=unproven", output.getvalue())
        self.assertIn("parent_reason=parent-older-than-definition", output.getvalue())


if __name__ == "__main__":
    unittest.main()
