#!/usr/bin/env python3
"""Offline checks for the four-axis behaviour scenarios (no TUI is launched)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as hb  # noqa: E402
import harness_behavior_cases_d as cd  # noqa: E402


def _post(base, path, payload):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


class NativeCallTest(unittest.TestCase):
    def test_spawn_call_uses_each_harness_sub_agent_tool(self):
        call = cd.spawn_call("memory-scout", "do it")
        self.assertEqual(call.build("anthropic", [])[0], "Agent")
        self.assertEqual(call.build("anthropic", [])[1]["subagent_type"], "memory-scout")
        name, args = call.build("responses", [])
        self.assertEqual(name, "multi_agent_v1::spawn_agent")
        self.assertEqual(args, {"agent_type": "memory-scout", "message": "do it"})
        name, args = call.build("chat", [])
        self.assertEqual((name, args["agent"]), ("subagent", "memory-scout"))
        self.assertTrue(str(call).startswith("spawn:"))
        self.assertEqual(cd.wait_call("a1").build("responses", [])[1]["targets"], ["a1"])

    def test_error_tool_reads_a_missing_workspace_file(self):
        workspace = Path("/tmp/ws")
        call = cd.error_tool(workspace)
        self.assertEqual(call.build("anthropic", []),
                         ("Read", {"file_path": str(workspace / cd.MISSING_FILE)}))
        self.assertEqual(call.build("chat", [])[0], "read")
        name, args = call.build("responses", ["exec_command"])
        self.assertEqual(name, "exec_command")
        self.assertIn(cd.MISSING_FILE, args["cmd"])

    def test_namespaced_call_is_split_on_the_wire(self):
        body = b'{"type":"function_call","name":"multi_agent_v1::spawn_agent","arguments":"{}"}'
        fixed = cd.split_namespaced(body)
        self.assertIn(b'"name":"spawn_agent","namespace":"multi_agent_v1"', fixed)
        self.assertEqual(cd.split_namespaced(b'"name":"exec_command"'), b'"name":"exec_command"')

    def test_fixture_emits_namespace_for_codex_spawn(self):
        def script(record, earlier):
            return hb.Reply(shell=cd.spawn_call("default", "sub task"))
        cd._patch_namespaces(hb)
        fixture = hb.ModelFixture(script)
        try:
            body = _post(fixture.base, "/v1/responses", {
                "tools": [{"type": "function", "name": "exec_command"}],
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "go"}]}]})
        finally:
            fixture.close()
        self.assertIn('"name":"spawn_agent","namespace":"multi_agent_v1"', body)


class CountingTest(unittest.TestCase):
    @staticmethod
    def requests(*texts):
        return [{"main_index": i, "text": text} for i, text in enumerate(texts)]

    def test_new_arrivals_ignores_history_repeats(self):
        requests = self.requests("a", "a M", "a M b", "a M b M")
        self.assertEqual(cd.new_arrivals(requests, "M"), [1, 3])
        self.assertEqual(cd.new_arrivals(self.requests("x", "y"), "M"), [])

    def test_seen_markers_starts_at_the_given_request(self):
        requests = self.requests("T1", "T2 T3")
        self.assertEqual(cd.seen_markers(requests, ["T1", "T2", "T9"]), {"T1", "T2"})
        self.assertEqual(cd.seen_markers(requests, ["T1", "T2"], start=1), {"T2"})

    def test_sub_session_requests_carry_only_the_sub_marker(self):
        self.assertTrue(cd._is_sub({"text": "SUB task"}, "MAIN", "SUB"))
        self.assertFalse(cd._is_sub({"text": "MAIN spawn SUB"}, "MAIN", "SUB"))


class OwnerRouteTest(unittest.TestCase):
    def test_synthetic_routes_have_valid_ids_and_their_scope(self):
        import commit_policy
        import route_identity
        with tempfile.TemporaryDirectory() as tmp:
            for shape, allowed in (("unscoped", True), ("complete", True), ("report", False)):
                route = cd.synthetic_route(Path(tmp) / "ws", Path(tmp) / "art", shape)
                digest = route_identity.route_hash(route)
                self.assertEqual(route["route_hash"], digest)
                self.assertEqual(route["route_id"], route_identity.route_id_from_hash(digest))
                self.assertIs(commit_policy._entry_scope_allows_publication(route), allowed)

    def test_probe_command_does_not_echo_its_own_verdict_tokens(self):
        scenario = cd.OwnerPublishScenario()
        scenario.port = 9
        command = scenario.probe("unscoped")
        self.assertNotIn("HB-NET-unscoped-REACHED", command)
        self.assertNotIn("HB-NET-unscoped-BLOCKED", command)
        self.assertIn("git push origin HEAD:refs/heads/hb-unscoped", command)


class DelinkTest(unittest.TestCase):
    def test_only_links_into_the_measured_tree_become_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "plugins").mkdir(parents=True)
            (source / "hooks.json").write_text("{}", encoding="utf-8")
            (source / "plugins" / "p.js").write_text("x", encoding="utf-8")
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            codex = root / "codex"
            codex.mkdir()
            (codex / "hooks.json").symlink_to(source / "hooks.json")
            (codex / "other.json").symlink_to(outside)
            (codex / "plugins").symlink_to(source / "plugins")
            env = {"CODEX_HOME": str(codex), "XDG_CONFIG_HOME": str(root / "xdg")}
            original = hb.SOURCE
            hb.SOURCE = source
            try:
                changed = cd.delink_config(env)
            finally:
                hb.SOURCE = original
            self.assertFalse((codex / "hooks.json").is_symlink())
            self.assertTrue((codex / "other.json").is_symlink())
            self.assertFalse((codex / "plugins").is_symlink())
            self.assertEqual((codex / "plugins" / "p.js").read_text(encoding="utf-8"), "x")
            (codex / "hooks.json").write_text('{"changed": 1}', encoding="utf-8")
            self.assertEqual((source / "hooks.json").read_text(encoding="utf-8"), "{}")
            self.assertEqual(len(changed), 2)


class RegistrationTest(unittest.TestCase):
    def test_every_scenario_is_loaded_and_names_its_register_row(self):
        hb._load_extra()
        for cls in cd.SCENARIOS:
            self.assertIn(cls.name, hb.SCENARIOS)
            self.assertRegex(cd.PG[cls.name], r"^PG-\d+(,PG-\d+)*$")
        self.assertEqual(set(cd.PG), {cls.name for cls in cd.SCENARIOS})

    def test_headless_scenario_skips_the_terminal(self):
        self.assertFalse(cd.OwnerPublishScenario.needs_tui)
        self.assertTrue(cd.QuotedPeerFromScenario.needs_tui)
        tui = hb.Scenario().make_tui(
            type("R", (), {"home": type("H", (), {"env": {}, "workspace": Path("/tmp")})()})(),
            Path("/tmp"), ["true"])
        try:
            self.assertIsInstance(tui, hb.PrivateTui)
        finally:
            tui.stop()

    def test_list_includes_the_new_scenarios(self):
        done = subprocess.run([sys.executable, str(Path(__file__).with_name(
            "harness_behavior_scenarios.py")), "--list"], text=True, capture_output=True,
            timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        for cls in cd.SCENARIOS:
            self.assertIn(cls.name + "\t", done.stdout)


if __name__ == "__main__":
    unittest.main()
