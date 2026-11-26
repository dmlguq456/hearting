#!/usr/bin/env python3
"""Every wrapper prints the same shared receipt lines; only its translations differ."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import launch_receipt as L  # noqa: E402


def args(**overrides):
    base = dict(attempt_id="att-1", launch_authority="conductor", fallback_ordinal=0, fallback_hop="-",
                execution_surface="headless", registered_worker=True, attempt_claimed=True, child_pid=42,
                parent_completion_delivery="claude-parent-runtime", agent_home="/h", launch_lifecycle="detached",
                launch_lifecycle_requested="detached",
                launch_lifecycle_resolution=SimpleNamespace(reselection="-", override="-"))
    base.update(overrides)
    return SimpleNamespace(**base)


def keys(lines):
    return [line.split("=", 1)[0] for line in lines]


class SharedReceiptTest(unittest.TestCase):
    def test_attempt_lines_carry_the_fields_a_parent_reads(self):
        lines = L.attempt_lines(args(), jobs="/s/jobs.log", registry_source="inherited", action="start",
                                launch_state="claimed", after_lifecycle=["runtime_sandbox=x"],
                                before_early_death=["nested_codex_home=-"])
        found = keys(lines)
        for key in ("registry_lock", "terminal_verdict", "handoff_state", "handoff_verdict", "parent_next",
                    "child_spawned", "worker_exit", "early_death"):
            self.assertIn(key, found)
        self.assertLess(found.index("launch_lifecycle_override"), found.index("runtime_sandbox"))
        self.assertLess(found.index("runtime_sandbox"), found.index("worker_exit"))
        self.assertEqual(found[-2:], ["nested_codex_home", "early_death"])

    def test_completion_lines_have_defaults_for_a_wrapper_without_a_sidecar(self):
        lines = L.completion_lines(SimpleNamespace())
        self.assertEqual(keys(lines)[0], "completion_delivery")
        self.assertIn("managed_sidecar_state=not-started", lines)
        self.assertIn("parent_completion_reason=unspecified", lines)

    def test_every_wrapper_prints_the_shared_blocks(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                source = (ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text()
                self.assertEqual(source.count("launch_receipt.attempt_lines("), 1)
                self.assertEqual(source.count("launch_receipt.completion_lines(args)"), 1)
                self.assertNotIn('print(f"registry_lock=', source)
                self.assertNotIn('print(f"terminal_verdict=', source)

    def test_pre_carrier_opencode_parent_gets_relaunch_notice(self):
        old = args(parent_harness="opencode", parent_completion_delivery="poll-fallback",
                   parent_completion_reason="parent-identity-unmatched")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_PARENT_COMPLETION_CARRIER", None)
            lines = L.attempt_lines(old, jobs="/s/jobs.log", registry_source="inherited",
                                    action="start", launch_state="claimed")
        notice = next(line for line in lines if line.startswith("opencode_carrier_notice="))
        # The server, not the TUI session, loads the plugin; the line names the wait in use.
        self.assertIn("bounded-wait", notice)
        self.assertIn("OpenCode 서버", notice)
        self.assertNotIn("세션을 다시 띄우면", notice)
        self.assertLess(lines.index("parent_next=bounded-wait"), lines.index(notice))

    def test_carrier_opencode_parent_gets_no_notice(self):
        new = args(parent_harness="opencode", parent_completion_delivery="opencode-turn",
                   parent_completion_reason="opencode-plugin-turn")
        with mock.patch.dict(os.environ, {"AGENT_PARENT_COMPLETION_CARRIER": "opencode-turn:ses_x"}):
            lines = L.attempt_lines(new, jobs="/s/jobs.log", registry_source="inherited",
                                    action="start", launch_state="claimed")
        self.assertFalse(any(line.startswith("opencode_carrier_notice=") for line in lines))

    def test_non_opencode_parent_gets_no_notice(self):
        other = args(parent_harness="claude", parent_completion_delivery="poll-fallback",
                     parent_completion_reason="parent-identity-unmatched")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_PARENT_COMPLETION_CARRIER", None)
            lines = L.attempt_lines(other, jobs="/s/jobs.log", registry_source="inherited",
                                    action="start", launch_state="claimed")
        self.assertFalse(any(line.startswith("opencode_carrier_notice=") for line in lines))


if __name__ == "__main__":
    unittest.main()
