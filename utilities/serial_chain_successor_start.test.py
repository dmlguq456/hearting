#!/usr/bin/env python3
"""Regression coverage for the checked serial-chain successor boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_subsession_advance as advance


def _load_chain():
    spec = importlib.util.spec_from_file_location(
        "serial_chain_start_test_chain", ROOT / "utilities" / "stage-session-chain.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _request(tmp: Path, adapter: str = "claude"):
    session = {
        "index": 2, "count": 3, "adapter": adapter, "subsession_id": "ss-chain-2",
        "attempt_id": "att-chain-2", "slug": "execute-s2", "phase_brief": str(tmp / "brief.md"),
        "fixed_files": ["utilities/example.py"], "narrow_verify": "true",
        "expected_round_trips": 2,
    }
    (tmp / "brief.md").write_text("brief\n", encoding="utf-8")
    return advance.SubsessionAdvanceRequest(
        jobs=tmp / "jobs.log", route_id="rt-test", route_hash="sha256:test",
        route_node="execute", chain_id="ssc-test", manifest_sha256="sha256:manifest",
        predecessor_subsession_id="ss-chain-1", predecessor_terminal_attempt_id="att-owner",
        successor_subsession_index=2, successor_session=session, parent_attempt_id="att-owner",
        parent_slug="owner-slug", registered_parent_sid="parent-session",
        registered_parent_cwd=str(tmp),
    )


class StartReceiptTest(unittest.TestCase):
    def test_three_start_shapes_are_strict(self):
        chain = _load_chain()
        success = "check=ok\nattempt_id=att-x\nregistered=1\nstarted=1\nduplicate_attempt=0\nchild_spawned=1\n"
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                self.assertTrue(chain.parse_start_receipt(0, success, "att-x")["ok"])
                no_spawn = success.replace("child_spawned=1", "child_spawned=0")
                self.assertEqual(chain.parse_start_receipt(0, no_spawn, "att-x")["verdict"], "not-spawned")
                self.assertEqual(chain.parse_start_receipt(75, success, "att-x")["verdict"], "returncode-nonzero")

    def test_duplicate_and_foreign_receipts_fail_closed(self):
        chain = _load_chain()
        base = "check=ok\nattempt_id=att-x\nregistered=1\nstarted=1\nchild_spawned=1\n"
        self.assertEqual(
            chain.parse_start_receipt(0, base + "duplicate_attempt=1\n", "att-x")["verdict"],
            "duplicate-attempt",
        )
        self.assertEqual(
            chain.parse_start_receipt(0, base.replace("att-x", "att-y") + "duplicate_attempt=0\n", "att-x")["verdict"],
            "attempt-mismatch",
        )


class RealSuccessorCommandTest(unittest.TestCase):
    def test_real_service_uses_exact_slug_and_parent_context_for_each_adapter(self):
        chain = _load_chain()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            request_manifest = {
                "route_file": str(tmp / "route.json"), "route_node": "execute",
                "mode": "serial", "chain_id": "ssc-test",
            }
            (tmp / "jobs.log").touch()
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner"}, clear=False):
                for adapter in ("claude", "codex", "opencode"):
                    request = _request(tmp, adapter)
                    service = advance.RealSubsessionAdvanceServices(request_manifest)
                    captured = {}

                    def run(command, env=None):
                        captured["command"] = command
                        captured["env"] = env
                        return SimpleNamespace(
                            returncode=0,
                            stdout=(
                                "check=ok\nattempt_id=att-chain-2\nregistered=1\nstarted=1\n"
                                "duplicate_attempt=0\nchild_spawned=1\n"
                            ),
                            stderr="",
                        )

                    service._chain.run_checked = run
                    result = service.start_successor(request, claim=None)
                    self.assertTrue(result["child_spawned"])
                    self.assertIn("--parent", captured["command"])
                    self.assertEqual(captured["command"][captured["command"].index("--parent") + 1], "owner-slug")
                    self.assertIn("--adapter", captured["command"])
                    self.assertEqual(captured["command"][captured["command"].index("--adapter") + 1], adapter)
                    self.assertEqual(captured["env"]["AGENT_DISPATCH_ATTEMPT_ID"], "att-owner")
                    self.assertEqual(captured["env"]["AGENT_DISPATCH_PARENT_SESSION_ID"], "parent-session")
                    self.assertEqual(captured["env"]["AGENT_DISPATCH_PARENT_CWD"], str(tmp))

    def test_parent_binding_and_adapter_prechecks_do_not_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            request = _request(tmp)
            service = advance.RealSubsessionAdvanceServices({"route_file": "x", "route_node": "execute", "mode": "serial", "chain_id": "ssc-test"})
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "foreign"}, clear=False):
                with mock.patch.object(service._chain, "run_checked") as run:
                    result = service.start_successor(request, claim=None)
            self.assertEqual(result["reason"], "subsession-advance-parent-binding-invalid")
            run.assert_not_called()
            request.successor_session["adapter"] = "bogus"
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner"}, clear=False):
                result = service.start_successor(request, claim=None)
            self.assertEqual(result["reason"], "subsession-advance-adapter-invalid")


class SerialDriverControlFlowTest(unittest.TestCase):
    def test_three_successors_reset_repark_budget_and_aggregate_traversal(self):
        rows = [SimpleNamespace(attempt_id="att-1", status="done", metadata={"session_chain_id": "ssc", "subsession_mode": "serial"})]
        steps = iter([
            advance.ChainAdvanceStep("advanced", chain_id="ssc", predecessor_index=1, successor_index=2, attempt_id="att-2"),
            advance.ChainAdvanceStep("advanced", chain_id="ssc", predecessor_index=2, successor_index=3, attempt_id="att-3"),
            advance.ChainAdvanceStep("complete", chain_id="ssc", predecessor_index=3),
        ])
        joins = []
        timeouts = []
        with mock.patch.object(advance, "advance_chain_step", side_effect=lambda *args: next(steps)):
            result = advance.drive_serial_chain(
                jobs=Path("/unused/jobs"), parent_attempt_id="owner", attempts={"att-1"},
                receipt={"state": "completed"}, refresh=lambda attempts: rows,
                join=lambda attempts: joins.append(set(attempts)) or {"state": "completed"},
                reconcile=lambda rows, attempts: False, max_reparks=1,
                on_timeout=lambda attempts: timeouts.append(set(attempts)),
            )
        self.assertEqual(result.last_advanced_attempt_id, "att-3")
        self.assertEqual(result.attempts, frozenset({"att-3"}))
        self.assertEqual(result.traversed, frozenset({"att-1", "att-2"}))
        self.assertEqual(joins, [{"att-2"}, {"att-3"}])
        self.assertEqual(timeouts, [])


if __name__ == "__main__":
    unittest.main()
