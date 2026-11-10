#!/usr/bin/env python3
"""Offline checks for the four-axis behaviour scenarios (no TUI is launched)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as hb  # noqa: E402
import harness_behavior_cases_a as ca  # noqa: E402
import harness_behavior_cases_b as cb  # noqa: E402
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


def _record(index, text):
    return {"main_index": index, "text": text, "last_kind": "tool", "last_text": text,
            "protocol": "responses", "at": index, "reply": {"text": "done", "shell": None},
            "main": True}


class _Run:
    """Only the measured inputs of drive() are fixed; the verdict code is real."""

    harness = "codex"

    def __init__(self):
        self.evidence, self.notes, self.rows = {}, [], [_record(0, "initial user turn")]

    def prompt(self, text):
        return True

    def wait_requests(self, *args):
        return True

    def wait_idle(self, *args):
        return len(self.rows)

    def main_requests(self):
        return self.rows

    def note(self, text):
        self.notes.append(text)


class OracleTest(unittest.TestCase):
    """Deliberately broken inputs must not pass (PR #1458 review, oracle-probe)."""

    def burst(self, kind):
        scenario, run, items = cd.ReportBurstScenario(), _Run(), []

        def notify(_run, _sid, values):
            items.extend(values)
            return [str(i) for i in range(20)]

        def flow(*args, **kwargs):
            if kind == "whole":
                text = "\n".join(x["text"] for x in items)
            elif kind == "acked_missing_tail":
                text = "\n".join(x["text"] for x in items[:-1])
            else:
                text = "\n".join(scenario.tails)
            run.rows.append(_record(1, text))
            return {"before": 1, "woken_without_prompt": 0, "typed_rounds": [20]}
        with patch.object(cb, "session_id", return_value="root"), \
                patch.object(cd, "notify", side_effect=notify), \
                patch.object(cd, "notice_states", return_value=["acked"] * 20), \
                patch.object(cd, "prompt_until", side_effect=flow):
            return scenario.drive(run), run.evidence

    def test_burst_counts_whole_bodies_not_tail_markers(self):
        self.assertEqual(self.burst("whole")[0], "pass")
        self.assertEqual(self.burst("acked_missing_tail")[0], "fail")
        verdict, ev = self.burst("tail_tokens_only")
        self.assertEqual(verdict, "fail")
        self.assertEqual((ev["seen"], ev["bodies_seen"]), (20, 0))
        self.assertEqual(len(ev["tail_without_body"]), 20)

    def test_body_match_ignores_whitespace_reflow(self):
        requests = [{"main_index": 0, "text": "- burst 0: A  b\n c TAIL (x)"}]
        self.assertEqual(cd.seen_bodies(requests, ["A b c TAIL", "A b TAIL"]), {0})

    def subsession(self, kind):
        scenario, run = cd.SubsessionCompletionScenario(), _Run()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run.home = SimpleNamespace(root=root)
            registered = "child" if kind.startswith("sub") else "root"
            (root / "hb_fx_state.json").write_text(json.dumps(
                {"harness": "codex", "session_id": registered}))
            run.rows = [_record(0, scenario.main), _record(1, scenario.sub + " HB-REGISTERED")]

            def helper(_run, command):
                if command == "complete":
                    who = scenario.sub if kind.startswith("sub") else scenario.main
                    run.rows.append(_record(2, f"{who} {scenario.attempt} completion"))
                    if kind != "main_wake_no_next":
                        run.rows.append(_record(3, f"{who} {scenario.NEXT}-42"))
                    if kind == "sub_wake_relayed":
                        run.rows.append(_record(4, f"{scenario.main} {scenario.attempt} relayed"))
                        run.rows.append(_record(5, f"{scenario.main} {scenario.NEXT}-42"))
                return {"command": command}
            with patch.object(cb, "session_id", return_value="root"), \
                    patch.object(ca, "_helper", side_effect=helper), \
                    patch.object(cd.time, "sleep"), \
                    patch.object(cd, "wait_for", side_effect=lambda fn, *a, **kw: fn()):
                return scenario.drive(run), run.evidence

    def test_subsession_passes_only_when_the_root_receives_and_acts(self):
        self.assertEqual(self.subsession("main_wake_next_ran")[0], "pass")
        self.assertEqual(self.subsession("main_wake_no_next")[0], "fail")
        verdict, ev = self.subsession("sub_wake_next_ran")
        self.assertEqual(verdict, "fail")
        self.assertEqual((ev["woken_thread"], ev["root_received"]), ("sub", False))
        verdict, ev = self.subsession("sub_wake_relayed")
        self.assertEqual(verdict, "pass")
        self.assertTrue(ev["root_next_action_ran"])

    def herdr(self, agent):
        scenario, run = cd.HerdrPeerPromptScenario(), _Run()
        run.harness = "opencode"
        run.fixture = SimpleNamespace(requests=[])
        run.home = SimpleNamespace(env={}, workspace=Path("/tmp"))
        run.herdr = {"session": "private-fake"}

        def herdr(*args, **kwargs):
            payload = ([{"pane_id": "w1:p1", "agent": agent, "agent_status": "idle"}]
                       if args[:2] == ("agent", "list") else {})
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        run.tui = SimpleNamespace(pane="w1:p1", settle=lambda *a: True,
                                  screen=lambda: "composer empty", herdr=herdr)
        fake = SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(
            returncode=0, stdout="prompted=queued", stderr=""))
        with patch.object(cb, "session_id", return_value="root"), \
                patch.object(cb, "first_with", return_value=1), \
                patch.object(cd.time, "sleep"), \
                patch.object(cd, "wait_for", side_effect=lambda fn, *a, **kw: fn()), \
                patch.object(scenario, "source_changed", return_value=False), \
                patch.object(cd, "subprocess", fake):
            return scenario.drive(run), run.evidence

    def test_herdr_detection_needs_the_launched_harness(self):
        self.assertEqual(self.herdr("opencode")[0], "pass")
        for wrong in ("codex", None):
            verdict, ev = self.herdr(wrong)
            self.assertEqual((verdict, ev["verdict_detection"]), ("fail", "fail"), wrong)


