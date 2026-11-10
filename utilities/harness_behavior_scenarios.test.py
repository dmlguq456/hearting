#!/usr/bin/env python3
"""Offline checks for the real-TUI scenario harness (no TUI is launched)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as hb  # noqa: E402


def _post(base, path, payload):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


class FixtureProtocolTest(unittest.TestCase):
    def setUp(self):
        def script(record, earlier):
            if "RUN" in record["last_text"] and record["last_kind"] != "tool":
                return hb.Reply(shell="echo hi")
            return hb.Reply("answered")
        self.fixture = hb.ModelFixture(script)

    def tearDown(self):
        self.fixture.close()

    def test_anthropic_tool_call_skips_trailing_system_turn(self):
        body = _post(self.fixture.base, "/v1/messages", {
            "stream": True, "tools": [{"name": "Bash"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "RUN now"}]},
                         {"role": "system", "content": "env block"}]})
        self.assertIn('"tool_use"', body)
        self.assertIn('"name":"Bash"', body)
        record = self.fixture.main_requests()[0]
        self.assertEqual(record["last_kind"], "user")
        self.assertEqual(record["reply"]["shell"], "echo hi")

    def test_responses_and_chat_tool_shapes(self):
        body = _post(self.fixture.base, "/v1/responses", {
            "tools": [{"type": "function", "name": "exec_command"}],
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "RUN"}]}]})
        self.assertIn('"function_call"', body)
        self.assertIn('\\"cmd\\": \\"echo hi\\"', body)
        body = _post(self.fixture.base, "/v1/chat/completions", {
            "stream": True, "tools": [{"type": "function", "function": {"name": "shell"}}],
            "messages": [{"role": "user", "content": "RUN"}]})
        self.assertIn('"tool_calls"', body)
        self.assertIn('"name":"shell"', body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))

    def test_tool_result_and_auxiliary_requests(self):
        _post(self.fixture.base, "/v1/chat/completions", {
            "stream": False, "messages": [{"role": "user", "content": "title please"}]})
        _post(self.fixture.base, "/v1/chat/completions", {
            "stream": False, "tools": [{"type": "function", "function": {"name": "bash"}}],
            "messages": [{"role": "user", "content": "RUN"},
                         {"role": "tool", "content": "hi"}]})
        main = self.fixture.main_requests()
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0]["last_kind"], "tool")
        self.assertEqual(main[0]["reply"]["text"], "answered")
        self.assertFalse(self.fixture.requests[0]["main"])


class EnvironmentTest(unittest.TestCase):
    def test_fixture_env_keeps_its_registry_and_hides_herdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "operator-bin"
            bin_dir.mkdir()
            for name in ("herdr", "git"):
                (bin_dir / name).write_text("#!/bin/sh\n")
            with unittest.mock.patch.dict("os.environ", {"PATH": str(bin_dir)}):
                env = hb._fixture_env(root / "fx")
            jobs = Path(env["AGENT_DISPATCH_JOBS"])
            self.assertTrue(jobs.is_file())
            self.assertTrue(str(jobs).startswith(str(root / "fx")))
            self.assertEqual(jobs.parent.stat().st_mode & 0o077, 0)
            shadow = Path(env["PATH"].split(":")[0])
            self.assertTrue((shadow / "git").exists())
            self.assertFalse((shadow / "herdr").exists())
            self.assertTrue(env["TMUX_TMPDIR"].startswith(str(root / "fx")))

    def test_fixture_process_scan_never_matches_the_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(hb.fixture_processes(Path(tmp) / "absent"), [])

    def test_markdown_table_has_one_row_per_scenario(self):
        rows = [{"harness": "claude", "scenario": "boot", "verdict": "pass", "notes": []},
                {"harness": "codex", "scenario": "boot", "verdict": "gap", "notes": ["x"]}]
        table = hb.render_markdown(rows)
        self.assertIn("| boot | pass | gap — x | - |", table)

    def test_gap_is_only_for_a_harness_without_a_declared_carrier(self):
        import harness_behavior_cases_a as cases
        receipt = cases.CompletionReceiptScenario()
        for harness in hb.HARNESSES:
            self.assertTrue(hb.declared_carrier(harness, receipt))
        self.assertTrue(hb.declared_carrier("claude", cases.StopWithRemainingActionScenario()))
        self.assertIsNone(hb.declared_carrier("claude", hb.BootScenario()))
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(hb, "SOURCE", Path(tmp)):
            self.assertIsNone(hb.declared_carrier("claude", receipt))


class TidyContinueCaseTest(unittest.TestCase):
    """The session-tidy continue scenarios register and patch the shared child helper."""

    def test_registered_and_routed_helper_patch_applies(self):
        import harness_behavior_cases_a as ca
        import harness_behavior_cases_tidy as tidy
        hb._load_extra()
        names = [cls.name for cls in tidy.SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertIn(name, hb.SCENARIOS)
            # The fixture root name carries the scenario; a Herdr client socket under it
            # must stay within the UNIX socket path limit.
            sock = f"/tmp/hb-opencode-{name}-xxxxxxxx/home/.config/herdr/herdr-client.sock"
            self.assertLess(len(sock), 104)
        self.assertEqual(ca.FX_HELPER.count(tidy.ROUTED_PATCH[0]), 1)
        patched = ca.FX_HELPER.replace(tidy.ROUTED_PATCH[0], tidy.ROUTED_PATCH[1], 1)
        compile(patched, "hb_fx.py", "exec")
        self.assertIn('"worker_type=frame"', patched)
        compile(tidy.SAMPLER, "continue-look-sampler", "exec")   # runs as `python3 -c`


if __name__ == "__main__":
    unittest.main()