class SourceGuardTest(unittest.TestCase):
    """The real prepare() on a linked dummy source (PR #1458 review, source-write-probe)."""

    def test_prepare_writes_no_linked_source_file(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.invalid",
                   "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.invalid"}

            def git(*args):
                return real_run(["git", "-C", str(source), *args], env=env, text=True,
                                capture_output=True, check=True).stdout
            git("init", "-q")
            target = source / "settings.json"
            target.write_text("{}\n")
            git("add", ".")
            git("commit", "-qm", "fixture")
            claude, codex, config = root / "claude", root / "codex", root / "config"
            for folder in (claude, codex, config, root / "home"):
                folder.mkdir()
            (claude / "settings.json").symlink_to(target)
            initial = target.read_bytes()
            run = SimpleNamespace(harness="claude", evidence={}, home=SimpleNamespace(
                env={**env, "CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex),
                     "XDG_CONFIG_HOME": str(config)},
                workspace=root, configure_model=lambda: None))
            scenario = cd.HerdrPeerPromptScenario()

            def harmless(argv, **kw):
                if argv and argv[0] == "git":
                    return real_run(argv, **kw)
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            fake = SimpleNamespace(run=harmless, DEVNULL=subprocess.DEVNULL,
                                   TimeoutExpired=subprocess.TimeoutExpired,
                                   Popen=lambda *a, **kw: SimpleNamespace(pid=-999,
                                                                          poll=lambda: 0))
            try:
                with patch.object(hb, "SOURCE", source), \
                        patch.object(cd, "real_herdr", return_value="/bin/true"), \
                        patch.object(cd, "subprocess", fake), \
                        patch.object(cd, "wait_for", return_value=True):
                    scenario.prepare(run)
                    changed = scenario.source_changed(run)
                    self.assertEqual(target.read_bytes(), initial)
                    self.assertFalse(changed)
                    self.assertFalse((claude / "settings.json").is_symlink())
                    self.assertTrue(json.loads((claude / "settings.json").read_text())
                                    ["promptSuggestionEnabled"])
                    # A second edit to an already-dirty file is still a change.
                    target.write_text("{\"a\": 1}\n")
                    dirty = cd.source_status()
                    target.write_text("{\"a\": 2}\n")
                    self.assertNotEqual(cd.source_status(), dirty)
            finally:
                if scenario.scratch:
                    shutil.rmtree(scenario.scratch, ignore_errors=True)


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
